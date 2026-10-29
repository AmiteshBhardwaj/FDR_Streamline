from datetime import date, datetime, time, timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from portal.models import Document, Institution, Instrument, Notification, Policy, Profile, ReleaseRequest, WorkerHeartbeat
from portal.monitoring import MAX_EMAIL_ATTEMPTS, run_monitoring, send_pending_email
from portal.reporting import filtered_instruments, metrics_for


@override_settings(SMTP_ENABLED=False)
class MonitoringTests(TestCase):
    today = date(2026, 10, 29)

    def setUp(self):
        self.institution = Institution.objects.create(name="Engineering Institute", code="I001", email="office@example.test")
        self.other = Institution.objects.create(name="Other Institute", code="I002", email="other@example.test")
        self.policy = Policy.objects.create(
            name="Illustrative policy", reference="Demonstration only", release_conditions=["Authorized decision recorded"],
            reminder_days=[90, 60, 30, 7, 1], review_sla_days=7,
        )
        self.operator = self.user("operator", "institution", self.institution)
        self.reviewer = self.user("reviewer", "reviewer", self.institution)
        self.approver = self.user("approver", "approver", self.institution)
        self.admin = self.user("administrator", "admin", self.institution)
        self.auditor = self.user("auditor", "auditor", self.institution)
        self.outsider = self.user("outsider", "reviewer", self.other)
        self.unassigned = self.user("unassigned", "reviewer")

    def user(self, username, role, institution=None):
        user = get_user_model().objects.create_user(username=username, email=f"{username}@example.test")
        profile = Profile.objects.create(user=user, role=role)
        if institution:
            profile.institutions.add(institution)
        return user

    def instrument(self, **overrides):
        values = {
            "institution": self.institution, "reference": f"FDR-{Instrument.objects.count() + 1}",
            "bank_name": "Test Bank", "ifsc": "TEST0000001", "amount": Decimal("100000.00"),
            "issue_date": self.today - timedelta(days=365), "maturity_date": self.today + timedelta(days=29),
            "status": "accepted", "policy": self.policy, "created_by": self.operator,
        }
        values.update(overrides)
        return Instrument.objects.create(**values)

    def instant(self, day):
        return timezone.make_aware(datetime.combine(day, time(12)))

    def document(self, instrument, category="instrument"):
        return Document.objects.create(
            instrument=instrument, category=category, original_name="evidence.pdf", mime_type="application/pdf",
            size=100, checksum="a" * 64, encrypted_path="private/test.enc", uploaded_by=self.operator,
        )

    def test_catchup_sends_only_most_urgent_threshold_to_scoped_users(self):
        self.instrument()
        result = run_monitoring(self.today)
        self.assertEqual(result["notifications_created"], 2)
        self.assertSetEqual(set(Notification.objects.values_list("user_id", flat=True)), {self.operator.pk, self.reviewer.pk})
        self.assertTrue(all(":before:30:" in key for key in Notification.objects.values_list("dedupe_key", flat=True)))
        self.assertEqual(run_monitoring(self.today)["notifications_created"], 0)
        self.assertTrue(WorkerHeartbeat.objects.filter(name="monitor_deadlines", last_error="").exists())
        self.assertFalse(Notification.objects.exclude(email_status="disabled").exists())

    def test_advance_to_next_threshold_and_weekly_overdue_are_idempotent(self):
        instrument = self.instrument(maturity_date=self.today + timedelta(days=7))
        self.assertEqual(run_monitoring(self.today)["notifications_created"], 2)
        self.assertEqual(run_monitoring(self.today + timedelta(days=6))["notifications_created"], 2)
        maturity = instrument.maturity_date
        self.assertEqual(run_monitoring(maturity)["notifications_created"], 3)
        self.assertEqual(run_monitoring(maturity + timedelta(days=6))["notifications_created"], 0)
        self.assertEqual(run_monitoring(maturity + timedelta(days=7))["notifications_created"], 3)

    def test_validity_and_maturity_are_distinct_and_equal_dates_combine(self):
        instrument = self.instrument(validity_date=self.today - timedelta(days=1))
        run_monitoring(self.today)
        keys = list(Notification.objects.values_list("dedupe_key", flat=True))
        self.assertEqual(len(keys), 5)
        self.assertTrue(any(":validity:" in key and ":overdue:" in key for key in keys))
        self.assertTrue(any(":maturity:" in key and ":before:" in key for key in keys))
        instrument.validity_date = instrument.maturity_date
        instrument.save(update_fields=["validity_date"])
        self.assertEqual(run_monitoring(self.today)["notifications_created"], 2)
        self.assertEqual(Notification.objects.filter(dedupe_key__contains=":maturity+validity:").count(), 2)

    def test_terminal_and_draft_records_do_not_generate_deadline_alerts(self):
        for status in ("draft", "withdrawn", "rejected", "superseded", "released"):
            self.instrument(status=status, maturity_date=self.today)
        self.assertEqual(run_monitoring(self.today)["notifications_created"], 0)

    def test_review_overdue_uses_submission_date_and_policy_sla(self):
        self.instrument(status="submitted", maturity_date=self.today + timedelta(days=180), submitted_at=self.instant(self.today - timedelta(days=7)))
        self.assertEqual(run_monitoring(self.today)["notifications_created"], 0)
        self.assertEqual(run_monitoring(self.today + timedelta(days=1))["notifications_created"], 3)
        self.assertEqual(run_monitoring(self.today + timedelta(days=2))["notifications_created"], 0)
        self.assertEqual(run_monitoring(self.today + timedelta(days=8))["notifications_created"], 3)

    def test_release_approval_queue_alerts_authority_and_institution(self):
        instrument = self.instrument(maturity_date=self.today + timedelta(days=180))
        case = ReleaseRequest.objects.create(instrument=instrument, kind="refund", reason="Conditions fulfilled", policy=self.policy, status="recommended", created_by=self.operator)
        ReleaseRequest.objects.filter(pk=case.pk).update(updated_at=self.instant(self.today - timedelta(days=8)))
        result = run_monitoring(self.today)
        self.assertEqual(result["notifications_created"], 2)
        self.assertSetEqual(set(Notification.objects.values_list("user_id", flat=True)), {self.operator.pk, self.approver.pk})
        self.assertEqual(run_monitoring(self.today)["notifications_created"], 0)
        self.assertTrue(all(n.link == f"/releases/{case.pk}/" for n in Notification.objects.all()))

    @override_settings(SMTP_ENABLED=True)
    @patch("portal.monitoring.send_mail", return_value=1)
    def test_outbox_acceptance_is_persisted_and_not_resent(self, send):
        self.instrument()
        result = run_monitoring(self.today)
        self.assertEqual(result["email_accepted"], 2)
        self.assertEqual(Notification.objects.filter(email_status="accepted").count(), 2)
        self.assertEqual(send.call_count, 2)
        run_monitoring(self.today)
        self.assertEqual(send.call_count, 2)
        self.assertNotIn("FDR-1", send.call_args.args[1])

    @override_settings(SMTP_ENABLED=True)
    @patch("portal.monitoring.send_mail", side_effect=OSError("sensitive-provider-value"))
    def test_email_failure_retries_only_after_backoff_without_exposing_details(self, send):
        self.instrument()
        result = run_monitoring(self.today)
        self.assertEqual(result["email_failed"], 2)
        notification = Notification.objects.first()
        self.assertEqual(notification.email_status, "failed")
        self.assertNotIn("sensitive-provider-value", notification.last_error)
        send_pending_email()
        self.assertEqual(send.call_count, 2)
        Notification.objects.update(next_attempt_at=timezone.now() - timedelta(seconds=1))
        with patch("portal.monitoring.send_mail", return_value=1):
            self.assertEqual(send_pending_email()["email_accepted"], 2)
        notification.refresh_from_db()
        self.assertEqual(notification.email_attempts, 2)

    @override_settings(SMTP_ENABLED=True)
    @patch("portal.monitoring.send_mail", return_value=1)
    def test_stale_email_lease_recovers_but_exhausted_lease_stops(self, send):
        instrument = self.instrument()
        for user, attempts in ((self.operator, 1), (self.reviewer, MAX_EMAIL_ATTEMPTS)):
            Notification.objects.create(user=user, instrument=instrument, title="Reminder", body="Review", dedupe_key=f"lease-{user.pk}", email_status="sending", email_attempts=attempts, next_attempt_at=timezone.now() - timedelta(minutes=1))
        result = send_pending_email()
        self.assertEqual(result["email_accepted"], 1)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(Notification.objects.get(user=self.reviewer).email_status, "failed")

    @override_settings(SMTP_ENABLED=True)
    @patch("portal.monitoring.send_mail", return_value=1)
    def test_revoked_institution_scope_blocks_already_queued_email(self, send):
        instrument = self.instrument()
        notification = Notification.objects.create(user=self.reviewer, instrument=instrument, title="Reminder", body="Review", dedupe_key="revoked", email_status="queued")
        self.reviewer.profile.institutions.clear()
        send_pending_email()
        self.assertFalse(send.called)
        notification.refresh_from_db()
        self.assertEqual(notification.email_status, "disabled")

    def test_reporting_separates_maturity_expiry_and_current_exposure(self):
        current = self.instrument()
        original = self.instrument(status="superseded", amount=Decimal("900000.00"))
        successor = self.instrument(predecessor=original, maturity_date=self.today + timedelta(days=180))
        matured = self.instrument(maturity_date=self.today)
        expired = self.instrument(validity_date=self.today, maturity_date=self.today + timedelta(days=180))
        self.instrument(status="released")
        self.instrument(institution=self.other)
        pending = self.instrument(status="submitted", submitted_at=self.instant(self.today - timedelta(days=8)), bank_status="discrepant")
        for instrument in (current, successor, matured, expired):
            self.document(instrument)
        scope = Instrument.objects.filter(institution=self.institution)
        metrics = metrics_for(scope, self.today)
        self.assertEqual(metrics["active"], 2)
        self.assertEqual(metrics["expiring"], 1)
        self.assertEqual(metrics["matured"], 1)
        self.assertEqual(metrics["expired"], 1)
        self.assertEqual(metrics["renewed"], 1)
        self.assertEqual(metrics["released"], 1)
        self.assertEqual(metrics["portfolio_amount"], Decimal("400000.00"))
        self.assertEqual(metrics["overdue"], 3)
        self.assertEqual(metrics["missing"], 1)
        self.assertEqual(metrics["discrepant"], 1)
        self.assertEqual(list(filtered_instruments(scope, {"report": "missing"}, self.today)), [pending])
        self.assertEqual(list(filtered_instruments(scope, {"temporal": "expired"}, self.today)), [expired])
        self.assertEqual(list(filtered_instruments(scope, {"report": "renewed"}, self.today)), [successor])

    def test_reporting_follows_policy_window_and_keeps_scope(self):
        short_policy = Policy.objects.create(name="Short window", reference="Example", release_conditions=["Approval"], reminder_days=[7, 1])
        short = self.instrument(policy=short_policy)
        self.instrument(institution=self.other)
        scope = Instrument.objects.filter(institution=self.institution)
        self.assertEqual(metrics_for(scope, self.today)["expiring"], 0)
        self.assertEqual(list(filtered_instruments(scope, {"temporal": "valid", "q": "Test Bank"}, self.today)), [short])
        self.assertFalse(filtered_instruments(scope, {"q": "Other Institute"}, self.today).exists())

    def test_turnaround_reports_observed_hours_and_undated_deposits_are_active(self):
        submitted = self.instant(self.today - timedelta(days=2))
        self.instrument(kind="deposit", maturity_date=None, submitted_at=submitted, decided_at=submitted + timedelta(hours=12))
        metrics = metrics_for(Instrument.objects.all(), self.today)
        self.assertEqual(metrics["active"], 1)
        self.assertEqual(metrics["expiring"], 0)
        self.assertEqual(metrics["avg_turnaround"], 12)
        self.assertEqual(filtered_instruments(Instrument.objects.all(), {"temporal": "no_expiry"}, self.today).count(), 1)

    @patch("portal.monitoring._release_reminders", side_effect=RuntimeError("private backend detail"))
    def test_worker_failure_records_sanitized_heartbeat(self, reminder):
        with self.assertRaises(RuntimeError):
            run_monitoring(self.today)
        heartbeat = WorkerHeartbeat.objects.get(name="monitor_deadlines")
        self.assertIn("RuntimeError", heartbeat.last_error)
        self.assertNotIn("private backend detail", heartbeat.last_error)

    def test_management_command_once_reports_counts(self):
        output = StringIO()
        call_command("monitor_deadlines", once=True, stdout=output)
        self.assertIn('"notifications_created": 0', output.getvalue())
