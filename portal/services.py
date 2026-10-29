"""Authorized lifecycle commands; every state change and its audit commit together."""
from datetime import date
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F, Max
from django.utils import timezone

from .access import can_access, require_role, scoped_institutions
from .models import AuditEvent, Document, Instrument, Notification, Observation, Policy, ReleaseRequest, Role, Submission, Verification


OPEN_RELEASE_STATES = ["submitted", "corrections", "recommended", "approved"]
OPEN_SUCCESSOR_STATES = ["draft", "submitted", "review", "corrections", "accepted"]
EDITABLE_FIELDS = ["kind", "reference", "bank_name", "ifsc", "amount", "issue_date", "maturity_date", "validity_date", "policy"]
FINANCIAL_FIELDS = ["kind", "reference", "bank_name", "ifsc", "amount", "issue_date", "maturity_date", "validity_date"]


def audit(actor, instrument, action, **details):
    event = AuditEvent.objects.create(actor=actor, institution=instrument.institution, instrument=instrument, action=action, details=details)
    audiences = {
        "instrument.submitted": [Role.REVIEWER, Role.INSTITUTION], "instrument.withdrawn": [Role.REVIEWER],
        "instrument.superseded": [Role.INSTITUTION], "review.start": [Role.INSTITUTION],
        "review.corrections": [Role.INSTITUTION], "review.reject": [Role.INSTITUTION], "review.accept": [Role.INSTITUTION],
        "verification.recorded": [Role.INSTITUTION, Role.REVIEWER], "release.requested": [Role.REVIEWER, Role.INSTITUTION],
        "release.resubmit": [Role.REVIEWER], "release.corrections": [Role.INSTITUTION],
        "release.recommend": [Role.APPROVER, Role.INSTITUTION], "release.approve": [Role.REVIEWER, Role.INSTITUTION],
        "release.reject": [Role.REVIEWER, Role.INSTITUTION], "release.complete": [Role.REVIEWER, Role.APPROVER, Role.INSTITUTION],
        "release.withdraw": [Role.REVIEWER, Role.APPROVER],
    }
    if action in audiences:
        users = get_user_model().objects.filter(is_active=True, profile__role__in=audiences[action], profile__institutions=instrument.institution).exclude(pk=actor.pk).distinct()
        label = action.replace(".", " ").replace("_", " ").capitalize()
        link = f"/releases/{details['request_id']}/" if details.get("request_id") else f"/instruments/{instrument.pk}/"
        for user in users:
            send_email = getattr(settings, "SMTP_ENABLED", False) and bool(user.email)
            Notification.objects.create(user=user, instrument=instrument, title=f"{label}: {instrument.reference}"[:200],
                                        body=f"{instrument.institution.name}: {label.lower()}. {details.get('note', '')}".strip(),
                                        link=link, dedupe_key=f"event:{event.pk}:user:{user.pk}",
                                        email_status="queued" if send_email else "disabled", next_attempt_at=timezone.now() if send_email else None)
    return event


def _authorized(actor, instrument, *roles):
    require_role(actor, *roles)
    if not can_access(actor, instrument):
        raise PermissionDenied("This institution is outside your assigned scope.")


def _fresh_instrument(instrument):
    return Instrument.objects.select_for_update(of=("self",)).select_related("institution", "policy", "predecessor").get(pk=instrument.pk)


def _check_version(record, expected_version):
    try:
        matches = record.version == int(expected_version)
    except (TypeError, ValueError):
        matches = False
    if not matches:
        raise ValidationError("This record changed while you were working. Reload it and review the latest version.")


def _cas(record, expected_version, **changes):
    _check_version(record, expected_version)
    try:
        with transaction.atomic():
            changed = type(record).objects.filter(pk=record.pk, version=expected_version).update(**changes, version=F("version") + 1, updated_at=timezone.now())
    except IntegrityError as exc:
        raise ValidationError("This action conflicts with an existing receipt or lifecycle request. Reload and check the current record.") from exc
    if changed != 1:
        raise ValidationError("Another action changed this record. Reload before continuing.")
    record.refresh_from_db()
    return record


def _no_open_release(instrument):
    if instrument.release_requests.filter(status__in=OPEN_RELEASE_STATES).exists():
        raise ValidationError("Resolve or withdraw the open release request before changing the instrument lifecycle.")


