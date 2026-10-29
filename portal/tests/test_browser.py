"""Real Chromium journeys. Opt in with RUN_BROWSER_TESTS=1; no mocked services."""
import io
import os
import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings
from django.utils import timezone
from PIL import Image
from portal.models import Institution,Profile,Policy,Instrument,ReleaseRequest,Submission
from portal import services
from portal.documents import upload_document
from portal.tests.test_views import image_upload

@unittest.skipUnless(os.environ.get('RUN_BROWSER_TESTS')=='1','Set RUN_BROWSER_TESTS=1 to run real-browser journeys.')
class BrowserJourneys(StaticLiveServerTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Playwright's synchronous API owns an event loop on this test thread.
        # These ORM assertions are strictly sequential, with no async ORM tasks.
        cls.previous_async_guard=os.environ.get('DJANGO_ALLOW_ASYNC_UNSAFE')
        os.environ['DJANGO_ALLOW_ASYNC_UNSAFE']='true'
        from playwright.sync_api import sync_playwright
        cls.playwright=sync_playwright().start()
        cls.browser=cls.playwright.chromium.launch(executable_path=os.environ.get('CHROME_PATH','/opt/google/chrome/chrome'),headless=True,args=['--no-sandbox'])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.playwright.stop()
        if cls.previous_async_guard is None: os.environ.pop('DJANGO_ALLOW_ASYNC_UNSAFE',None)
        else: os.environ['DJANGO_ALLOW_ASYNC_UNSAFE']=cls.previous_async_guard
        super().tearDownClass()

    def setUp(self):
        self.storage=tempfile.TemporaryDirectory()
        self.override=override_settings(PRIVATE_STORAGE_ROOT=self.storage.name)
        self.override.enable()
        self.context=self.browser.new_context(locale='en-IN',timezone_id='Asia/Kolkata',viewport={'width':1440,'height':1000})
        self.page=self.context.new_page()
        self.errors=[]
        self.page.on('pageerror',lambda error:self.errors.append(str(error)))
        self.institution=Institution.objects.create(code='BROWSER',name='Browser Technical Institute',email='office@example.invalid')
        self.policy=Policy.objects.create(name='Browser policy',reference='Illustrative browser acceptance policy',release_conditions=['Eligibility independently checked'])
        self.users={}
        for role in ['institution','reviewer','approver']:
            user=get_user_model().objects.create_user(role,password='BrowserPass!26265',first_name=role.title())
            profile=Profile.objects.create(user=user,role=role); profile.institutions.add(self.institution)
            self.users[role]=user
        self.evidence=Path(self.storage.name)/'evidence.png'
        Image.new('RGB',(400,200),'white').save(self.evidence)

    def tearDown(self):
        self.context.close(); self.override.disable(); self.storage.cleanup()
        super().tearDown()

    def login(self,username):
        self.context.clear_cookies()
        self.page.goto(self.live_server_url+'/login/')
        self.page.locator('[name=username]').fill(username)
        self.page.locator('[name=password]').fill('BrowserPass!26265')
        self.page.locator('button[type=submit]').click()
        self.page.wait_for_url(self.live_server_url+'/dashboard/')

    def upload(self,category):
        form=self.page.locator('form[action$="/documents/"]')
        form.locator('[name=category]').select_option(category)
        form.locator('[name=file]').set_input_files(self.evidence)
        form.locator('button[type=submit]').click()
        self.page.wait_for_load_state()

    def review(self,action,note=''):
        form=self.page.locator('form[action$="/action/"]').filter(has=self.page.locator('textarea[name=note]'))
        if note: form.locator('[name=note]').fill(note)
        form.locator(f'button[value="{action}"]').click()
        self.page.wait_for_load_state()

    def test_submission_correction_resubmission_and_acceptance(self):
        self.login('institution')
        self.page.goto(self.live_server_url+'/instruments/new/')
        for name,value in {'reference':'BROWSER-FDR-001','bank_name':'Example Bank','ifsc':'TEST0000001','amount':'500000','issue_date':str(timezone.localdate()-timedelta(days=90)),'maturity_date':str(timezone.localdate()+timedelta(days=30))}.items():
            self.page.locator(f'[name={name}]').fill(value)
        self.page.locator('[name=institution]').select_option(str(self.institution.pk))
        self.page.locator('[name=policy]').select_option(str(self.policy.pk))
        self.page.locator('main form button[type=submit]').click()
        self.page.wait_for_url('**/instruments/*/')
        item=Instrument.objects.get(reference='BROWSER-FDR-001')
        self.upload('instrument')
        self.page.locator('form[action$="/submit/"] button').click()
        self.page.wait_for_load_state()
        item.refresh_from_db(); self.assertEqual(item.status,'submitted')
        self.login('reviewer'); self.page.goto(self.live_server_url+f'/instruments/{item.pk}/')
        self.review('start')
        self.review('corrections','Please provide the additional deposit confirmation.')
        item.refresh_from_db(); self.assertEqual(item.status,'corrections')
        self.login('institution'); self.page.goto(self.live_server_url+f'/instruments/{item.pk}/')
        self.upload('supporting')
        self.page.locator('form[action$="/submit/"] button').click(); self.page.wait_for_load_state()
        self.login('reviewer'); self.page.goto(self.live_server_url+f'/instruments/{item.pk}/')
        self.review('start'); self.review('accept','Administrative requirements satisfied; independent bank confirmation remains pending.')
        item.refresh_from_db()
        self.assertEqual(item.status,'accepted'); self.assertEqual(item.bank_status,'unconfirmed')
        self.assertEqual(Submission.objects.filter(instrument=item).count(),2)
        self.page.reload(); self.assertIn('Accepted',self.page.locator('main').inner_text())
        self.assertEqual(self.errors,[])

    def test_release_approval_and_external_completion(self):
        item=services.create_instrument(self.users['institution'],{'institution':self.institution,'policy':self.policy,'kind':'fdr','reference':'BROWSER-RELEASE','bank_name':'Example Bank','ifsc':'TEST0000001','amount':Decimal('250000'),'issue_date':timezone.localdate()-timedelta(days=365),'maturity_date':timezone.localdate()-timedelta(days=1),'validity_date':None})
        upload_document(self.users['institution'],item,image_upload(),'instrument'); item.refresh_from_db()
        item=services.submit_instrument(self.users['institution'],item,item.version)
        item=services.review_instrument(self.users['reviewer'],item,'start','',item.version)
        item=services.review_instrument(self.users['reviewer'],item,'accept','Eligible for administration',item.version)
        self.login('institution'); self.page.goto(self.live_server_url+f'/instruments/{item.pk}/'); self.upload('release')
        self.page.goto(self.live_server_url+f'/instruments/{item.pk}/release/')
        self.page.locator('[name=kind]').select_option('refund')
        self.page.locator('[name=reason]').fill('Request full principal refund after documented eligibility.')
        self.page.locator('main form button[type=submit]').click(); self.page.wait_for_load_state()
        case=ReleaseRequest.objects.get(instrument=item)
        self.login('reviewer'); self.page.goto(self.live_server_url+f'/releases/{case.pk}/')
        form=self.page.locator('form[action$="/action/"]')
        form.locator('[name=conditions]').check()
        form.locator('[name=evidence]').select_option(str(item.documents.get(category='release').pk))
        self.review('recommend','Eligibility independently checked against uploaded evidence.')
        self.login('approver'); self.page.goto(self.live_server_url+f'/releases/{case.pk}/')
        self.review('approve','Authorized by competent authority under the recorded policy.')
        case.refresh_from_db(); item.refresh_from_db()
        self.assertEqual(case.status,'approved'); self.assertEqual(item.status,'accepted')
        self.login('reviewer'); self.page.goto(self.live_server_url+f'/instruments/{item.pk}/'); self.upload('completion')
        self.page.goto(self.live_server_url+f'/releases/{case.pk}/')
        form=self.page.locator('form[action$="/action/"]')
        form.locator('[name=evidence]').select_option(str(item.documents.get(category='completion').pk))
        form.locator('[name=execution_reference]').fill('EXTERNAL-REFUND-001')
        form.locator('[name=execution_date]').fill(str(timezone.localdate()))
        self.review('complete','External payment evidence recorded.')
        case.refresh_from_db(); item.refresh_from_db()
        self.assertEqual(case.status,'completed'); self.assertEqual(item.status,'released')
        self.assertEqual(self.errors,[])
