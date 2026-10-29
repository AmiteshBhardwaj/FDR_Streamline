"""Durable deadline reminders and an explicitly acknowledged SMTP outbox."""
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.db.models import F, Q
from django.utils import timezone

from .models import Instrument, Notification, ReleaseRequest, WorkerHeartbeat


OPEN_INSTRUMENT_STATUSES = ("submitted", "review", "corrections", "accepted")
EMAIL_LEASE = timedelta(minutes=5)
MAX_EMAIL_ATTEMPTS = 6


def _recipients(institution_id, roles):
    return get_user_model().objects.filter(
        is_active=True,
        profile__institutions__pk=institution_id,
        profile__role__in=roles,
    ).distinct()


def _notify(instrument, key, title, body, roles, link=None):
    created_count = 0
    for user in _recipients(instrument.institution_id, roles):
        _, created = Notification.objects.get_or_create(
            dedupe_key=f"{key}:user:{user.pk}",
            defaults={
                "user": user,
                "instrument": instrument,
                "title": title[:200],
                "body": body,
                "link": link or f"/instruments/{instrument.pk}/",
                "email_status": "queued" if settings.SMTP_ENABLED and user.email else "disabled",
                "next_attempt_at": timezone.now() if settings.SMTP_ENABLED and user.email else None,
            },
        )
        created_count += int(created)
    return created_count


def _deadline_reminders(instrument, today):
    count = 0
    deadlines = {}
    for name in ("maturity", "validity"):
        deadline = getattr(instrument, f"{name}_date")
        if deadline:
            deadlines.setdefault(deadline, []).append(name)
    for deadline, kinds in deadlines.items():
        kind = "+".join(kinds)
        label = " and ".join(kinds)
        remaining = (deadline - today).days
        if remaining <= 0:
            period = (-remaining) // 7
            key = f"deadline:{instrument.pk}:{kind}:{deadline}:overdue:{period}"
            title = f"{instrument.reference}: {label} date reached"
            body = f"The {label} date was {deadline:%d %b %Y}. Review the outstanding instrument and record renewal or the authorized next action."
            roles = ("institution", "reviewer", "approver")
        else:
            applicable = [offset for offset in instrument.policy.reminder_days if remaining <= offset]
            if not applicable:
                continue
            threshold = min(applicable)
            key = f"deadline:{instrument.pk}:{kind}:{deadline}:before:{threshold}"
            title = f"{instrument.reference}: {label} in {remaining} days"
            body = f"The {label} date is {deadline:%d %b %Y}. Submit any required renewal or replacement and supporting documents before the deadline."
            roles = ("institution", "reviewer")
        count += _notify(instrument, key, title, body, roles)
    return count


def _review_reminders(instrument, today):
    if instrument.status not in ("submitted", "review", "corrections") or not instrument.submitted_at:
        return 0
    due = timezone.localdate(instrument.submitted_at) + timedelta(days=instrument.policy.review_sla_days)
    if today <= due:
        return 0
    period = ((today - due).days - 1) // 7
    return _notify(
        instrument,
        f"review:{instrument.pk}:{due}:{period}",
        f"{instrument.reference}: review action overdue",
        f"This case remains {instrument.get_status_display().lower()} beyond its {instrument.policy.review_sla_days}-day review target (due {due:%d %b %Y}). Follow up on outstanding verification or corrections.",
        ("institution", "reviewer", "approver"),
    )


def _release_reminders(today):
    count = 0
    cases = ReleaseRequest.objects.filter(status__in=("submitted", "corrections", "recommended", "approved")).select_related("policy", "instrument")
    for case in cases.iterator():
        due = timezone.localdate(case.updated_at) + timedelta(days=case.policy.review_sla_days)
        if today <= due:
            continue
        period = ((today - due).days - 1) // 7
        roles = ("institution", "approver") if case.status == "recommended" else ("institution", "reviewer", "approver")
        count += _notify(
            case.instrument,
            f"release:{case.pk}:{case.status}:{due}:{period}",
            f"{case.instrument.reference}: {case.get_kind_display().lower()} action overdue",
            f"The request is {case.get_status_display().lower()} and its next action was due {due:%d %b %Y}. Approval alone does not confirm execution or payment.",
            roles,
            link=f"/releases/{case.pk}/",
        )
    return count