def _snapshot(instrument):
    result = {name: str(getattr(instrument, name)) if getattr(instrument, name) is not None else None for name in FINANCIAL_FIELDS}
    result.update(institution_id=instrument.institution_id, policy_id=instrument.policy_id, policy_version=instrument.policy.version,
                  documents=[str(pk) for pk in instrument.documents.values_list("pk", flat=True)], version=instrument.version)
    return result


def _validate_duplicate(instrument):
    # Renewals may retain the original receipt reference; unrelated instruments may not.
    ancestors = []
    predecessor = instrument.predecessor
    while predecessor:
        if predecessor.pk in ancestors:
            raise ValidationError("The renewal history contains a cycle.")
        ancestors.append(predecessor.pk)
        predecessor = predecessor.predecessor
    duplicate = Instrument.objects.filter(ifsc__iexact=instrument.ifsc, reference__iexact=instrument.reference, issue_date=instrument.issue_date,
                                          status__in=["submitted", "review", "corrections", "accepted", "superseded", "released"]).exclude(pk=instrument.pk)
    if duplicate.exists():
        raise ValidationError("This receipt reference is already registered at the issuing branch. Use the renewal workflow for an existing instrument.")


@transaction.atomic
def create_instrument(actor, data, predecessor=None):
    require_role(actor, Role.INSTITUTION)
    institution = data.get("institution")
    institution_id = getattr(institution, "pk", institution)
    if not scoped_institutions(actor).filter(pk=institution_id).exists():
        raise PermissionDenied("You cannot create records for this institution.")
    institution = scoped_institutions(actor).get(pk=institution_id)
    policy = data.get("policy")
    if not isinstance(policy, Policy):
        try:
            policy = Policy.objects.get(pk=policy, is_active=True)
        except (Policy.DoesNotExist, ValueError, TypeError) as exc:
            raise ValidationError("Choose an active policy.") from exc
    if not policy.is_active:
        raise ValidationError("Choose an active policy.")
    if predecessor:
        predecessor = _fresh_instrument(predecessor)
        _authorized(actor, predecessor, Role.INSTITUTION)
        if predecessor.institution_id != institution.pk or predecessor.status != Instrument.Status.ACCEPTED:
            raise ValidationError("Only an accepted instrument at this institution can be renewed or replaced.")
        _no_open_release(predecessor)
        if predecessor.successors.filter(status__in=OPEN_SUCCESSOR_STATES).exists():
            raise ValidationError("A renewal or replacement is already in progress.")
        _cas(predecessor, predecessor.version)
    values = {name: data[name] for name in EDITABLE_FIELDS if name in data and name != "policy"}
    instrument = Instrument(institution=institution, policy=policy, created_by=actor, predecessor=predecessor, **values)
    instrument.full_clean()
    instrument.save()
    audit(actor, instrument, "instrument.created", predecessor=str(predecessor.pk) if predecessor else None)
    return instrument


@transaction.atomic
def edit_instrument(actor, instrument, data, expected_version):
    instrument = _fresh_instrument(instrument)
    _authorized(actor, instrument, Role.INSTITUTION)
    _check_version(instrument, expected_version)
    if instrument.status not in [Instrument.Status.DRAFT, Instrument.Status.CORRECTIONS]:
        raise ValidationError("Only drafts and records returned for correction can be edited.")
    previous = _snapshot(instrument)
    changes = {name: data[name] for name in EDITABLE_FIELDS if name in data}
    if "policy" in changes:
        policy = changes["policy"]
        if not isinstance(policy, Policy):
            policy = Policy.objects.get(pk=policy)
        if policy.pk != instrument.policy_id and (instrument.submitted_at or not policy.is_active):
            raise ValidationError("Submitted cases retain their original policy version.")
        changes["policy"] = policy
    for name, value in changes.items():
        setattr(instrument, name, value)
    instrument.full_clean()
    changes = {name: getattr(instrument, name) for name in changes}
    # Independent confirmation applies to the exact values originally checked.
    if any(previous[name] != (str(getattr(instrument, name)) if getattr(instrument, name) is not None else None) for name in FINANCIAL_FIELDS):
        changes["bank_status"] = Instrument.BankStatus.UNCONFIRMED
    instrument = _cas(instrument, expected_version, **changes)
    audit(actor, instrument, "instrument.edited", before=previous, after=_snapshot(instrument))
    return instrument


