from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.user.models import CollectorProfile
from apps.user.admin_portal.services import set_collector_account_active


class CollectorAccountAccessTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_superuser('access_admin', password='test-password')
        self.user = User.objects.create_user('access_collector', password='test-password', account_activated_at=timezone.now())
        self.profile = CollectorProfile.objects.create(user=self.user, approval_status='suspended')
        self.client.force_login(self.admin)

    def test_toggle_preserves_suspension_and_records_audit(self):
        url = reverse('system_admin_collector_account_access', args=[self.user.pk])
        self.assertEqual(self.client.get(url).status_code, 405)
        for action, expected in [('deactivate', False), ('activate', True)]:
            self.assertEqual(self.client.post(url, {'action': action}).status_code, 302)
            self.user.refresh_from_db()
            self.profile.refresh_from_db()
            self.assertEqual(self.user.is_active, expected)
            self.assertEqual(self.profile.approval_status, 'suspended')
        self.assertEqual(AuditEvent.objects.filter(action='system_admin_user_updated', entity_id=str(self.user.pk)).count(), 2)

    def test_incomplete_setup_toggle_does_not_bypass_setup(self):
        self.user.is_active = False
        self.user.account_activated_at = None
        self.user.set_unusable_password()
        self.user.save()
        set_collector_account_active(actor=self.admin, user_id=self.user.pk, active=False)
        self.user.refresh_from_db()
        self.assertTrue(self.user.collector_account_disabled)
        response = self.client.get(reverse('system_admin_users'))
        self.assertContains(response, 'Activate account')
        set_collector_account_active(actor=self.admin, user_id=self.user.pk, active=True)
        self.user.refresh_from_db()
        self.assertFalse(self.user.collector_account_disabled)
        self.assertFalse(self.user.is_active)
        self.assertFalse(self.user.has_usable_password())
        response = self.client.get(reverse('system_admin_users'))
        self.assertContains(response, 'Deactivate account')
        self.assertContains(response, 'Suspended')

    def test_collector_menu_and_edit_only_show_account_management(self):
        response = self.client.get(reverse('system_admin_users'))
        self.assertContains(response, 'Deactivate account')
        for text in ['Manage Client Access', 'Send Password Reset', 'Issue Warning', 'Reinstate Collector', 'Portal Access']:
            self.assertNotContains(response, text)
        response = self.client.get(reverse('system_admin_collector_edit', args=[self.user.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="client_accesses"')
        self.assertNotContains(response, 'type="password"')

    def test_non_admin_cannot_toggle_account(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse('system_admin_collector_account_access', args=[self.user.pk]), {'action': 'deactivate'})
        self.assertIn(response.status_code, [302, 403])
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)
