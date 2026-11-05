from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.user.models import CollectorProfile


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class CollectorEmailLoginTests(TestCase):
    def test_create_uses_email_and_invitation_explains_login(self):
        User = get_user_model()
        admin = User.objects.create_superuser('email_admin', password='Password123!')
        self.client.force_login(admin)
        response = self.client.get(reverse('system_admin_collector_create'))
        self.assertNotContains(response, 'name="username"')
        response = self.client.post(reverse('system_admin_collector_create'), {
            'email': 'NewCollector@example.com', 'first_name': 'Juan', 'last_name': 'Cruz',
        })
        self.assertEqual(response.status_code, 302)
        user = User.objects.get(email='newcollector@example.com')
        self.assertEqual(user.username, user.email)
        self.assertFalse(user.is_active)
        self.assertIn('Login email: newcollector@example.com', mail.outbox[-1].body)
        self.assertIn('newcollector@example.com', mail.outbox[-1].alternatives[0].content)

    def test_existing_collector_can_login_by_email_without_renaming(self):
        user = get_user_model().objects.create_user('legacy_collector', email='legacy@example.com', password='Password123!')
        CollectorProfile.objects.create(user=user)
        self.assertTrue(self.client.login(username='LEGACY@example.com', password='Password123!'))
        user.is_active = False
        user.save()
        self.assertFalse(self.client.login(username='legacy@example.com', password='Password123!'))