@transaction.atomic
def submit_instrument(actor, instrument, expected_version):
    instrument = _fresh_instrument(instrument)
    _authorized(actor, instrument, Role.INSTITUTION)
    _check_version(instrument, expected_version)
    if instrument.status not in [Instrument.Status.DRAFT, Instrument.Status.CORRECTIONS]:
        raise ValidationError("This instrument cannot be submitted in its current state.")
    instrument.full_clean()
    if instrument.missing_documents:
        raise ValidationError("Upload required documents before submission: " + ", ".join(instrument.missing_documents))
    _validate_duplicate(instrument)
    if instrument.predecessor_id:
        predecessor = _fresh_instrument(instrument.predecessor)
        if predecessor.status != Instrument.Status.ACCEPTED:
            raise ValidationError("The previous instrument is no longer available for renewal.")
        _no_open_release(predecessor)
    instrument = _cas(instrument, expected_version, status=Instrument.Status.SUBMITTED, submitted_at=timezone.now(), decided_at=None)
    number = (instrument.submissions.aggregate(number=Max("number"))["number"] or 0) + 1
    Submission.objects.create(instrument=instrument, number=number, snapshot=_snapshot(instrument), created_by=actor)
    audit(actor, instrument, "instrument.submitted", submission=number)
    return instrument


@transaction.atomic
def review_instrument(actor, instrument, action, note, expected_version):
    instrument = _fresh_instrument(instrument)
    _authorized(actor, instrument, Role.REVIEWER)
    _check_version(instrument, expected_version)
    if actor.pk == instrument.created_by_id:
        raise PermissionDenied("You cannot review your own submission.")
    if action == "start":
        if instrument.status != Instrument.Status.SUBMITTED:
            raise ValidationError("Only submitted instruments can enter review.")
        changes = {"status": Instrument.Status.REVIEW}
    else:
        if instrument.status not in [Instrument.Status.SUBMITTED, Instrument.Status.REVIEW]:
            raise ValidationError("This instrument is not awaiting a review decision.")
        if not note or not note.strip():
            raise ValidationError("Record the reasons for this review decision.")
        if action == "corrections":
            changes = {"status": Instrument.Status.CORRECTIONS}
        elif action == "reject":
            changes = {"status": Instrument.Status.REJECTED, "decided_at": timezone.now()}
        elif action == "accept":
            if instrument.status != Instrument.Status.REVIEW:
                raise ValidationError("Start the review before accepting the instrument.")
            if instrument.bank_status == Instrument.BankStatus.DISCREPANT:
                raise ValidationError("Resolve the recorded bank discrepancy before acceptance.")
            if instrument.missing_documents:
                raise ValidationError("Required documents are missing.")
            _validate_duplicate(instrument)
            if instrument.predecessor_id:
                predecessor = _fresh_instrument(instrument.predecessor)
                if predecessor.status != Instrument.Status.ACCEPTED:
                    raise ValidationError("The predecessor is no longer an active accepted instrument.")
                _no_open_release(predecessor)
                _cas(predecessor, predecessor.version, status=Instrument.Status.SUPERSEDED)
                audit(actor, predecessor, "instrument.superseded", successor=str(instrument.pk))
            changes = {"status": Instrument.Status.ACCEPTED, "decided_at": timezone.now()}
        else:
            raise ValidationError("Unknown review action.")
    instrument = _cas(instrument, expected_version, **changes)
    if note and note.strip():
        Observation.objects.create(instrument=instrument, actor=actor, text=note.strip())
    audit(actor, instrument, f"review.{action}", note=(note or "").strip(), bank_status=instrument.bank_status)
    return instrument


