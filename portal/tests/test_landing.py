from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from portal.models import Profile


class PublicLandingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.password = 'LandingTest!26265'
        cls.user = get_user_model().objects.create_user('landing-reviewer', password=cls.password)
        Profile.objects.create(user=cls.user, role='reviewer')

    def test_public_homepage_renders_content_and_sign_in_navigation(self):
        response = self.client.get(reverse('portal:home'))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'portal/landing.html')
        self.assertContains(response, 'A clearer view of every')
        self.assertContains(response, 'ILLUSTRATIVE')
        self.assertContains(response, 'Independent verification')
        self.assertContains(response, 'href="/login/"')

    def test_authenticated_homepage_redirects_to_dashboard(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse('portal:home'))

        self.assertRedirects(response, reverse('portal:dashboard'), fetch_redirect_response=False)

    def test_dashboard_remains_authentication_protected(self):
        response = self.client.get(reverse('portal:dashboard'))

        self.assertRedirects(
            response,
            f"{reverse('login')}?next={reverse('portal:dashboard')}",
        )

    def test_sign_in_and_password_change_return_to_dashboard(self):
        login_response = self.client.post(reverse('login'), {
            'username': self.user.username,
            'password': self.password,
        })
        self.assertRedirects(login_response, reverse('portal:dashboard'), fetch_redirect_response=False)

        new_password = 'ChangedSecure!9283'
        password_response = self.client.post(reverse('password_change'), {
            'old_password': self.password,
            'new_password1': new_password,
            'new_password2': new_password,
        })
        self.assertRedirects(password_response, reverse('portal:dashboard'), fetch_redirect_response=False)
