"""Explicit, disposable, synthetic demonstration data; never production bootstrap."""
import io
import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from portal.documents import upload_document
from portal.models import AuditEvent, Document, Institution, Instrument, Policy, Profile, ReleaseRequest
from portal.services import create_instrument, record_verification, review_instrument, start_release, submit_instrument, transition_release


def synthetic_pdf(reference, purpose):
    writer = PdfWriter()
    page = writer.add_blank_page(width=595, height=842)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
    lines = ["SYNTHETIC DEMO - NO FINANCIAL VALUE", "FDR Streamline / SIH 26265", reference, purpose, "Not issued or verified by a real bank.", "Illustrates the portal workflow only."]
    commands = ["BT /F1 16 Tf 45 775 Td"]
    for index, line in enumerate(lines):
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        if index:
            commands.append("0 -36 Td")
        commands.append(f"({escaped}) Tj")
    commands.append("ET")
    stream = DecodedStreamObject()
    stream.set_data("\n".join(commands).encode("ascii", errors="replace"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    writer.add_metadata({"/Title": "SYNTHETIC DEMO", "/Subject": purpose})
    result = io.BytesIO()
    writer.write(result)
    return result.getvalue()


class Command(BaseCommand):
    help = "Seed an empty development database with explicitly synthetic demo records."

    def add_arguments(self, parser):
        parser.add_argument("--password", default="DemoPass!26265", help="Password for newly created demo users only (minimum 12 characters).")

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("Demo seeding is disabled when DJANGO_DEBUG=false.")
        if len(options["password"]) < 12:
            raise CommandError("The demo password must contain at least 12 characters.")
        marker = Path(settings.LOCAL_ROOT) / "demo-mode"
        populated = any(model.objects.exists() for model in (get_user_model(), Institution, Instrument, Policy, Document, AuditEvent))
        if populated:
            if marker.is_file():
                self.stdout.write("Demo data already exists; records, credentials, and user edits were left unchanged.")
                return
            raise CommandError("Refusing to seed a nonempty database without its explicit demo marker. Use a separate empty development database.")
        self.created_files = []
        try:
            with transaction.atomic():
                self._seed(options["password"])
        except Exception:
            for path in self.created_files:
                path.unlink(missing_ok=True)
            raise
        marker.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        marker.write_text(json.dumps({"synthetic": True, "seed_version": 1, "database": str(settings.DATABASES["default"]["NAME"])}))
        marker.chmod(0o600)
        self.stdout.write(self.style.SUCCESS("Created 15 synthetic instruments across 2 institutions. Demo usernames: institution, institution2, reviewer, approver, auditor, admin."))
        self.stdout.write("All PDFs, bank confirmations, and execution records are labeled synthetic. No external service was called.")

    def _upload(self, actor, instrument, category):
        document = upload_document(actor, instrument, SimpleUploadedFile(f"SYNTHETIC-{category}-{instrument.reference}.pdf", synthetic_pdf(instrument.reference, category), content_type="application/pdf"), category)
        self.created_files.append(Path(settings.PRIVATE_STORAGE_ROOT) / document.encrypted_path)
        instrument.refresh_from_db()
        return document

    def _record(self, reference, days, *, state="accepted", amount="1000000.00", institution=None, actor=None, confirmed=False, discrepancy=False, validity_days=None, predecessor=None, kind="fdr", age=10):
        actor = actor or self.users["institution"]
        institution = institution or self.institutions[0]
        instrument = create_instrument(actor, {
            "institution": institution, "policy": self.policy, "kind": kind, "reference": reference,
            "bank_name": "Demonstration Bank (synthetic)", "ifsc": "DEMO0000001", "amount": Decimal(amount),
            "issue_date": self.today - timedelta(days=365),
            "maturity_date": self.today + timedelta(days=days) if days is not None else None,
            "validity_date": self.today + timedelta(days=validity_days) if validity_days is not None else None,
        }, predecessor=predecessor)
        if state == "draft":
            return instrument
        self._upload(actor, instrument, "instrument")
        instrument = submit_instrument(actor, instrument, instrument.version)
        if state != "submitted":
            instrument = review_instrument(self.users["reviewer"], instrument, "start", "Synthetic example: scrutiny opened.", instrument.version)
        if confirmed or discrepancy:
            evidence = self._upload(self.users["reviewer"], instrument, "verification")
            values = {"reference": instrument.reference, "amount": str(instrument.amount), "issue_date": instrument.issue_date.isoformat()}
            if instrument.maturity_date:
                values["maturity_date"] = instrument.maturity_date.isoformat()
            if discrepancy:
                values["amount"] = "999.00"
            instrument = record_verification(
                self.users["reviewer"], instrument, outcome="discrepant" if discrepancy else "confirmed",
                source="SYNTHETIC DEMO bank evidence; no live bank contacted", method="Synthetic manual evidence workflow",
                reference=f"DEMO-EVIDENCE-{reference}", verified_on=self.today, evidence=evidence,
                confirmed_values=values, expected_version=instrument.version,
            )
        if state == "corrections":
            instrument = review_instrument(self.users["reviewer"], instrument, "corrections", "Synthetic discrepancy: deposit amount differs from supporting evidence. Submit a correction.", instrument.version)
        elif state == "accepted":
            instrument = review_instrument(self.users["reviewer"], instrument, "accept", "Synthetic administrative acceptance. Bank confirmation status remains explicitly separate.", instrument.version)
        submitted = timezone.now() - timedelta(days=age)
        updates = {"submitted_at": submitted, "created_at": submitted - timedelta(hours=2)}
        if state == "accepted":
            updates["decided_at"] = submitted + timedelta(hours=8 + age)
        Instrument.objects.filter(pk=instrument.pk).update(**updates)
        instrument.refresh_from_db()
        return instrument

    def _release(self, instrument, *, complete=False):
        case = start_release(self.users["institution"], instrument, kind="refund", reason="SYNTHETIC DEMO: documented conditions fulfilled; demonstrate approval before execution.")
        instrument.refresh_from_db()
        evidence = self._upload(self.users["institution"], instrument, "release")
        case = transition_release(self.users["reviewer"], case, "recommend", expected_version=case.version, note="SYNTHETIC DEMO: checklist and supporting evidence reviewed.", conditions={condition: True for condition in self.policy.release_conditions}, evidence=evidence)
        case = transition_release(self.users["approver"], case, "approve", expected_version=case.version, note="SYNTHETIC DEMO competent authority decision. No real payment authorized.")
        if complete:
            instrument.refresh_from_db()
            evidence = self._upload(self.users["reviewer"], instrument, "completion")
            transition_release(self.users["reviewer"], case, "complete", expected_version=case.version, note="SYNTHETIC DEMO execution only.", evidence=evidence, execution_reference="SYNTHETIC-EXECUTION-NOT-A-PAYMENT", execution_date=self.today)
        else:
            ReleaseRequest.objects.filter(pk=case.pk).update(updated_at=timezone.now() - timedelta(days=9))

    def _seed(self, password):
        self.today = timezone.localdate()
        self.institutions = [
            Institution.objects.create(name="Sahyadri Institute of Technology (Demo)", code="DEMO-AICTE-001", email="office@sahyadri.example.test", region="Western Region"),
            Institution.objects.create(name="Eastern Technical College (Demo)", code="DEMO-AICTE-002", email="office@eastern.example.test", region="Eastern Region"),
        ]
        self.policy = Policy.objects.create(name="Illustrative deposit administration policy", version=1, reference="SYNTHETIC DEMO ONLY. Replace with an authorized, applicable AICTE policy before real use.", release_conditions=["Applicable approval conditions have been fulfilled", "No outstanding regulatory claim remains", "Supporting release evidence has been reviewed"], illustrative=True)
        self.users = {}
        for username, role in [("institution", "institution"), ("institution2", "institution"), ("reviewer", "reviewer"), ("approver", "approver"), ("auditor", "auditor"), ("admin", "admin")]:
            user = get_user_model().objects.create_user(username=username, password=password, email=f"{username}@portal.example.test", first_name={"institution": "Institution", "institution2": "Eastern Institution", "reviewer": "Regulatory Reviewer", "approver": "Competent Authority", "auditor": "Audit Officer", "admin": "System Administrator"}[username], is_staff=role == "admin")
            profile = Profile.objects.create(user=user, role=role)
            if role == "institution":
                profile.institutions.add(self.institutions[1 if username == "institution2" else 0])
            elif role != "admin":
                profile.institutions.add(*self.institutions)
            self.users[username] = user
        self._record("DEMO-DRAFT-001", 365, state="draft")
        self._record("DEMO-PENDING-002", 120, state="submitted", age=3)
        self._record("DEMO-REVIEW-003", 75, state="review", age=12)
        self._record("DEMO-CORRECTION-004", 120, state="corrections", discrepancy=True, age=14)
        self._record("DEMO-EXPIRING-005", 7, amount="1500000.00", confirmed=True)
        self._record("DEMO-EXPIRING-006", 30, amount="2500000.00")
        self._record("DEMO-MATURED-007", -10, amount="1800000.00")
        self._record("DEMO-ACTIVE-008", 180, amount="5000000.00", confirmed=True)
        self._record("DEMO-VALIDITY-009", 180, validity_days=-2, amount="750000.00")
        self._record("DEMO-DEPOSIT-010", None, kind="deposit", amount="500000.00")
        self._release(self._record("DEMO-RELEASED-011", 180, amount="1000000.00"), complete=True)
        self._release(self._record("DEMO-APPROVED-012", 180, amount="1200000.00"))
        original = self._record("DEMO-ORIGINAL-013", -5, amount="2000000.00")
        self._record("DEMO-RENEWED-014", 365, amount="2000000.00", predecessor=original, confirmed=True)
        self._record("DEMO-EASTERN-015", 45, state="submitted", institution=self.institutions[1], actor=self.users["institution2"], amount="3000000.00", age=2)
