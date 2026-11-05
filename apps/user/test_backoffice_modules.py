from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.test import TestCase, override_settings
from django.core import mail
import re
from django.urls import reverse

from apps.audit.models import AuditEvent
from apps.user.models import has_backoffice_access
from apps.user.admin_portal.services import create_backoffice_user_account, update_backoffice_user_account
from apps.user.admin_portal.services import accept_backoffice_invitation
from apps.user.admin_portal.forms import BackOfficeUserCreateForm, BackOfficeUserEditForm


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class BackOfficeModuleTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser('module_admin', password='TestPassword123!')
        self.client.force_login(self.admin)

    def create(self, modules):
        user = create_backoffice_user_account(actor=self.admin, username='module_user', email='module@example.com', module_keys=modules)
        token = re.search(r'/invitations/back-office/([^/]+)/', mail.outbox[-1].body).group(1)
        return accept_backoffice_invitation(raw_token=token, password='TestPassword123!')

    def test_create_form_and_payment_module_grants_full_workflow(self):
        response = self.client.get(reverse('system_admin_backoffice_user_create'))
        self.assertContains(response, 'name="modules"', count=11)
        self.assertContains(response, 'View, verify, and reject reported payments')
        self.assertNotContains(response, 'name="role_groups"')
        response = self.client.post(reverse('system_admin_backoffice_user_create'), {
            'username': 'module_user', 'password': 'TestPassword123!', 'password_confirm': 'TestPassword123!',
            'modules': ['payments'],
            'email': 'module@example.com',
        })
        self.assertEqual(response.status_code, 302)
        user = get_user_model().objects.get(username='module_user')
        self.assertFalse(user.is_active)
        self.assertFalse(user.has_usable_password())
        token = re.search(r'/invitations/back-office/([^/]+)/', mail.outbox[-1].body).group(1)
        user = accept_backoffice_invitation(raw_token=token, password='TestPassword123!')
        self.assertTrue(has_backoffice_access(user))
        self.assertTrue(user.has_perm('collection.change_payment'))
        self.assertFalse(user.has_perm('collection.manage_payouts'))
        self.assertFalse(user.is_staff)
        self.assertFalse(hasattr(user, 'collector_profile'))
        self.client.force_login(user)
        response = self.client.get(reverse('backoffice_payments'))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'class="bo-nav-label">Payouts')
        self.assertEqual(self.client.get(reverse('backoffice_payouts')).status_code, 403)

    def test_edit_replaces_modules_and_preserves_unrelated_permissions(self):
        user = self.create(['payments'])
        custom_group = Group.objects.create(name='Custom department')
        user.groups.add(custom_group)
        extra = Permission.objects.get(content_type__app_label='auth', codename='view_group')
        user.user_permissions.add(extra)
        response = self.client.get(reverse('system_admin_backoffice_user_edit', args=[user.pk]))
        self.assertContains(response, 'value="payments" checked')
        response = self.client.post(reverse('system_admin_backoffice_user_edit', args=[user.pk]), {'is_active': 'on', 'modules': ['commissions']})
        self.assertEqual(response.status_code, 302)
        user = get_user_model().objects.get(pk=user.pk)
        self.assertFalse(user.has_perm('collection.change_payment'))
        self.assertTrue(user.has_perm('collection.review_commissions'))
        self.assertTrue(user.has_perm('auth.view_group'))
        self.assertTrue(user.groups.filter(pk=custom_group.pk).exists())
        self.assertTrue(AuditEvent.objects.filter(action='backoffice_module_access_changed', entity_id=str(user.pk), changes__module='payments', changes__enabled=False).exists())

    def test_empty_or_unknown_modules_rejected(self):
        for modules in [[], ['made_up']]:
            response = self.client.post(reverse('system_admin_backoffice_user_create'), {'username': 'invalid_user', 'password': 'TestPassword123!', 'password_confirm': 'TestPassword123!', 'modules': modules})
            self.assertEqual(response.status_code, 200)
            self.assertFalse(get_user_model().objects.filter(username='invalid_user').exists())

    def test_forms_reject_unknown_permission_keys(self):
        data = {'username': 'unknown_permission', 'password': 'TestPassword123!',
                'password_confirm': 'TestPassword123!', 'modules': ['payments', 'unknown_key']}
        for form in (BackOfficeUserCreateForm(data), BackOfficeUserEditForm(data)):
            self.assertFalse(form.is_valid())
            self.assertEqual(form.errors.as_data()['modules'][0].code, 'invalid_choice')

    def test_grouped_cards_and_scoped_assets_render(self):
        response = self.client.get(reverse('system_admin_backoffice_user_create'))
        for heading in ('Collector Operations', 'Verification', 'Finance'):
            self.assertContains(response, f'<legend>{heading}</legend>')
        self.assertContains(response, 'data-permission-group', count=3)
        self.assertContains(response, 'backoffice_user_form.css')
        self.assertContains(response, 'backoffice_user_form.js')
        self.assertNotContains(response, 'style="')
        self.assertNotContains(response, 'type="password"')
        self.assertNotContains(response, 'data-client-search>')

    def test_client_search_only_appears_for_long_lists(self):
        from apps.accounts.models import Client
        Client.objects.bulk_create([Client(name=f'Client {i}', code=f'CARD-{i}') for i in range(9)])
        response = self.client.get(reverse('system_admin_backoffice_user_create'))
        self.assertContains(response, 'data-client-search>')
        self.assertContains(response, 'data-client-card', count=9)
        response = self.client.post(reverse('system_admin_backoffice_user_create'), {
            'username': 'invalid_email_user', 'email': 'bad-email',
            'modules': ['payments'], 'client_accesses': [str(Client.objects.first().pk)],
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'value="payments" checked')
        self.assertContains(response, 'Enter a valid email address.')
