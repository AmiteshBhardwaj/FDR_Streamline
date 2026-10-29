"""Proof that local delivery and recovery preserve real records and evidence."""
import io
import json
import sqlite3
import stat
import tempfile
import zipfile
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from portal.documents import read_document, upload_document
from portal.management.commands.seed_demo import synthetic_pdf
from portal.models import Document, Institution, Instrument, Policy, Profile, ReleaseRequest
from portal.services import create_instrument


class OperationsTests(TransactionTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="fdr-ops-test-")
        self.root = Path(self.temporary.name)
        self.local = self.root / "live"
        self.local.mkdir()
        self.overrides = override_settings(DEBUG=True, LOCAL_ROOT=self.local, PRIVATE_STORAGE_ROOT=self.local / "documents")
        self.overrides.enable()
        self.addCleanup(self.overrides.disable)
        self.addCleanup(self.temporary.cleanup)

    def command(self, name, *args, **options):
        output = io.StringIO()
        call_command(name, *args, stdout=output, stderr=output, **options)
        return output.getvalue()

    def record_with_document(self):
        institution = Institution.objects.create(name="Recovery Test Institute", code="RECOVERY", email="office@example.test")
        owner = get_user_model().objects.create_user(username="recovery-operator")
        Profile.objects.create(user=owner, role="institution").institutions.add(institution)
        policy = Policy.objects.create(name="Recovery test policy", reference="TEST ONLY", release_conditions=["Evidence reviewed"])
        today = timezone.localdate()
        instrument = create_instrument(owner, dict(institution=institution, policy=policy, kind="fdr", reference="RECOVERY-FDR", bank_name="Synthetic bank", ifsc="TEST0000001", amount=Decimal("500000.00"), issue_date=today, maturity_date=today + timedelta(days=365), validity_date=None))
        content = synthetic_pdf(instrument.reference, "Backup and restore acceptance test")
        document = upload_document(owner, instrument, SimpleUploadedFile("recovery.pdf", content, content_type="application/pdf"), "instrument")
        return instrument, document, content

    def test_explicit_seed_creates_complete_demo_and_preserves_edits_on_rerun(self):
        self.command("seed_demo")
        self.assertEqual(Instrument.objects.count(), 15)
        self.assertEqual(Institution.objects.count(), 2)
        self.assertEqual(Profile.objects.count(), 6)
        self.assertTrue(ReleaseRequest.objects.filter(status="approved", instrument__status="accepted").exists())
        self.assertTrue(ReleaseRequest.objects.filter(status="completed", instrument__status="released").exists())
        self.assertTrue(Instrument.objects.filter(status="accepted", predecessor__status="superseded").exists())
        self.assertTrue(Instrument.objects.filter(bank_status="discrepant", status="corrections").exists())
        document = Document.objects.first()
        self.assertTrue(read_document(document).startswith(b"%PDF-"))
        from pypdf import PdfReader
        text = PdfReader(io.BytesIO(read_document(document))).pages[0].extract_text()
        self.assertIn("SYNTHETIC DEMO", text)
        owner = get_user_model().objects.get(username="institution")
        self.assertTrue(owner.check_password("DemoPass!26265"))
        institution = owner.profile.institutions.get()
        institution.name = "A retained demonstration edit"
        institution.save()
        self.command("seed_demo", password="DifferentPass!26265")
        owner.refresh_from_db(); institution.refresh_from_db()
        self.assertEqual(Instrument.objects.count(), 15)
        self.assertEqual(institution.name, "A retained demonstration edit")
        self.assertTrue(owner.check_password("DemoPass!26265"))

    def test_seed_refuses_production_and_nonempty_unmarked_database(self):
        with override_settings(DEBUG=False), self.assertRaises(CommandError):
            self.command("seed_demo")
        self.assertFalse(get_user_model().objects.exists())
        get_user_model().objects.create_user(username="real-account")
        with self.assertRaisesMessage(CommandError, "nonempty database"):
            self.command("seed_demo")
        self.assertEqual(get_user_model().objects.count(), 1)
        self.assertFalse(Instrument.objects.exists())

    def test_backup_restore_recovers_database_and_decryptable_evidence_without_keys(self):
        instrument, document, content = self.record_with_document()
        archive = self.root / "backup.zip"
        self.command("backup_portal", output=str(archive))
        with zipfile.ZipFile(archive) as bundle:
            self.assertNotIn("secrets.json", bundle.namelist())
            manifest = json.loads(bundle.read("manifest.json"))
            self.assertFalse(manifest["includes_development_keys"])
            self.assertIn("documents/" + document.encrypted_path, manifest["files"])
        destination = self.root / "recovered"
        self.command("restore_portal", str(archive), destination=str(destination))
        with sqlite3.connect(destination / "portal.sqlite3") as database:
            self.assertEqual(database.execute("SELECT reference FROM portal_instrument").fetchone()[0], instrument.reference)
            self.assertEqual(database.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        with override_settings(PRIVATE_STORAGE_ROOT=destination / "documents"):
            self.assertEqual(read_document(document), content)
        self.assertEqual(Instrument.objects.count(), 1)
        self.assertEqual(read_document(document), content)
        self.assertEqual(stat.S_IMODE(archive.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((destination / "portal.sqlite3").stat().st_mode), 0o600)

    def test_backup_never_overwrites_and_missing_evidence_aborts(self):
        _, document, _ = self.record_with_document()
        archive = self.root / "backup.zip"
        archive.write_text("existing archive must survive")
        with self.assertRaisesMessage(CommandError, "already exists"):
            self.command("backup_portal", output=str(archive))
        self.assertEqual(archive.read_text(), "existing archive must survive")
        (Path(settings.PRIVATE_STORAGE_ROOT) / document.encrypted_path).unlink()
        missing_archive = self.root / "incomplete.zip"
        with self.assertRaisesMessage(CommandError, "missing"):
            self.command("backup_portal", output=str(missing_archive))
        self.assertFalse(missing_archive.exists())

    def test_restore_rejects_tampering_and_preserves_current_data(self):
        _, document, content = self.record_with_document()
        archive = self.root / "backup.zip"
        self.command("backup_portal", output=str(archive))
        tampered = self.root / "tampered.zip"
        with zipfile.ZipFile(archive) as source, zipfile.ZipFile(tampered, "w") as target:
            for member in source.infolist():
                data = source.read(member.filename)
                if member.filename.startswith("documents/"):
                    data = bytes([data[0] ^ 1]) + data[1:]
                target.writestr(member, data)
        destination = self.root / "tampered-restore"
        with self.assertRaisesMessage(CommandError, "integrity check"):
            self.command("restore_portal", str(tampered), destination=str(destination))
        self.assertFalse(destination.exists())
        self.assertEqual(read_document(document), content)

    def test_restore_rejects_traversal_symlinks_and_nonempty_destination(self):
        self.record_with_document()
        archive = self.root / "backup.zip"
        self.command("backup_portal", output=str(archive))
        destination = self.root / "occupied"
        destination.mkdir(); (destination / "keep.txt").write_text("preserve")
        with self.assertRaisesMessage(CommandError, "new or empty"):
            self.command("restore_portal", str(archive), destination=str(destination))
        self.assertEqual((destination / "keep.txt").read_text(), "preserve")
        for filename, symlink in [("../escape.txt", False), ("documents/link", True)]:
            unsafe = self.root / ("symlink.zip" if symlink else "traversal.zip")
            with zipfile.ZipFile(unsafe, "w") as bundle:
                bundle.writestr("manifest.json", "{}")
                member = zipfile.ZipInfo(filename)
                if symlink:
                    member.create_system = 3
                    member.external_attr = (stat.S_IFLNK | 0o777) << 16
                bundle.writestr(member, "/etc/passwd")
            with self.assertRaisesMessage(CommandError, "unsafe path"):
                self.command("restore_portal", str(unsafe), destination=str(self.root / "unsafe-restore"))
        self.assertFalse((self.root / "escape.txt").exists())

    def test_key_inclusion_requires_explicit_development_flag(self):
        self.record_with_document()
        archive = self.root / "with-keys.zip"
        self.command("backup_portal", output=str(archive), include_dev_keys=True)
        with zipfile.ZipFile(archive) as bundle:
            self.assertEqual(json.loads(bundle.read("secrets.json"))["document_key"], settings.DOCUMENT_ENCRYPTION_KEY)
        with override_settings(DEBUG=False), self.assertRaisesMessage(CommandError, "DEBUG development"):
            self.command("backup_portal", output=str(self.root / "prod.zip"), include_dev_keys=True)
