"""Persistent records for deposit administration; financial events are never overwritten."""
import uuid
from datetime import date
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, RegexValidator
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone


class Role(models.TextChoices):
    INSTITUTION = "institution", "Institution operator"
    REVIEWER = "reviewer", "Regulatory reviewer"
    APPROVER = "approver", "Competent authority"
    AUDITOR = "auditor", "Read-only auditor"
    ADMIN = "admin", "System administrator"


class Institution(models.Model):
    name = models.CharField(max_length=240)
    code = models.CharField(max_length=64, unique=True)
    email = models.EmailField()
    region = models.CharField(max_length=100, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.code})"


class Profile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")
    role = models.CharField(max_length=20, choices=Role.choices)
    institutions = models.ManyToManyField(Institution, related_name="profiles", blank=True)

    def __str__(self):
        return f"{self.user} — {self.get_role_display()}"


def default_required_documents():
    return ["instrument"]


def default_reminder_days():
    return [90, 60, 30, 7, 1]


class Policy(models.Model):
    name = models.CharField(max_length=180)
    version = models.PositiveIntegerField(default=1)
    reference = models.CharField(max_length=500)
    required_documents = models.JSONField(default=default_required_documents)
    release_conditions = models.JSONField(default=list, blank=True)
    reminder_days = models.JSONField(default=default_reminder_days)
    review_sla_days = models.PositiveIntegerField(default=7)
    is_active = models.BooleanField(default=True)
    illustrative = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["name", "version"], name="policy_name_version_unique")]
        ordering = ["name", "-version"]

    def clean(self):
        if not isinstance(self.required_documents, list) or any(x not in ["instrument", "supporting"] for x in self.required_documents):
            raise ValidationError({"required_documents": "Use a list containing instrument and/or supporting."})
        if not isinstance(self.release_conditions, list) or any(not isinstance(x, str) or not x.strip() for x in self.release_conditions):
            raise ValidationError({"release_conditions": "Provide a list of non-empty condition descriptions."})
        if not isinstance(self.reminder_days, list) or not self.reminder_days or any(type(x) is not int or x < 0 or x > 3650 for x in self.reminder_days):
            raise ValidationError({"reminder_days": "Provide reminder offsets between 0 and 3650 days."})
        if self.review_sla_days < 1:
            raise ValidationError({"review_sla_days": "Review SLA must be at least one day."})

    def save(self, *args, **kwargs):
        self.full_clean()
        if self.pk and (self.instruments.exists() or self.release_requests.exists()):
            previous = type(self).objects.get(pk=self.pk)
            immutable = ["name", "version", "reference", "required_documents", "release_conditions", "reminder_days", "review_sla_days", "illustrative"]
            if any(getattr(previous, key) != getattr(self, key) for key in immutable):
                raise ValidationError("A policy already used by a case is immutable. Create a new version.")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} · v{self.version}"


