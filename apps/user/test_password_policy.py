from django.contrib.auth import get_user_model
from django.test import SimpleTestCase

from apps.user.admin_portal.forms import CollectorSetPasswordForm


class PasswordPolicyTests(SimpleTestCase):
    def form(self, password, confirmation=None):
        return CollectorSetPasswordForm(
            {'password': password, 'password_confirm': password if confirmation is None else confirmation},
            user=get_user_model()(username='collector'),
        )

    def test_normal_passwords_only_require_length_and_matching(self):
        for password in ['12345678', 'password', 'collector']:
            self.assertTrue(self.form(password).is_valid())

    def test_short_password_and_mismatch_are_rejected(self):
        self.assertFalse(self.form('1234').is_valid())
        form = self.form('12345678', '87654321')
        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors['password_confirm'], ['Passwords do not match. Please re-enter them.'])
