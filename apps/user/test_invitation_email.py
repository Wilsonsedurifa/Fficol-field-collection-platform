from types import SimpleNamespace

from django.core import mail
from django.test import SimpleTestCase, RequestFactory, override_settings
from django.urls import reverse

from apps.user.invitations import send_invitation_email


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend', SITE_URL='https://ficol.example/')
class InvitationEmailTests(SimpleTestCase):
    def invitation(self, name='Wilson Sedurifa'):
        return SimpleNamespace(user=SimpleNamespace(get_full_name=lambda: name, username='collector'), email='collector@example.com')

    def test_multipart_button_url_and_plain_text_security_notice(self):
        send_invitation_email(self.invitation(), 'safe-test-token')
        message = mail.outbox[-1]
        url = 'https://ficol.example' + reverse('collector_invitation_setup', kwargs={'token': 'safe-test-token'})
        self.assertEqual(message.content_subtype, 'plain')
        self.assertEqual(message.alternatives[0].mimetype, 'text/html')
        html = message.alternatives[0].content
        self.assertIn(f'href="{url}"', html)
        self.assertIn('Activate Account', html)
        self.assertIn(url, message.body)
        for notice in ['Security Notice:', '72 hours', 'one-time use only', 'Never share this link', 'If you did not request or expect this account']:
            self.assertIn(notice, message.body)
            self.assertIn(notice, html)
        self.assertIn('Hello Wilson Sedurifa,', message.body)
        self.assertIn('multipart/alternative', message.message().get_content_type())

    def test_request_url_and_autoescaped_name(self):
        request = RequestFactory().get('/', secure=True, HTTP_HOST='testserver')
        send_invitation_email(self.invitation('<b>WIlson</b>'), 'another-token', request=request)
        html = mail.outbox[-1].alternatives[0].content
        self.assertIn('https://testserver/invitations/collector/another-token/', html)
        self.assertIn('&lt;b&gt;WIlson&lt;/b&gt;', html)
        self.assertNotIn('<b>WIlson</b>', html)

    def test_backoffice_sender_keeps_its_route_and_account_label(self):
        send_invitation_email(self.invitation(), 'staff-token', account_type='Back Office', setup_route='backoffice_invitation_setup')
        message = mail.outbox[-1]
        self.assertIn('Back Office account', message.body)
        self.assertIn('/invitations/back-office/staff-token/', message.alternatives[0].content)
