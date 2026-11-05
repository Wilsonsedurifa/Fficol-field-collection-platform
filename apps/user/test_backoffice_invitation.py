import re
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.user.admin_portal.forms import BackOfficeUserCreateForm
from apps.user.admin_portal.services import create_backoffice_user_account, accept_backoffice_invitation
from apps.user.models import BackOfficeInvitation


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class BackOfficeInvitationTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser('invite_admin', password='StrongPassword123!')

    def invite(self):
        user = create_backoffice_user_account(actor=self.admin, username='staff', email='staff@example.com', module_keys=['payments'])
        token = re.search(r'/invitations/back-office/([^/]+)/', mail.outbox[-1].body).group(1)
        return user, token

    def test_required_email_and_no_admin_password_fields(self):
        form = BackOfficeUserCreateForm({'username': 'staff', 'modules': ['payments']})
        self.assertFalse(form.is_valid())
        self.assertIn('email', form.errors)
        self.assertNotIn('password', form.fields)

    def test_setup_verifies_email_and_activates_once(self):
        user, token = self.invite()
        self.assertFalse(user.is_active)
        self.assertFalse(user.has_usable_password())
        self.assertNotEqual(user.backoffice_invitations.get().token_hash, token)
        self.assertIn('Back Office', mail.outbox[-1].subject)
        url = reverse('backoffice_invitation_setup', args=[token])
        self.assertEqual(self.client.get(url).status_code, 200)
        response = self.client.post(url, {'password': 'StrongPassword123!', 'password_confirm': 'StrongPassword123!'})
        self.assertRedirects(response, reverse('login'))
        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertIsNotNone(user.email_verified_at)
        self.assertTrue(user.check_password('StrongPassword123!'))
        self.assertTrue(user.has_perm('collection.change_payment'))
        self.assertFalse(hasattr(user, 'collector_profile'))
        self.assertEqual(self.client.get(url).status_code, 400)

    def test_expired_revoked_and_invalid_tokens_do_not_activate(self):
        user, token = self.invite()
        invitation = user.backoffice_invitations.get()
        for status in ['revoked', 'accepted']:
            invitation.status = status
            invitation.save()
            with self.assertRaises(ValidationError):
                accept_backoffice_invitation(raw_token=token, password='StrongPassword123!')
        invitation.status = 'sent'
        invitation.expires_at = timezone.now() - timedelta(seconds=1)
        invitation.save()
        for candidate in [token, 'invalid']:
            with self.assertRaises(ValidationError):
                accept_backoffice_invitation(raw_token=candidate, password='StrongPassword123!')
        user.refresh_from_db()
        self.assertFalse(user.is_active)