@transaction.atomic
def record_verification(actor, instrument, *, outcome, source, method, reference, verified_on, evidence, confirmed_values, expected_version):
    instrument = _fresh_instrument(instrument)
    _authorized(actor, instrument, Role.REVIEWER)
    _check_version(instrument, expected_version)
    if instrument.status not in ["submitted", "review", "corrections", "accepted"]:
        raise ValidationError("This instrument is not open for verification.")
    if actor.pk == instrument.created_by_id:
        raise PermissionDenied("You cannot independently verify your own submission.")
    if outcome not in ["confirmed", "discrepant", "unable"]:
        raise ValidationError("Choose a valid verification outcome.")
    if not source.strip() or not method.strip() or not reference.strip():
        raise ValidationError("Provide the independent source, verification method, and bank reference.")
    if not isinstance(verified_on, date) or verified_on > timezone.localdate():
        raise ValidationError("The verification date must be today or earlier.")
    evidence = _evidence_for(instrument, evidence, Document.Category.VERIFICATION)
    if not isinstance(confirmed_values, dict):
        raise ValidationError("Confirmed values must be a JSON object.")
    if outcome == "confirmed":
        required = ["reference", "amount", "issue_date"] + (["maturity_date"] if instrument.kind == "fdr" else [])
        if any(not confirmed_values.get(key) for key in required):
            raise ValidationError("Record the reference, amount, issue date, and FDR maturity date independently confirmed by the source.")
        try:
            mismatched = str(confirmed_values["reference"]).strip() != instrument.reference or Decimal(str(confirmed_values["amount"])) != instrument.amount
            mismatched = mismatched or str(confirmed_values["issue_date"]) != instrument.issue_date.isoformat()
            if instrument.kind == "fdr":
                mismatched = mismatched or str(confirmed_values["maturity_date"]) != instrument.maturity_date.isoformat()
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ValidationError("The independently confirmed values are invalid.") from exc
        if mismatched:
            raise ValidationError("Confirmed values differ from the submitted record. Record a discrepant outcome instead.")
    verification = Verification(instrument=instrument, actor=actor, outcome=outcome, source=source.strip(), method=method.strip(), reference=reference.strip(), verified_on=verified_on, evidence=evidence, confirmed_values=confirmed_values)
    verification.full_clean()
    verification.save()
    instrument = _cas(instrument, expected_version, bank_status=outcome)
    audit(actor, instrument, "verification.recorded", verification_id=verification.pk, outcome=outcome, source=source, evidence=str(evidence.pk))
    return instrument


@transaction.atomic
def start_release(actor, instrument, *, kind, reason, amount=None):
    instrument = _fresh_instrument(instrument)
    _authorized(actor, instrument, Role.INSTITUTION)
    if instrument.status != Instrument.Status.ACCEPTED:
        raise ValidationError("Only an accepted instrument can be requested for release.")
    _no_open_release(instrument)
    if instrument.successors.filter(status__in=OPEN_SUCCESSOR_STATES).exists():
        raise ValidationError("Resolve the pending renewal or replacement before requesting release.")
    if kind not in ReleaseRequest.Kind.values or not reason.strip():
        raise ValidationError("Choose a request type and provide a reason.")
    if kind == ReleaseRequest.Kind.REFUND:
        try:
            amount = Decimal(str(amount if amount is not None else instrument.amount))
        except InvalidOperation as exc:
            raise ValidationError("Enter a valid refund amount.") from exc
        if not amount.is_finite() or amount != instrument.amount:
            raise ValidationError("A full-instrument refund must equal the recorded deposit principal.")
    elif amount is not None:
        raise ValidationError("A refund amount applies only to refund requests.")
    case = ReleaseRequest(instrument=instrument, kind=kind, reason=reason.strip(), amount=amount, policy=instrument.policy, created_by=actor)
    case.full_clean()
    _cas(instrument, instrument.version)
    case.save()
    audit(actor, instrument, "release.requested", request_id=str(case.pk), kind=kind, amount=str(amount) if amount is not None else None)
    return case


def _checked_conditions(case, conditions):
    if not isinstance(conditions, dict):
        raise ValidationError("Record the result for every policy condition.")
    required = case.policy.release_conditions
    if not required or not case.policy.reference.strip():
        raise ValidationError("This policy must define documented release conditions before a release can be recommended.")
    if any(conditions.get(condition) is not True for condition in required):
        raise ValidationError("All policy conditions must be explicitly satisfied before recommendation.")
    return {condition: True for condition in required}


def _evidence_for(instrument, evidence, category):
    if not evidence:
        raise ValidationError("Attach the required evidence document.")
    try:
        return Document.objects.get(pk=evidence.pk, instrument=instrument, category=category)
    except Document.DoesNotExist as exc:
        raise ValidationError("The evidence must have the correct category and belong to this instrument.") from exc