def send_pending_email():
    """Claim each email with a lease; report server acceptance, never delivery.

    SMTP has no transactional idempotency. A worker crash after SMTP acceptance
    can cause a retry; this outbox deliberately does not claim exactly-once mail.
    """
    counts = {"email_accepted": 0, "email_failed": 0}
    if not settings.SMTP_ENABLED:
        return counts
    now = timezone.now()
    Notification.objects.filter(
        email_status="sending", next_attempt_at__lte=now, email_attempts__gte=MAX_EMAIL_ATTEMPTS
    ).update(email_status="failed", next_attempt_at=None, last_error="Mail retry limit reached after an interrupted send.")
    eligible = (
        Q(email_status__in=("queued", "failed"))
        & (Q(next_attempt_at__lte=now) | Q(next_attempt_at__isnull=True))
    ) | Q(email_status="sending", next_attempt_at__lte=now)
    candidates = Notification.objects.filter(eligible, email_attempts__lt=MAX_EMAIL_ATTEMPTS).order_by("created_at").values_list("pk", flat=True)[:100]
    for notification_id in list(candidates):
        lease = timezone.now() + EMAIL_LEASE
        claimed = Notification.objects.filter(pk=notification_id).filter(eligible).filter(email_attempts__lt=MAX_EMAIL_ATTEMPTS).update(
            email_status="sending", next_attempt_at=lease, email_attempts=F("email_attempts") + 1
        )
        if not claimed:
            continue
        notification = Notification.objects.select_related("user").get(pk=notification_id)
        # A revoked account or institution assignment must not receive queued data.
        allowed = notification.user.is_active and (
            notification.instrument_id is None
            or _recipients(notification.instrument.institution_id, ("institution", "reviewer", "approver")).filter(pk=notification.user_id).exists()
        )
        owned = Notification.objects.filter(pk=notification_id, email_status="sending", next_attempt_at=lease)
        if not allowed or not notification.user.email:
            owned.update(email_status="disabled", next_attempt_at=None, last_error="Recipient is inactive, unassigned, or has no email address.")
            continue
        try:
            # Keep confidential financial details out of email bodies. Access to
            # the record is enforced again after signing in to the portal.
            result = send_mail(
                "FDR Streamline: a portal action needs attention",
                "A case assigned to you needs attention. Sign in to FDR Streamline and open your notification inbox. This message does not confirm bank verification, execution, or payment.",
                settings.DEFAULT_FROM_EMAIL,
                [notification.user.email],
                fail_silently=False,
            )
            if result != 1:
                raise RuntimeError("Mail backend did not accept the message")
        except Exception as error:
            delay = timedelta(minutes=min(60, 2 ** notification.email_attempts))
            # Exception text from providers can contain credentials or addresses.
            owned.update(email_status="failed", next_attempt_at=timezone.now() + delay, last_error=f"{type(error).__name__}: mail submission failed; check SMTP configuration and connectivity.")
            counts["email_failed"] += 1
        else:
            owned.update(email_status="accepted", next_attempt_at=None, last_error="")
            counts["email_accepted"] += 1
    return counts


def run_monitoring(today=None):
    """One repeatable monitoring pass. Exceptions leave a visible heartbeat."""
    today = today or timezone.localdate()
    counts = {"instruments_checked": 0, "notifications_created": 0, "email_accepted": 0, "email_failed": 0}
    try:
        instruments = Instrument.objects.filter(status__in=OPEN_INSTRUMENT_STATUSES).select_related("policy")
        for instrument in instruments.iterator():
            counts["instruments_checked"] += 1
            counts["notifications_created"] += _deadline_reminders(instrument, today)
            counts["notifications_created"] += _review_reminders(instrument, today)
        counts["notifications_created"] += _release_reminders(today)
        counts.update(send_pending_email())
    except Exception as error:
        WorkerHeartbeat.objects.update_or_create(name="monitor_deadlines", defaults={"last_run": timezone.now(), "last_error": f"{type(error).__name__}: monitoring pass failed"})
        raise
    WorkerHeartbeat.objects.update_or_create(name="monitor_deadlines", defaults={"last_run": timezone.now(), "last_error": ""})
    return counts