class Instrument(models.Model):
    class Kind(models.TextChoices):
        FDR = "fdr", "Fixed deposit receipt"
        DEPOSIT = "deposit", "Security deposit"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SUBMITTED = "submitted", "Submitted"
        REVIEW = "review", "Under review"
        CORRECTIONS = "corrections", "Corrections requested"
        REJECTED = "rejected", "Rejected"
        ACCEPTED = "accepted", "Accepted"
        SUPERSEDED = "superseded", "Renewed / replaced"
        RELEASED = "released", "Released"
        WITHDRAWN = "withdrawn", "Withdrawn"

    class BankStatus(models.TextChoices):
        UNCONFIRMED = "unconfirmed", "Not independently confirmed"
        CONFIRMED = "confirmed", "Independently confirmed"
        DISCREPANT = "discrepant", "Discrepancy identified"
        UNABLE = "unable", "Unable to verify"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    institution = models.ForeignKey(Institution, on_delete=models.PROTECT, related_name="instruments")
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.FDR)
    reference = models.CharField(max_length=100)
    bank_name = models.CharField(max_length=180)
    ifsc = models.CharField(max_length=11, validators=[RegexValidator(r"^[A-Z]{4}0[A-Z0-9]{6}$", "Enter a valid 11-character IFSC.")])
    amount = models.DecimalField(max_digits=18, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    issue_date = models.DateField()
    maturity_date = models.DateField(null=True, blank=True)
    validity_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    bank_status = models.CharField(max_length=16, choices=BankStatus.choices, default=BankStatus.UNCONFIRMED)
    version = models.PositiveIntegerField(default=1)
    predecessor = models.ForeignKey("self", on_delete=models.PROTECT, related_name="successors", null=True, blank=True)
    policy = models.ForeignKey(Policy, on_delete=models.PROTECT, related_name="instruments")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_instruments")
    submitted_at = models.DateTimeField(null=True, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [models.Index(fields=["institution", "status"]), models.Index(fields=["maturity_date"]), models.Index(fields=["validity_date"])]
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="instrument_positive_amount"),
            models.CheckConstraint(condition=models.Q(maturity_date__isnull=True) | models.Q(maturity_date__gte=models.F("issue_date")), name="instrument_maturity_after_issue"),
            models.CheckConstraint(condition=models.Q(validity_date__isnull=True) | models.Q(validity_date__gte=models.F("issue_date")), name="instrument_validity_after_issue"),
            models.UniqueConstraint(fields=["predecessor"], condition=models.Q(status__in=["draft", "submitted", "review", "corrections", "accepted"]), name="one_live_successor_per_instrument"),
            models.UniqueConstraint(Lower("ifsc"), Lower("reference"), "issue_date", condition=models.Q(status__in=["submitted", "review", "corrections", "accepted", "superseded", "released"]), name="submitted_receipt_identity_unique"),
        ]

    def clean(self):
        self.reference = self.reference.strip()
        self.bank_name = self.bank_name.strip()
        self.ifsc = self.ifsc.strip().upper()
        if self.kind == self.Kind.FDR and not self.maturity_date:
            raise ValidationError({"maturity_date": "An FDR requires a maturity date."})
        for field in ["maturity_date", "validity_date"]:
            value = getattr(self, field)
            if value and self.issue_date and value < self.issue_date:
                raise ValidationError({field: "This date cannot precede the issue date."})
        if self.predecessor_id:
            if self.predecessor_id == self.pk or self.predecessor.institution_id != self.institution_id:
                raise ValidationError("A replacement must reference a different instrument at the same institution.")

    @property
    def temporal_status(self):
        today = timezone.localdate()
        if self.maturity_date and self.maturity_date <= today:
            return "matured"
        if self.validity_date and self.validity_date <= today:
            return "expired"
        dates = [d for d in [self.maturity_date, self.validity_date] if d]
        if not dates:
            return "no_expiry"
        threshold = max(self.policy.reminder_days or [30])
        return "expiring" if (min(dates) - today).days <= threshold else "valid"

    @property
    def days_remaining(self):
        dates = [d for d in [self.maturity_date, self.validity_date] if d]
        return (min(dates) - timezone.localdate()).days if dates else None

    @property
    def missing_documents(self):
        present = set(self.documents.values_list("category", flat=True))
        return [name for name in self.policy.required_documents if name not in present]

    def __str__(self):
        return self.reference


class Submission(models.Model):
    instrument = models.ForeignKey(Instrument, on_delete=models.PROTECT, related_name="submissions")
    number = models.PositiveIntegerField()
    snapshot = models.JSONField()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-number"]
        constraints = [models.UniqueConstraint(fields=["instrument", "number"], name="instrument_submission_unique")]