@transaction.atomic
def transition_release(actor, case, action, *, expected_version, note="", conditions=None, evidence=None, execution_reference="", execution_date=None):
    # All lifecycle commands lock the instrument first, avoiding inconsistent lock order.
    instrument = _fresh_instrument(case.instrument)
    case = ReleaseRequest.objects.select_for_update().select_related("policy").get(pk=case.pk)
    if not can_access(actor, instrument):
        raise PermissionDenied("This institution is outside your assigned scope.")
    _check_version(case, expected_version)
    if instrument.status != Instrument.Status.ACCEPTED:
        raise ValidationError("The instrument is no longer open for release processing.")
    if action in ["recommend", "approve", "complete"] and instrument.bank_status == Instrument.BankStatus.DISCREPANT:
        raise ValidationError("Resolve the recorded bank discrepancy before authorizing or completing release.")
    changes = {}
    if action in ["recommend", "corrections"]:
        require_role(actor, Role.REVIEWER)
        if actor.pk == case.created_by_id:
            raise PermissionDenied("You cannot scrutinize your own request.")
        if case.status not in ["submitted", "corrections"]:
            raise ValidationError("This request is not awaiting scrutiny.")
        if not note.strip():
            raise ValidationError("Record the scrutiny findings.")
        if action == "recommend":
            evidence = _evidence_for(instrument, evidence, Document.Category.RELEASE)
            changes = {"status": "recommended", "conditions": _checked_conditions(case, conditions), "reviewer": actor, "evidence": evidence}
        else:
            changes = {"status": "corrections", "reviewer": actor}
    elif action == "resubmit":
        require_role(actor, Role.INSTITUTION)
        if case.status != "corrections" or not note.strip():
            raise ValidationError("Provide a correction response for a returned request.")
        changes = {"status": "submitted", "conditions": {}}
    elif action in ["approve", "reject"]:
        require_role(actor, Role.APPROVER)
        if actor.pk in [case.created_by_id, case.reviewer_id]:
            raise PermissionDenied("Approval requires an authority distinct from the requester and reviewer.")
        if case.status != "recommended":
            raise ValidationError("Only scrutinized and recommended requests await authority approval.")
        if not note.strip():
            raise ValidationError("Record the authority's decision and reasons.")
        if action == "approve":
            _checked_conditions(case, case.conditions)
        changes = {"status": "approved" if action == "approve" else "rejected", "approver": actor}
    elif action == "complete":
        require_role(actor, Role.REVIEWER)
        if case.status != "approved":
            raise ValidationError("Execution can be recorded only after authority approval.")
        if not execution_reference.strip() or not isinstance(execution_date, date) or execution_date > timezone.localdate():
            raise ValidationError("Provide the real execution reference and a date no later than today.")
        execution_reference = ReleaseRequest._meta.get_field('execution_reference').clean(execution_reference.strip(), case)
        approval = AuditEvent.objects.filter(instrument=instrument, action="release.approve", details__request_id=str(case.pk)).order_by("created_at").first()
        if approval and execution_date < timezone.localdate(approval.created_at):
            raise ValidationError("Execution cannot precede authority approval.")
        evidence = _evidence_for(instrument, evidence, Document.Category.COMPLETION)
        changes = {"status": "completed", "execution_reference": execution_reference.strip(), "execution_date": execution_date, "evidence": evidence}
        _cas(instrument, instrument.version, status=Instrument.Status.RELEASED)
    elif action == "withdraw":
        require_role(actor, Role.INSTITUTION)
        if case.status not in ["submitted", "corrections", "recommended"]:
            raise ValidationError("Only requests awaiting approval can be withdrawn.")
        if not note.strip():
            raise ValidationError("Provide a withdrawal reason.")
        changes = {"status": "withdrawn"}
    else:
        raise ValidationError("Unknown release action.")
    if action != "complete":
        _cas(instrument, instrument.version)
    case = _cas(case, expected_version, **changes)
    audit(actor, instrument, f"release.{action}", request_id=str(case.pk), note=note.strip(), conditions=case.conditions,
          execution_reference=case.execution_reference, evidence=str(case.evidence_id) if case.evidence_id else None)
    if note.strip():
        Observation.objects.create(instrument=instrument, actor=actor, text=f"{case.get_kind_display()}: {note.strip()}")
    return case


@transaction.atomic
def withdraw_instrument(actor, instrument, expected_version):
    instrument = _fresh_instrument(instrument)
    _authorized(actor, instrument, Role.INSTITUTION)
    if instrument.status not in ["draft", "submitted", "corrections"]:
        raise ValidationError("Only drafts and submissions awaiting review can be withdrawn.")
    instrument = _cas(instrument, expected_version, status=Instrument.Status.WITHDRAWN)
    audit(actor, instrument, "instrument.withdrawn")
    return instrument
