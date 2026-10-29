from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from portal.models import AuditEvent, Institution, Instrument, Policy, Profile, ReleaseRequest


class NullableActorRenderingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user('template-reviewer', password='TemplateTest!26265')
        cls.institution = Institution.objects.create(code='TEMPLATE', name='Template Institute', email='test@example.invalid')
        profile = Profile.objects.create(user=cls.user, role='reviewer')
        profile.institutions.add(cls.institution)
        policy = Policy.objects.create(name='Template policy', reference='Illustrative rendering test', release_conditions=['Eligibility documented'])
        cls.instrument = Instrument.objects.create(
            institution=cls.institution, created_by=cls.user, policy=policy,
            reference='TEMPLATE-FDR', bank_name='Example Bank', ifsc='TEST0000001',
            amount=Decimal('100000'), issue_date=timezone.localdate()-timedelta(days=30),
            maturity_date=timezone.localdate()+timedelta(days=60), status='accepted',
        )
        cls.case = ReleaseRequest.objects.create(
            instrument=cls.instrument, created_by=cls.user, policy=policy,
            kind='release', reason='Rendering an unreviewed request',
        )
        AuditEvent.objects.create(
            actor=None, institution=cls.institution, instrument=cls.instrument,
            action='monitoring.deadline', details={'source':'scheduled monitoring'},
        )

    def setUp(self):
        self.client.force_login(self.user)

    def test_pending_release_renders_without_reviewer_or_approver(self):
        response = self.client.get(reverse('portal:release_detail', args=[self.case.pk]))
        self.assertContains(response, 'Not yet recommended')
        self.assertContains(response, 'Not yet approved')

    def test_scheduled_audit_actor_renders_on_all_activity_screens(self):
        routes = [reverse('portal:dashboard'), reverse('portal:instrument_detail', args=[self.instrument.pk]), reverse('portal:audit')]
        for route in routes:
            with self.subTest(route=route):
                self.assertContains(self.client.get(route), 'Scheduled monitoring')
