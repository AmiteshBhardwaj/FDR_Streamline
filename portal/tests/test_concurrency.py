"""Actual competing commands on separate database connections.

SQLite can reject a contender with a lock conflict; a successful command must
still commit one consistent workflow and audit, never two competing processes.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from tempfile import TemporaryDirectory
from threading import Barrier

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import OperationalError, close_old_connections, connections
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from portal import services
from portal.documents import upload_document
from portal.models import AuditEvent, Institution, Instrument, Policy, Profile, ReleaseRequest
from portal.tests.test_views import image_upload


class ConcurrentLifecycleTests(TransactionTestCase):
    def setUp(self):
        self.storage = TemporaryDirectory()
        self.override = override_settings(PRIVATE_STORAGE_ROOT=self.storage.name)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.addCleanup(self.storage.cleanup)
        self.institution = Institution.objects.create(code='RACE', name='Concurrency Test Institute', email='race@example.invalid')
        self.policy = Policy.objects.create(name='Concurrency test policy', reference='Illustrative concurrency tests', release_conditions=['Eligibility checked'])
        self.users = {}
        for username, role in [('institution', 'institution'), ('reviewer', 'reviewer'), ('authority-one', 'approver'), ('authority-two', 'approver')]:
            user = get_user_model().objects.create_user(username=username)
            Profile.objects.create(user=user, role=role).institutions.add(self.institution)
            self.users[username] = user
        item = services.create_instrument(self.users['institution'], self.data())
        upload_document(self.users['institution'], item, image_upload(), 'instrument')
        item.refresh_from_db()
        item = services.submit_instrument(self.users['institution'], item, item.version)
        item = services.review_instrument(self.users['reviewer'], item, 'start', '', item.version)
        self.instrument = services.review_instrument(self.users['reviewer'], item, 'accept', 'Administrative records accepted.', item.version)

    def data(self, **overrides):
        data = {
            'institution': self.institution.pk, 'policy': self.policy.pk, 'kind': 'fdr', 'reference': 'RACE-FDR-001',
            'bank_name': 'Test Bank', 'ifsc': 'TEST0000001', 'amount': Decimal('250000.00'),
            'issue_date': timezone.localdate() - timedelta(days=60),
            'maturity_date': timezone.localdate() + timedelta(days=30), 'validity_date': None,
        }
        data.update(overrides)
        return data

    def race(self, operations):
        barrier = Barrier(2)

        def run(operation):
            close_old_connections()
            try:
                # Each participant uses its own connection; both start the
                # command at the barrier, with no enclosing test transaction.
                barrier.wait(timeout=5)
                result = operation()
                return ('success', str(result.pk))
            except (ValidationError, OperationalError) as exc:
                return (type(exc).__name__, str(exc))
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(run, operation) for operation in operations]
            results = [future.result(timeout=15) for future in futures]
        self.assertEqual(sum(result[0] == 'success' for result in results), 1, results)
        self.assertEqual(sum(result[0] in ('ValidationError', 'OperationalError') for result in results), 1, results)
        return results

    def test_renewal_and_release_cannot_both_start(self):
        instrument_id = self.instrument.pk
        user_id = self.users['institution'].pk
        initial_version = self.instrument.version
        audit_count = AuditEvent.objects.count()

        def renewal():
            actor = get_user_model().objects.get(pk=user_id)
            instrument = Instrument.objects.get(pk=instrument_id)
            return services.create_instrument(actor, self.data(reference='RACE-RENEWAL', issue_date=timezone.localdate()), predecessor=instrument)

        def release():
            actor = get_user_model().objects.get(pk=user_id)
            instrument = Instrument.objects.get(pk=instrument_id)
            return services.start_release(actor, instrument, kind='release', reason='Eligible release request.')

        self.race([renewal, release])
        successors = Instrument.objects.filter(predecessor_id=instrument_id, status__in=services.OPEN_SUCCESSOR_STATES)
        requests = ReleaseRequest.objects.filter(instrument_id=instrument_id, status__in=services.OPEN_RELEASE_STATES)
        self.assertEqual(successors.count() + requests.count(), 1)
        self.instrument.refresh_from_db()
        self.assertEqual(self.instrument.status, 'accepted')
        self.assertEqual(self.instrument.version, initial_version + 1)
        self.assertEqual(AuditEvent.objects.count(), audit_count + 1)
        if successors.exists():
            self.assertEqual(AuditEvent.objects.filter(instrument=successors.get(), action='instrument.created').count(), 1)
        else:
            self.assertEqual(AuditEvent.objects.filter(instrument=self.instrument, action='release.requested').count(), 1)

    def test_simultaneous_authority_decisions_record_one_approval(self):
        evidence = upload_document(self.users['institution'], self.instrument, image_upload(), 'release')
        case = services.start_release(self.users['institution'], self.instrument, kind='release', reason='Eligible release request.')
        case = services.transition_release(self.users['reviewer'], case, 'recommend', expected_version=case.version,
            note='Conditions checked.', conditions={'Eligibility checked': True}, evidence=evidence)
        expected_version = case.version
        audit_count = AuditEvent.objects.count()
        self.instrument.refresh_from_db()
        instrument_version = self.instrument.version

        def decision(user_id):
            def approve():
                actor = get_user_model().objects.get(pk=user_id)
                fresh_case = ReleaseRequest.objects.get(pk=case.pk)
                return services.transition_release(actor, fresh_case, 'approve', expected_version=expected_version, note='Authorized under the recorded policy.')
            return approve

        self.race([decision(self.users['authority-one'].pk), decision(self.users['authority-two'].pk)])
        case.refresh_from_db()
        self.instrument.refresh_from_db()
        self.assertEqual(case.status, 'approved')
        self.assertEqual(case.version, expected_version + 1)
        self.assertEqual(self.instrument.status, 'accepted')
        self.assertEqual(self.instrument.version, instrument_version + 1)
        self.assertEqual(AuditEvent.objects.count(), audit_count + 1)
        events = AuditEvent.objects.filter(instrument=self.instrument, action='release.approve', details__request_id=str(case.pk))
        self.assertEqual(events.count(), 1)
        self.assertEqual(events.get().actor_id, case.approver_id)
