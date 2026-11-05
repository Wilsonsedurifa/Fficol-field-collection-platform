from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.http import QueryDict
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.user.models import BackOfficeInvitation, CollectorProfile
from .services import get_users_context


class UsersAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.admin = User.objects.create_superuser('list_admin', password='test-password')
        cls.active = User.objects.create_user(
            'active_collector', password='test-password', first_name='Alex',
            last_name='Rivera', email='alex@example.com', account_activated_at=timezone.now())
        cls.awaiting = User.objects.create_user(
            'pending_collector', is_active=False, first_name='Sam', email='sam@example.com')
        cls.other = User.objects.create_user('ordinary_user', password='test-password')
        CollectorProfile.objects.create(user=cls.active)
        CollectorProfile.objects.create(user=cls.awaiting)

    def setUp(self):
        self.client.force_login(self.admin)
        self.url = reverse('system_admin_users')

    def ids(self, **params):
        response = self.client.get(self.url, params)
        self.assertEqual(response.status_code, 200)
        return {user.pk for user in response.context['page_obj']}

    def test_role_filters(self):
        self.assertEqual(self.ids(role='admin'), {self.admin.pk})
        self.assertEqual(self.ids(role='collector'), {self.active.pk, self.awaiting.pk})

    def test_status_and_combined_filters(self):
        self.assertEqual(self.ids(status='awaiting'), {self.awaiting.pk})
        self.assertEqual(self.ids(role='collector', status='awaiting', q='sam@example'), {self.awaiting.pk})
        self.assertEqual(self.ids(role='admin', status='awaiting'), set())

    def test_search_username_full_name_and_email(self):
        for term in ['active_collector', 'Alex Rivera', 'alex@example.com']:
            with self.subTest(term=term):
                self.assertEqual(self.ids(q=term), {self.active.pk})

    def test_summary_counts_are_global(self):
        expected = dict(total=4, active=3, awaiting=1, collectors=2, admins=1)
        for params in [{}, {'role': 'admin'}, {'status': 'awaiting'}, {'q': 'missing'}]:
            response = self.client.get(self.url, params)
            self.assertEqual(response.context['counts'], expected)

    def test_protected_admin_has_no_menu_and_awaiting_has_resend(self):
        response = self.client.get(self.url, {'role': 'admin'})
        self.assertContains(response, 'Read-only')
        self.assertNotContains(response, 'data-dropdown')
        self.assertNotContains(response, 'Resend invite')
        response = self.client.get(self.url, {'status': 'awaiting'})
        self.assertContains(response, 'Resend invite')
        self.assertContains(response, reverse('system_admin_collector_resend_invitation', args=[self.awaiting.pk]))
        self.assertContains(response, 'csrfmiddlewaretoken')
        self.assertContains(response, 'ua-row-awaiting')

    def test_unknown_filters_are_ignored_and_empty_state_is_clear(self):
        self.assertEqual(self.ids(role='unknown', status='unknown'),
                         {self.admin.pk, self.active.pk, self.awaiting.pk, self.other.pk})
        response = self.client.get(self.url, {'q': 'missing'})
        self.assertContains(response, 'No users found. Try a different search.')
        self.assertContains(response, 'Clear filters')

    def test_presentation_helpers_and_query_budget(self):
        with self.assertNumQueries(3):
            context = get_users_context(QueryDict(''))
        active = next(user for user in context['page_obj'] if user.pk == self.active.pk)
        self.assertEqual((active.display_name, active.initials, active.status_key), ('Alex Rivera', 'AR', 'active'))
        self.assertContains(self.client.get(self.url), 'No email')

    def test_pagination_and_tabs_preserve_combined_filters(self):
        User = get_user_model()
        for index in range(21):
            user = User.objects.create_user(f'pending_extra_{index}', is_active=False)
            CollectorProfile.objects.create(user=user)
        response = self.client.get(self.url, {'role': 'collector', 'status': 'awaiting', 'q': 'pending'})
        context = response.context
        self.assertIn('role=collector', context['next_url'])
        self.assertIn('status=awaiting', context['next_url'])
        self.assertIn('q=pending', context['next_url'])
        self.assertIn('page=2', context['next_url'])
        collector_tab = next(tab for tab in context['filter_tabs'] if tab['label'] == 'Collector')
        self.assertIn('status=awaiting', collector_tab['url'])
        self.assertIn('q=pending', collector_tab['url'])

    def test_non_admin_cannot_open_users_page(self):
        self.client.force_login(self.other)
        self.assertIn(self.client.get(self.url).status_code, [302, 403])

    def test_suspended_and_deactivated_states_are_preserved(self):
        self.active.collector_profile.approval_status = 'suspended'
        self.active.collector_profile.save()
        response = self.client.get(self.url, {'q': self.active.username})
        self.assertContains(response, 'Suspended')
        self.assertContains(response, 'Deactivate account')
        self.assertNotContains(response, 'Resend invite')
        self.active.collector_account_disabled = True
        self.active.save()
        response = self.client.get(self.url, {'q': self.active.username})
        self.assertContains(response, 'Deactivated')
        self.assertContains(response, 'Activate account')

    def test_backoffice_setup_keeps_existing_edit_action(self):
        self.other.groups.add(Group.objects.get_or_create(name='Back Office')[0])
        self.other.is_active = False
        self.other.save()
        BackOfficeInvitation.objects.create(
            user=self.other, email='office@example.com', token_hash='office-token',
            expires_at=timezone.now() + timedelta(hours=72))
        response = self.client.get(self.url, {'q': self.other.username, 'status': 'awaiting'})
        self.assertContains(response, 'Awaiting setup')
        self.assertContains(response, 'Edit Back Office')
        self.assertNotContains(response, 'Resend invite')
        self.assertNotContains(response, 'data-dropdown')
