import csv
import io
import tempfile
from datetime import timedelta
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image
from portal.models import Institution,Profile,Policy,Instrument,Document,AuditEvent,Notification,LoginAttempt
from portal import services
from portal.documents import upload_document,read_document

def image_upload(name='evidence.png'):
    stream=io.BytesIO()
    Image.new('RGB',(50,30),'white').save(stream,format='PNG')
    return SimpleUploadedFile(name,stream.getvalue(),content_type='image/png')

class ViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.institution=Institution.objects.create(code='TEST-ONE',name='Test Technical Institute',email='office@example.invalid')
        cls.other=Institution.objects.create(code='TEST-TWO',name='Other Institute',email='other@example.invalid')
        cls.policy=Policy.objects.create(name='Test policy',reference='Illustrative test only',release_conditions=['Eligibility recorded'])
        cls.users={}
        for role in ['institution','reviewer','approver','auditor','admin']:
            user=get_user_model().objects.create_user(role,password='TestSecure!26265')
            profile=Profile.objects.create(user=user,role=role)
            if role!='admin': profile.institutions.add(cls.institution)
            cls.users[role]=user
        cls.other_user=get_user_model().objects.create_user('other',password='TestSecure!26265')
        profile=Profile.objects.create(user=cls.other_user,role='institution'); profile.institutions.add(cls.other)

    def setUp(self):
        self.storage=tempfile.TemporaryDirectory()
        self.override=override_settings(PRIVATE_STORAGE_ROOT=self.storage.name)
        self.override.enable(); self.addCleanup(self.override.disable); self.addCleanup(self.storage.cleanup)

    def draft(self,other=False,reference='VIEW-001'):
        return services.create_instrument(self.other_user if other else self.users['institution'],{'institution':self.other if other else self.institution,'policy':self.policy,'kind':'fdr','reference':reference,'bank_name':'Example Bank','ifsc':'TEST0000001','amount':Decimal('150000.00'),'issue_date':timezone.localdate()-timedelta(days=100),'maturity_date':timezone.localdate()+timedelta(days=20),'validity_date':None})

    def test_all_role_pages_and_access_boundaries(self):
        item=self.draft()
        for role,user in self.users.items():
            self.client.force_login(user)
            for name in ['dashboard','instruments','notifications','reports']:
                with self.subTest(role=role,page=name): self.assertEqual(self.client.get(reverse('portal:'+name),follow=True).status_code,200)
            self.assertEqual(self.client.get(reverse('portal:administration')).status_code,200 if role=='admin' else 403)
            self.assertEqual(self.client.get(reverse('portal:instrument_detail',args=[item.pk])).status_code,404 if role=='admin' else 200)
        self.client.force_login(self.other_user)
        self.assertEqual(self.client.get(reverse('portal:instrument_detail',args=[item.pk])).status_code,404)

    def test_submission_and_document_routes(self):
        item=self.draft()
        self.client.force_login(self.users['institution'])
        self.client.post(reverse('portal:document_upload',args=[item.pk]),{'category':'instrument','file':image_upload()})
        document=Document.objects.get(instrument=item)
        self.assertEqual(self.client.get(reverse('portal:document_download',args=[document.pk])).status_code,200)
        item.refresh_from_db()
        self.client.post(reverse('portal:instrument_submit',args=[item.pk]),{'expected_version':item.version})
        item.refresh_from_db(); self.assertEqual(item.status,'submitted')
        self.assertEqual(item.bank_status,'unconfirmed')
        self.client.force_login(self.other_user)
        self.assertEqual(self.client.get(reverse('portal:document_download',args=[document.pk])).status_code,404)

    def test_invalid_upload_does_not_create_document(self):
        item=self.draft(); self.client.force_login(self.users['institution'])
        self.client.post(reverse('portal:document_upload',args=[item.pk]),{'category':'instrument','file':SimpleUploadedFile('evil.pdf',b'<script>bad</script>',content_type='application/pdf')})
        self.assertFalse(Document.objects.exists())

    def test_csrf_and_get_mutation_protection(self):
        item=self.draft(); client=Client(enforce_csrf_checks=True); client.force_login(self.users['institution'])
        url=reverse('portal:instrument_submit',args=[item.pk])
        self.assertEqual(client.post(url,{'expected_version':item.version}).status_code,403)
        self.assertEqual(client.get(url).status_code,405)
        self.assertEqual(client.get(reverse('logout')).status_code,405)

    def test_export_scopes_and_formula_escaping(self):
        item=self.draft(reference='=1+1'); self.draft(other=True,reference='SECRET-OTHER')
        self.client.force_login(self.users['auditor'])
        response=self.client.get(reverse('portal:export'))
        body=response.content.decode('utf-8-sig')
        self.assertIn("'=1+1",body); self.assertNotIn('SECRET-OTHER',body)
        self.assertTrue(AuditEvent.objects.filter(action='records.exported').exists())

    def test_import_preview_confirm_and_replay(self):
        self.client.force_login(self.users['institution'])
        content='institution_code,kind,reference,bank_name,ifsc,amount,issue_date,maturity_date,validity_date\nTEST-ONE,fdr,CSV-001,Example Bank,TEST0000001,1000,2026-01-01,2027-01-01,\n'
        response=self.client.post(reverse('portal:import_records'),{'file':SimpleUploadedFile('records.csv',content.encode())})
        self.assertEqual(response.status_code,200)
        self.assertFalse(Instrument.objects.exists())
        token=self.client.session['csv_preview']['token']
        self.client.post(reverse('portal:import_records'),{'confirm':'1','token':token})
        self.assertEqual(Instrument.objects.count(),1)
        self.client.post(reverse('portal:import_records'),{'confirm':'1','token':token})
        self.assertEqual(Instrument.objects.count(),1)
        self.assertEqual(Instrument.objects.get().status,'draft')

    def test_invalid_import_no_partial_records(self):
        self.client.force_login(self.users['institution'])
        content='institution_code,kind,reference,bank_name,ifsc,amount,issue_date,maturity_date,validity_date\nTEST-TWO,fdr,CSV-002,Bank,TEST0000001,1000,2026-01-01,2027-01-01,\n'
        self.client.post(reverse('portal:import_records'),{'file':SimpleUploadedFile('records.csv',content.encode())})
        token=self.client.session['csv_preview']['token']
        self.client.post(reverse('portal:import_records'),{'confirm':'1','token':token})
        self.assertFalse(Instrument.objects.exists())

    def test_login_throttling_and_logout(self):
        for _ in range(5): self.client.post(reverse('login'),{'username':'institution','password':'incorrect'})
        response=self.client.post(reverse('login'),{'username':'institution','password':'TestSecure!26265'})
        self.assertEqual(response.status_code,429)
        LoginAttempt.objects.all().delete()
        self.assertEqual(self.client.post(reverse('login'),{'username':'institution','password':'TestSecure!26265'}).status_code,302)
        self.assertEqual(self.client.post(reverse('logout')).status_code,302)

    def test_revoked_scope_hides_existing_notification(self):
        item=self.draft()
        Notification.objects.create(user=self.users['reviewer'],instrument=item,title='Confidential reference',body='Details',dedupe_key='test')
        Profile.objects.get(user=self.users['reviewer']).institutions.clear()
        self.client.force_login(self.users['reviewer'])
        response=self.client.get(reverse('portal:notifications'))
        self.assertNotContains(response,'Confidential reference')

    def test_administration_creates_versioned_policy(self):
        self.client.force_login(self.users['admin'])
        response=self.client.post(reverse('portal:administration'),{'form_type':'policy','name':'New policy','version':1,'reference':'Authorized reference','required_documents':['instrument'],'review_sla_days':10,'illustrative':'on','is_active':'on','conditions_text':'Eligibility recorded\nOutstanding obligations cleared','reminders_text':'30,7,1'})
        self.assertEqual(response.status_code,302)
        policy=Policy.objects.get(name='New policy')
        self.assertEqual(policy.reminder_days,[30,7,1])
        self.assertEqual(len(policy.release_conditions),2)

    def test_health(self):
        self.assertEqual(self.client.get('/health/').json(),{'status':'ok'})
