import base64
import io
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.utils import timezone
from PIL import Image

from portal.access import can_access, scoped_instruments
from portal.documents import read_document, upload_document
from portal.models import AuditEvent, Document, Institution, Instrument, Notification, Policy, Profile, ReleaseRequest, Role
from portal.services import create_instrument, edit_instrument, record_verification, review_instrument, start_release, submit_instrument, transition_release, withdraw_instrument


class WorkflowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.institution = Institution.objects.create(name="Test Technical College", code="TEST-001", email="test@example.test", region="North")
        cls.other = Institution.objects.create(name="Other College", code="TEST-002", email="other@example.test")
        cls.policy = Policy.objects.create(name="Illustrative test policy", reference="TEST ONLY — not an AICTE regulation", release_conditions=["No outstanding obligations", "Supporting records reviewed"])
        cls.users = {}
        for role in Role.values:
            user = get_user_model().objects.create_user(username=role, password="Testing-Only!2030", email=f"{role}@example.test")
            profile = Profile.objects.create(user=user, role=role)
            profile.institutions.add(cls.institution)
            cls.users[role] = user
        cls.owner = cls.users["institution"]
        cls.reviewer = cls.users["reviewer"]
        cls.approver = cls.users["approver"]

    def setUp(self):
        self.storage = TemporaryDirectory()
        self.addCleanup(self.storage.cleanup)
        self.settings_override = override_settings(PRIVATE_STORAGE_ROOT=self.storage.name, DOCUMENT_ENCRYPTION_KEY=base64.urlsafe_b64encode(b"t" * 32).decode())
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)

    def data(self, **overrides):
        today = timezone.localdate()
        values = dict(institution=self.institution, policy=self.policy, kind="fdr", reference="FDR-12345", bank_name="Example Scheduled Bank", ifsc="TEST0001234", amount=Decimal("1000000.00"), issue_date=today - timedelta(days=90), maturity_date=today + timedelta(days=365), validity_date=None)
        values.update(overrides)
        return values

    def image_file(self):
        buffer = io.BytesIO()
        Image.new("RGB", (8, 8), "white").save(buffer, format="PNG")
        return SimpleUploadedFile("evidence.png", buffer.getvalue(), content_type="application/octet-stream")

    def upload(self, instrument, category="instrument", user=None):
        document = upload_document(user or self.owner, instrument, self.image_file(), category)
        instrument.refresh_from_db()
        return document

    def draft(self, **data):
        return create_instrument(self.owner, self.data(**data))

    def submitted(self, **data):
        instrument = self.draft(**data)
        self.upload(instrument)
        return submit_instrument(self.owner, instrument, instrument.version)

    def accepted(self, **data):
        instrument = self.submitted(**data)
        instrument = review_instrument(self.reviewer, instrument, "start", "", instrument.version)
        return review_instrument(self.reviewer, instrument, "accept", "Administrative records are complete; independent bank status remains separate.", instrument.version)

    def test_submission_requires_documents_and_preserves_snapshot(self):
        instrument = self.draft()
        with self.assertRaises(ValidationError):
            submit_instrument(self.owner, instrument, instrument.version)
        document = self.upload(instrument)
        instrument = submit_instrument(self.owner, instrument, instrument.version)
        self.assertEqual(instrument.status, "submitted")
        self.assertEqual(instrument.bank_status, "unconfirmed")
        self.assertEqual(instrument.submissions.get().snapshot["documents"], [str(document.pk)])
        self.assertTrue(instrument.audit_events.filter(action="instrument.submitted").exists())
        with self.assertRaises(ValidationError):
            edit_instrument(self.owner, instrument, {"amount": Decimal("1.00")}, instrument.version)

    def test_scope_is_deny_by_default_and_admin_has_no_financial_override(self):
        instrument = self.draft()
        self.assertTrue(can_access(self.reviewer, instrument))
        self.assertFalse(can_access(self.users["admin"], instrument))
        self.reviewer.profile.institutions.clear()
        self.assertFalse(can_access(self.reviewer, instrument))
        self.assertFalse(scoped_instruments(self.reviewer).exists())
        with self.assertRaises(PermissionDenied):
            create_instrument(self.owner, self.data(institution=self.other))
        with self.assertRaises(PermissionDenied):
            review_instrument(self.users["admin"], instrument, "start", "", instrument.version)

    def test_invalid_dates_and_nonpositive_amounts_fail(self):
        with self.assertRaises(ValidationError):
            self.draft(amount=Decimal("0"))
        with self.assertRaises(ValidationError):
            self.draft(maturity_date=timezone.localdate() - timedelta(days=100))
        with self.assertRaises(ValidationError):
            self.draft(maturity_date=None)

    def test_temporal_status_is_independent_of_administration(self):
        instrument = self.draft(maturity_date=timezone.localdate())
        self.assertEqual(instrument.temporal_status, "matured")
        self.assertEqual(instrument.status, "draft")
        instrument.maturity_date = timezone.localdate() + timedelta(days=30)
        self.assertEqual(instrument.temporal_status, "expiring")
        instrument.validity_date = timezone.localdate() - timedelta(days=1)
        self.assertEqual(instrument.temporal_status, "expired")
        self.assertEqual(instrument.bank_status, "unconfirmed")

    def test_corrections_create_new_snapshot_and_stale_update_is_rejected(self):
        instrument = self.submitted()
        original = instrument.submissions.get().snapshot
        instrument = review_instrument(self.reviewer, instrument, "corrections", "Correct the amount.", instrument.version)
        stale_version = instrument.version
        instrument = edit_instrument(self.owner, instrument, {"amount": Decimal("2000000.00")}, instrument.version)
        with self.assertRaises(ValidationError):
            submit_instrument(self.owner, instrument, stale_version)
        instrument = submit_instrument(self.owner, instrument, instrument.version)
        self.assertEqual(instrument.submissions.count(), 2)
        self.assertEqual(instrument.submissions.get(number=1).snapshot, original)
        self.assertEqual(instrument.submissions.get(number=2).snapshot["amount"], "2000000.00")

    def test_duplicate_receipt_is_flagged_before_submission(self):
        self.submitted()
        duplicate = self.draft()
        self.upload(duplicate)
        with self.assertRaisesMessage(ValidationError, "already registered"):
            submit_instrument(self.owner, duplicate, duplicate.version)

    def test_duplicate_draft_does_not_block_and_database_enforces_receipt_identity(self):
        duplicate_draft = self.draft()
        submitted = self.submitted()
        self.assertEqual(submitted.status, "submitted")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Instrument.objects.filter(pk=duplicate_draft.pk).update(status="submitted")

    def test_lifecycle_notifications_are_scoped_and_rollback_with_stale_actions(self):
        outsider = get_user_model().objects.create_user(username="outside-reviewer")
        Profile.objects.create(user=outsider, role="reviewer").institutions.add(self.other)
        instrument = self.submitted()
        self.assertTrue(Notification.objects.filter(user=self.reviewer, instrument=instrument).exists())
        self.assertFalse(Notification.objects.filter(user=outsider).exists())
        self.assertFalse(Notification.objects.filter(user=self.users["admin"]).exists())
        before = Notification.objects.count()
        with self.assertRaises(ValidationError):
            review_instrument(self.reviewer, instrument, "corrections", "Correct", instrument.version - 1)
        self.assertEqual(Notification.objects.count(), before)
        review_instrument(self.reviewer, instrument, "corrections", "Correct", instrument.version)
        self.assertTrue(Notification.objects.filter(user=self.owner, instrument=instrument).exists())

    def test_bank_confirmation_requires_officer_evidence_and_matching_values(self):
        instrument = self.submitted()
        with self.assertRaises(PermissionDenied):
            self.upload(instrument, "verification", self.owner)
        evidence = self.upload(instrument, "verification", self.reviewer)
        values = dict(reference=instrument.reference, amount=str(instrument.amount), issue_date=str(instrument.issue_date), maturity_date=str(instrument.maturity_date))
        kwargs = dict(outcome="confirmed", source="Bank branch independently contacted", method="Official-channel response", reference="BANK-CHECK-1", verified_on=timezone.localdate(), evidence=evidence, confirmed_values=values, expected_version=instrument.version)
        with self.assertRaises(PermissionDenied):
            record_verification(self.owner, instrument, **kwargs)
        kwargs["confirmed_values"] = {**values, "amount": "1.00"}
        with self.assertRaisesMessage(ValidationError, "differ"):
            record_verification(self.reviewer, instrument, **kwargs)
        kwargs["confirmed_values"] = values
        instrument = record_verification(self.reviewer, instrument, **kwargs)
        self.assertEqual(instrument.bank_status, "confirmed")
        self.assertEqual(instrument.status, "submitted")
        instrument = review_instrument(self.reviewer, instrument, "corrections", "Update the amount.", instrument.version)
        instrument = edit_instrument(self.owner, instrument, {"amount": Decimal("999.00")}, instrument.version)
        self.assertEqual(instrument.bank_status, "unconfirmed")
        self.assertEqual(instrument.verifications.get().confirmed_values["amount"], "1000000.00")

    def test_discrepancy_blocks_acceptance(self):
        instrument = self.submitted()
        evidence = self.upload(instrument, "verification", self.reviewer)
        instrument = record_verification(self.reviewer, instrument, outcome="discrepant", source="Bank", method="Authorized response", reference="MISMATCH", verified_on=timezone.localdate(), evidence=evidence, confirmed_values={"amount": "123.00"}, expected_version=instrument.version)
        instrument = review_instrument(self.reviewer, instrument, "start", "", instrument.version)
        with self.assertRaisesMessage(ValidationError, "discrepancy"):
            review_instrument(self.reviewer, instrument, "accept", "Complete", instrument.version)

    def test_renewal_preserves_predecessor_until_approval_and_excludes_release(self):
        predecessor = self.accepted()
        successor = create_instrument(self.owner, self.data(reference=predecessor.reference, issue_date=timezone.localdate()), predecessor=predecessor)
        predecessor.refresh_from_db()
        self.assertEqual(predecessor.status, "accepted")
        with self.assertRaisesMessage(ValidationError, "pending renewal"):
            start_release(self.owner, predecessor, kind="release", reason="Close")
        self.upload(successor)
        successor = submit_instrument(self.owner, successor, successor.version)
        successor = review_instrument(self.reviewer, successor, "start", "", successor.version)
        successor = review_instrument(self.reviewer, successor, "accept", "Renewal records accepted.", successor.version)
        predecessor.refresh_from_db()
        self.assertEqual(predecessor.status, "superseded")
        self.assertEqual(successor.status, "accepted")
        self.assertEqual(successor.predecessor_id, predecessor.pk)
        self.assertEqual(predecessor.documents.count(), 1)
        self.assertEqual(predecessor.submissions.count(), 1)

    def test_rejected_renewal_retains_original_and_allows_retry(self):
        predecessor = self.accepted()
        successor = create_instrument(self.owner, self.data(reference="RENEW-1"), predecessor=predecessor)
        self.upload(successor)
        successor = submit_instrument(self.owner, successor, successor.version)
        review_instrument(self.reviewer, successor, "reject", "Invalid renewal terms.", successor.version)
        predecessor.refresh_from_db()
        self.assertEqual(predecessor.status, "accepted")
        replacement = create_instrument(self.owner, self.data(reference="RENEW-2"), predecessor=predecessor)
        self.assertEqual(replacement.status, "draft")

    def test_release_requires_distinct_authority_and_real_execution(self):
        instrument = self.accepted()
        evidence = self.upload(instrument, "release")
        case = start_release(self.owner, instrument, kind="refund", reason="Eligible closure", amount=instrument.amount)
        with self.assertRaises(ValidationError):
            transition_release(self.approver, case, "approve", expected_version=case.version, note="Approve")
        with self.assertRaises(ValidationError):
            transition_release(self.reviewer, case, "recommend", expected_version=case.version, note="Checked", conditions={}, evidence=evidence)
        case = transition_release(self.reviewer, case, "recommend", expected_version=case.version, note="All prescribed conditions evidenced", conditions={c: True for c in self.policy.release_conditions}, evidence=evidence)
        self.reviewer.profile.role = "approver"
        self.reviewer.profile.save()
        with self.assertRaises(PermissionDenied):
            transition_release(self.reviewer, case, "approve", expected_version=case.version, note="Approve my own review")
        self.reviewer.profile.role = "reviewer"
        self.reviewer.profile.save()
        case = transition_release(self.approver, case, "approve", expected_version=case.version, note="Authorized under cited policy")
        instrument.refresh_from_db()
        self.assertEqual(instrument.status, "accepted")
        self.assertEqual(case.status, "approved")
        with self.assertRaises(ValidationError):
            transition_release(self.reviewer, case, "complete", expected_version=case.version, execution_reference="", execution_date=timezone.localdate())
        completion = self.upload(instrument, "completion", self.reviewer)
        case = transition_release(self.reviewer, case, "complete", expected_version=case.version, execution_reference="EXTERNAL-PAYMENT-123", execution_date=timezone.localdate(), evidence=completion)
        instrument.refresh_from_db()
        self.assertEqual(case.status, "completed")
        self.assertEqual(instrument.status, "released")
        self.assertEqual(case.execution_reference, "EXTERNAL-PAYMENT-123")

    def test_open_release_blocks_renewal_and_duplicate_request(self):
        instrument = self.accepted()
        start_release(self.owner, instrument, kind="release", reason="Request")
        with self.assertRaises(ValidationError):
            start_release(self.owner, instrument, kind="release", reason="Duplicate")
        with self.assertRaises(ValidationError):
            create_instrument(self.owner, self.data(reference="NEW"), predecessor=instrument)

    def test_used_policy_is_immutable_but_can_be_retired(self):
        self.draft()
        policy = Policy.objects.get(pk=self.policy.pk)
        policy.review_sla_days = 30
        with self.assertRaises(ValidationError):
            policy.save()
        policy.refresh_from_db()
        policy.is_active = False
        policy.save()
        self.assertFalse(Policy.objects.get(pk=policy.pk).is_active)

    def test_documents_are_encrypted_and_tamper_detected(self):
        instrument = self.draft()
        document = self.upload(instrument)
        plaintext = read_document(document)
        self.assertTrue(plaintext.startswith(b"\x89PNG"))
        path = Path(self.storage.name) / document.encrypted_path
        ciphertext = path.read_bytes()
        self.assertNotIn(plaintext, ciphertext)
        path.write_bytes(ciphertext[:-1] + bytes([ciphertext[-1] ^ 1]))
        with self.assertRaisesMessage(ValidationError, "integrity"):
            read_document(document)
        with self.assertRaises(ValidationError):
            upload_document(self.owner, instrument, SimpleUploadedFile("fake.pdf", b"<script>alert(1)</script>", content_type="application/pdf"), "instrument")

    def test_audit_failure_rolls_back_transition_and_events_are_append_only(self):
        instrument = self.submitted()
        version = instrument.version
        with patch("portal.services.audit", side_effect=RuntimeError("Audit unavailable")):
            with self.assertRaises(RuntimeError):
                review_instrument(self.reviewer, instrument, "start", "", version)
        instrument.refresh_from_db()
        self.assertEqual(instrument.status, "submitted")
        self.assertEqual(instrument.version, version)
        event = AuditEvent.objects.first()
        with self.assertRaises(ValidationError):
            event.delete()
        with self.assertRaises(ValidationError):
            AuditEvent.objects.filter(pk=event.pk).update(action="altered")
        event.action = "altered"
        with self.assertRaises(ValidationError):
            event.save()
