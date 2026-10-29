"""Regression proofs at the forms, imports, authentication and command boundaries."""
import tempfile
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from portal import services
from portal.documents import upload_document
from portal.forms import InstrumentForm
from portal.models import Institution, Instrument, LoginAttempt, Policy, Profile
from portal.tests.test_views import image_upload


class SecurityRegressionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.institution = Institution.objects.create(code='SEC-001', name='Security Test Institute', email='security@example.invalid')
        cls.policy = Policy.objects.create(name='Security test policy', reference='Illustrative security tests', release_conditions=['Eligibility checked'])
        cls.users = {}
        for role in ('institution', 'reviewer', 'approver'):
            user = get_user_model().objects.create_user(role, password='SecureTest!26265')
            Profile.objects.create(user=user, role=role).institutions.add(cls.institution)
            cls.users[role] = user

    def setUp(self):
        self.storage = tempfile.TemporaryDirectory()
        self.override = override_settings(PRIVATE_STORAGE_ROOT=self.storage.name)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.addCleanup(self.storage.cleanup)

    def draft(self, reference='SEC-FDR-001'):
        return services.create_instrument(self.users['institution'], {
            'institution': self.institution, 'policy': self.policy, 'kind': 'fdr', 'reference': reference,
            'bank_name': 'Test Bank', 'ifsc': 'TEST0000001', 'amount': Decimal('100000.00'),
            'issue_date': timezone.localdate() - timedelta(days=60),
            'maturity_date': timezone.localdate() + timedelta(days=30), 'validity_date': None,
        })

    def submitted(self):
        item = self.draft()
        upload_document(self.users['institution'], item, image_upload(), 'instrument')
        item.refresh_from_db()
        return services.submit_instrument(self.users['institution'], item, item.version)

    def csv_file(self, reference='CSV-SEC-001', duplicate_header=False):
        headers = 'institution_code,kind,reference,bank_name,ifsc,amount,issue_date,maturity_date,validity_date'
        row = f'SEC-001,fdr,{reference},Test Bank,TEST0000001,100000,{timezone.localdate()-timedelta(days=60)},{timezone.localdate()+timedelta(days=30)},'
        if duplicate_header:
            headers += ',amount'
            row += ',999'
        return SimpleUploadedFile('security.csv', (headers+'\n'+row+'\n').encode(), content_type='text/csv')

    def test_returned_case_can_keep_retired_policy_without_switching_it(self):
        item = self.submitted()
        item = services.review_instrument(self.users['reviewer'], item, 'corrections', 'Correct the amount.', item.version)
        self.policy.is_active = False
        self.policy.save()
        new_policy = Policy.objects.create(name='New policy', reference='New illustrative policy', release_conditions=['New condition'])
        form = InstrumentForm({
            'institution': self.institution.pk, 'kind': 'fdr', 'reference': item.reference,
            'bank_name': item.bank_name, 'ifsc': item.ifsc, 'amount': '200000.00',
            'issue_date': str(item.issue_date), 'maturity_date': str(item.maturity_date),
            'validity_date': '', 'policy': new_policy.pk, 'expected_version': item.version,
        }, user=self.users['institution'], instance=item)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['policy'].pk, self.policy.pk)
        updated = services.edit_instrument(self.users['institution'], item, form.cleaned_data, item.version)
        self.assertEqual(updated.policy_id, self.policy.pk)
        self.assertEqual(updated.amount, Decimal('200000.00'))

    def test_replayed_session_import_token_is_consumed_transactionally(self):
        self.client.force_login(self.users['institution'])
        url = reverse('portal:import_records')
        self.client.post(url, {'file': self.csv_file()})
        preview = self.client.session['csv_preview']
        payload = {'confirm': '1', 'token': preview['token']}
        self.client.post(url, payload)
        self.assertEqual(Instrument.objects.count(), 1)
        # A parallel request can have loaded the old session before the first commit.
        session = self.client.session
        session['csv_preview'] = preview
        session.save()
        self.client.post(url, payload)
        self.assertEqual(Instrument.objects.count(), 1)

    def test_import_preview_flags_existing_receipt_instead_of_creating_duplicate_draft(self):
        self.draft(reference='CSV-SEC-001')
        self.client.force_login(self.users['institution'])
        response = self.client.post(reverse('portal:import_records'), {'file': self.csv_file()})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['errors'])
        self.assertEqual(Instrument.objects.count(), 1)

    def test_duplicate_csv_headers_are_rejected(self):
        self.client.force_login(self.users['institution'])
        response = self.client.post(reverse('portal:import_records'), {'file': self.csv_file(duplicate_header=True)})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['form'].errors)
        self.assertNotIn('csv_preview', self.client.session)

    def test_locked_out_login_does_not_attempt_password_authentication(self):
        for _ in range(5):
            self.client.post(reverse('login'), {'username': 'institution', 'password': 'incorrect'})
        with patch('django.contrib.auth.forms.authenticate') as authenticate:
            response = self.client.post(reverse('login'), {'username': 'institution', 'password': 'SecureTest!26265'})
        self.assertEqual(response.status_code, 429)
        authenticate.assert_not_called()

    def test_overlong_execution_reference_cannot_partially_release_instrument(self):
        item = self.submitted()
        item = services.review_instrument(self.users['reviewer'], item, 'start', '', item.version)
        item = services.review_instrument(self.users['reviewer'], item, 'accept', 'Accepted.', item.version)
        evidence = upload_document(self.users['institution'], item, image_upload(), 'release')
        case = services.start_release(self.users['institution'], item, kind='release', reason='Eligible.')
        case = services.transition_release(self.users['reviewer'], case, 'recommend', expected_version=case.version,
            note='Eligibility checked.', conditions={'Eligibility checked': True}, evidence=evidence)
        case = services.transition_release(self.users['approver'], case, 'approve', expected_version=case.version, note='Approved.')
        completion = upload_document(self.users['reviewer'], item, image_upload(), 'completion')
        with self.assertRaises(ValidationError):
            services.transition_release(self.users['reviewer'], case, 'complete', expected_version=case.version,
                execution_reference='x'*251, execution_date=timezone.localdate(), evidence=completion)
        item.refresh_from_db()
        case.refresh_from_db()
        self.assertEqual(item.status, 'accepted')
        self.assertEqual(case.status, 'approved')

    def test_malformed_policy_retirement_is_a_validation_failure(self):
        admin = get_user_model().objects.create_user('policy-admin', password='SecureTest!26265')
        Profile.objects.create(user=admin, role='admin')
        self.client.force_login(admin)
        response = self.client.post(reverse('portal:administration'), {'form_type': 'retire_policy', 'policy_id': 'not-an-id'})
        self.assertIn(response.status_code, (200, 302, 400, 404))
        self.policy.refresh_from_db()
        self.assertTrue(self.policy.is_active)