class Document(models.Model):
    class Category(models.TextChoices):
        INSTRUMENT = "instrument", "Instrument document"
        SUPPORTING = "supporting", "Supporting record"
        VERIFICATION = "verification", "Independent verification evidence"
        RELEASE = "release", "Release request evidence"
        COMPLETION = "completion", "Execution / payment evidence"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    instrument = models.ForeignKey(Instrument, on_delete=models.PROTECT, related_name="documents")
    category = models.CharField(max_length=20, choices=Category.choices)
    original_name = models.CharField(max_length=255)
    mime_type = models.CharField(max_length=80)
    size = models.PositiveIntegerField()
    checksum = models.CharField(max_length=64)
    encrypted_path = models.CharField(max_length=500)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class Observation(models.Model):
    instrument = models.ForeignKey(Instrument, on_delete=models.PROTECT, related_name="observations")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class Verification(models.Model):
    instrument = models.ForeignKey(Instrument, on_delete=models.PROTECT, related_name="verifications")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    outcome = models.CharField(max_length=16, choices=[(v.value, v.label) for v in Instrument.BankStatus if v != Instrument.BankStatus.UNCONFIRMED])
    source = models.CharField(max_length=250)
    method = models.CharField(max_length=250)
    reference = models.CharField(max_length=200)
    verified_on = models.DateField()
    evidence = models.ForeignKey(Document, on_delete=models.PROTECT, related_name="verification_records")
    confirmed_values = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class ReleaseRequest(models.Model):
    class Kind(models.TextChoices):
        RELEASE = "release", "Release"
        DISCHARGE = "discharge", "Discharge"
        REFUND = "refund", "Refund"

    class Status(models.TextChoices):
        SUBMITTED = "submitted", "Submitted"
        CORRECTIONS = "corrections", "Corrections requested"
        RECOMMENDED = "recommended", "Recommended for approval"
        APPROVED = "approved", "Approved; awaiting execution"
        REJECTED = "rejected", "Rejected"
        COMPLETED = "completed", "Completed"
        WITHDRAWN = "withdrawn", "Withdrawn"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    instrument = models.ForeignKey(Instrument, on_delete=models.PROTECT, related_name="release_requests")
    kind = models.CharField(max_length=12, choices=Kind.choices)
    reason = models.TextField()
    policy = models.ForeignKey(Policy, on_delete=models.PROTECT, related_name="release_requests")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.SUBMITTED)
    conditions = models.JSONField(default=dict, blank=True)
    reviewer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="reviewed_releases", null=True, blank=True)
    approver = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="approved_releases", null=True, blank=True)
    amount = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    execution_reference = models.CharField(max_length=250, blank=True)
    execution_date = models.DateField(null=True, blank=True)
    evidence = models.ForeignKey(Document, on_delete=models.PROTECT, null=True, blank=True, related_name="release_executions")
    version = models.PositiveIntegerField(default=1)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="requested_releases")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(fields=["instrument"], condition=models.Q(status__in=["submitted", "corrections", "recommended", "approved"]), name="one_open_release_per_instrument")]


class AuditQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("Audit events are append-only.")

    def delete(self):
        raise ValidationError("Audit events cannot be deleted.")


class AuditEvent(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True)
    institution = models.ForeignKey(Institution, on_delete=models.PROTECT, null=True, blank=True)
    instrument = models.ForeignKey(Instrument, on_delete=models.PROTECT, null=True, blank=True, related_name="audit_events")
    action = models.CharField(max_length=100)
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    objects = AuditQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at", "-pk"]

    def save(self, *args, **kwargs):
        if self.pk is not None and type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError("Audit events are append-only.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Audit events cannot be deleted.")


class Notification(models.Model):
    class EmailStatus(models.TextChoices):
        DISABLED = "disabled", "Email disabled"
        QUEUED = "queued", "Queued"
        SENDING = "sending", "Sending"
        ACCEPTED = "accepted", "Accepted by mail server"
        FAILED = "failed", "Failed"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    instrument = models.ForeignKey(Instrument, on_delete=models.PROTECT, null=True, blank=True)
    title = models.CharField(max_length=200)
    body = models.TextField()
    link = models.CharField(max_length=500, blank=True)
    dedupe_key = models.CharField(max_length=250, unique=True)
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    email_status = models.CharField(max_length=12, choices=EmailStatus.choices, default=EmailStatus.DISABLED)
    email_attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["-created_at"]


class WorkerHeartbeat(models.Model):
    name = models.CharField(max_length=80, unique=True)
    last_run = models.DateTimeField()
    last_error = models.CharField(max_length=500, blank=True)


class LoginAttempt(models.Model):
    key = models.CharField(max_length=200, unique=True)
    count = models.PositiveIntegerField(default=0)
    window_start = models.DateTimeField()
    blocked_until = models.DateTimeField(null=True, blank=True)
