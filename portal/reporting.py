"""Scoped, reproducible portfolio reports shared by screens and exports."""
from datetime import timedelta
from decimal import Decimal

from django.db.models import Case, DateField, F, Q, Sum, When
from django.utils import timezone

from .models import Document, Instrument, Policy


PENDING_STATUSES = ("submitted", "review", "corrections")
OPEN_STATUSES = (*PENDING_STATUSES, "accepted")


def _dated(queryset):
    return queryset.annotate(
        _deadline=Case(
            When(maturity_date__isnull=True, then=F("validity_date")),
            When(validity_date__isnull=True, then=F("maturity_date")),
            When(maturity_date__lte=F("validity_date"), then=F("maturity_date")),
            default=F("validity_date"),
            output_field=DateField(),
        )
    )


def _expiring(queryset, today):
    clauses = Q(pk__in=[])
    policies = Policy.objects.filter(instruments__in=queryset).values_list("pk", "reminder_days").distinct()
    for policy_id, offsets in policies:
        clauses |= Q(policy_id=policy_id, _deadline__lte=today + timedelta(days=max(offsets or [30])))
    return queryset.filter(_deadline__gt=today).filter(clauses)


def _missing_ids(queryset):
    candidates = queryset.filter(status__in=("draft", *OPEN_STATUSES))
    present = {}
    for instrument_id, category in Document.objects.filter(instrument__in=candidates).values_list("instrument_id", "category"):
        present.setdefault(instrument_id, set()).add(category)
    return [
        instrument_id
        for instrument_id, required in candidates.values_list("pk", "policy__required_documents")
        if set(required) - present.get(instrument_id, set())
    ]


def _overdue_ids(queryset, today):
    overdue = []
    for record in queryset.filter(status__in=OPEN_STATUSES).values(
        "pk", "status", "submitted_at", "policy__review_sla_days", "maturity_date", "validity_date"
    ):
        dates = [record[name] for name in ("maturity_date", "validity_date") if record[name]]
        expired = record["status"] == "accepted" and dates and min(dates) <= today
        submitted = record["submitted_at"]
        review_overdue = (
            record["status"] in PENDING_STATUSES
            and submitted
            and timezone.localdate(submitted) + timedelta(days=record["policy__review_sla_days"]) < today
        )
        if expired or review_overdue:
            overdue.append(record["pk"])
    return overdue


def filtered_instruments(queryset, params, today=None):
    """Filter an already authorized queryset; never expand its institution scope."""
    today = today or timezone.localdate()
    queryset = _dated(queryset)
    search = str(params.get("q", "")).strip()[:200]
    if search:
        queryset = queryset.filter(
            Q(reference__icontains=search)
            | Q(bank_name__icontains=search)
            | Q(ifsc__icontains=search)
            | Q(institution__name__icontains=search)
            | Q(institution__code__icontains=search)
        )
    status = params.get("status")
    if status in Instrument.Status.values:
        queryset = queryset.filter(status=status)
    temporal = params.get("temporal")
    if temporal == "matured":
        queryset = queryset.filter(maturity_date__lte=today)
    elif temporal == "expired":
        queryset = queryset.filter(validity_date__lte=today).exclude(maturity_date__lte=today)
    elif temporal == "expiring":
        queryset = _expiring(queryset, today)
    elif temporal == "valid":
        dated = queryset.filter(_deadline__gt=today)
        queryset = dated.exclude(pk__in=_expiring(dated, today).values("pk"))
    elif temporal == "no_expiry":
        queryset = queryset.filter(_deadline__isnull=True)
    report = params.get("report")
    if report == "pending":
        queryset = queryset.filter(status__in=PENDING_STATUSES)
    elif report == "expiring":
        queryset = _expiring(queryset.filter(status="accepted"), today)
    elif report == "missing":
        queryset = queryset.filter(pk__in=_missing_ids(queryset))
    elif report == "discrepant":
        queryset = queryset.filter(bank_status="discrepant")
    elif report == "overdue":
        queryset = queryset.filter(pk__in=_overdue_ids(queryset, today))
    elif report == "renewed":
        queryset = queryset.filter(status="accepted", predecessor__isnull=False)
    elif report == "released":
        queryset = queryset.filter(status="released")
    elif report == "active":
        queryset = queryset.filter(status="accepted").filter(Q(_deadline__gt=today) | Q(_deadline__isnull=True))
    elif report == "matured":
        queryset = queryset.filter(status="accepted", maturity_date__lte=today)
    elif report == "expired":
        queryset = queryset.filter(status="accepted", validity_date__lte=today).exclude(maturity_date__lte=today)
    return queryset


def metrics_for(queryset, today=None):
    """Return actual scoped counts; expiry and maturity are deliberately distinct."""
    today = today or timezone.localdate()
    dated = _dated(queryset)
    accepted = dated.filter(status="accepted")
    active = accepted.filter(Q(_deadline__gt=today) | Q(_deadline__isnull=True))
    durations = [
        (decided - submitted).total_seconds() / 3600
        for submitted, decided in queryset.filter(submitted_at__isnull=False, decided_at__isnull=False).values_list("submitted_at", "decided_at")
        if decided >= submitted
    ]
    return {
        "active": active.count(),
        "expiring": _expiring(active, today).count(),
        "matured": accepted.filter(maturity_date__lte=today).count(),
        "expired": accepted.filter(validity_date__lte=today).exclude(maturity_date__lte=today).count(),
        "pending": queryset.filter(status__in=PENDING_STATUSES).count(),
        "renewed": accepted.filter(predecessor__isnull=False).count(),
        "released": queryset.filter(status="released").count(),
        "portfolio_amount": accepted.aggregate(total=Sum("amount"))["total"] or Decimal("0.00"),
        "overdue": len(_overdue_ids(queryset, today)),
        "missing": len(_missing_ids(queryset)),
        "discrepant": queryset.filter(bank_status="discrepant").count(),
        "avg_turnaround": round(sum(durations) / len(durations), 2) if durations else None,
    }
