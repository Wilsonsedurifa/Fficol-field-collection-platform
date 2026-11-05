from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Account, Client, Customer
from apps.audit.models import AuditEvent
from apps.collection.models import Payment
from apps.user.models import CollectorInvitation, CollectorProfile
from .services import get_dashboard_context, group_activity, range_window


class ActivityGroupingTests(SimpleTestCase):
    def event(self, minutes=0, actor_id=1, action='collector_invitation_resent'):
        return SimpleNamespace(actor_id=actor_id, actor=SimpleNamespace(username=f'admin{actor_id}'),
                               action=action, created_at=datetime(2026, 10, 20, 12, tzinfo=dt_timezone.utc) - timedelta(minutes=minutes),
                               entity_type='CollectorInvitation', entity_id='1', changes={})

    def test_consecutive_identical_events_group_without_mutating_entries(self):
        entries = [self.event(0), self.event(10), self.event(20)]
        result = group_activity(entries)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['count'], 3)
        self.assertEqual(result[0]['start'], entries[-1].created_at)
        self.assertEqual(result[0]['end'], entries[0].created_at)
        self.assertFalse(hasattr(entries[0], 'count'))

    def test_window_boundary_is_inclusive_and_does_not_chain(self):
        result = group_activity([self.event(0), self.event(30), self.event(31)])
        self.assertEqual([group['count'] for group in result], [2, 1])
        self.assertEqual([group['count'] for group in group_activity([self.event(0), self.event(20), self.event(40)])], [2, 1])

    def test_different_actors_and_actions_do_not_merge_or_skip_intervening_events(self):
        entries = [self.event(), self.event(1, actor_id=2), self.event(2), self.event(3, action='payment_verified')]
        self.assertEqual(len(group_activity(entries)), 4)
        self.assertEqual(group_activity([]), [])

    def test_range_defaults_and_singapore_midnight(self):
        now = datetime(2026, 10, 20, 16, 30, tzinfo=dt_timezone.utc)
        window = range_window('today', now)
        self.assertEqual(str(window['dates'][0]), '2026-10-21')
        self.assertEqual(window['start'].hour, 0)
        self.assertEqual(range_window('invalid', now)['days'], 7)
        self.assertEqual(range_window('30', now)['days'], 30)


class AdminDashboardTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser('dashboard_admin', password='Password123!')
        self.client.force_login(self.admin)

    def cards(self, context):
        return {card['label']: card for card in context['cards']}

    def account(self, status='open', currency='PHP'):
        client, _ = Client.objects.get_or_create(code='DASH', defaults={'name': 'Dashboard client'})
        customer = Customer.objects.create(client=client, display_name='Dashboard customer')
        return Account.objects.create(client=client, customer=customer, external_id=str(customer.pk),
                                      reference=f'DASH-{customer.pk}', balance_snapshot=100,
                                      balance_as_of=timezone.now(), status=status, currency=currency)

    def payment(self, account, amount, paid_at, verified=True):
        return Payment.objects.create(account=account, amount=amount, paid_at=paid_at, method='bank_transfer',
                                      status='verified' if verified else 'reported',
                                      verified_by=self.admin if verified else None,
                                      verified_at=timezone.now() if verified else None)

    def test_real_totals_pipeline_and_empty_stages(self):
        collector = get_user_model().objects.create_user('collector')
        CollectorProfile.objects.create(user=collector)
        CollectorProfile.objects.create(user=get_user_model().objects.create_user('suspended'), approval_status='suspended')
        self.account()
        self.account(status='resolved')
        self.account(status='disputed')
        Client.objects.create(code='INACTIVE', name='Inactive client', is_active=False)
        context = get_dashboard_context()
        cards = self.cards(context)
        for label, count in [('Accounts', 3), ('Collectors', 2), ('Users', 3), ('Clients', 2)]:
            self.assertEqual(cards[label]['value'], count)
            self.assertTrue(cards[label]['url'])
        self.assertEqual(context['collectors']['approved'], 1)
        self.assertEqual(context['collectors']['active'], 1)
        self.assertEqual(sum(stage['count'] for stage in context['pipeline']), 3)
        self.assertEqual([stage['key'] for stage in context['pipeline']], ['available', 'resolved', 'other'])
        self.assertIn('Offered', context['zero_stages'])

    def test_range_filters_collections_daily_sparkline_and_activity_not_totals(self):
        now = datetime(2026, 10, 20, 10, tzinfo=dt_timezone.utc)
        account = self.account()
        self.payment(account, Decimal('100.00'), now - timedelta(hours=1))
        self.payment(account, Decimal('200.00'), now - timedelta(days=5))
        self.payment(account, Decimal('300.00'), now - timedelta(days=20))
        self.payment(account, Decimal('999.00'), now, verified=False)
        event = AuditEvent.objects.create(actor=self.admin, action='collector_invitation_sent', entity_type='User', entity_id='1')
        AuditEvent.objects.filter(pk=event.pk).update(created_at=now - timedelta(days=20))
        with patch('apps.user.admin_portal.dashboard.services.timezone.now', return_value=now):
            for key, amount, length in [('today', 'PHP 100.00', 1), ('7', 'PHP 300.00', 7), ('30', 'PHP 600.00', 30)]:
                response = self.client.get(reverse('system_admin_dashboard'), {'range': key})
                self.assertEqual(response.status_code, 200)
                context = response.context
                self.assertEqual(context['collected'], amount)
                self.assertEqual(len(context['sparkline_data']), length)
                self.assertEqual(sum(context['sparkline_data']), Decimal(amount.split(' ')[1]))
                self.assertEqual(self.cards(context)['Accounts']['value'], 1)
                self.assertEqual(len(context['activity']), 1 if key == '30' else 0)

    def test_other_currencies_never_added_to_php(self):
        self.payment(self.account(currency='USD'), Decimal('20.00'), timezone.now())
        context = get_dashboard_context()
        self.assertEqual(context['collected'], 'PHP 0.00')
        self.assertEqual(context['other_currencies'], ['USD 20.00'])

    def test_attention_is_hidden_at_zero_and_uses_post_for_resend(self):
        response = self.client.get(reverse('system_admin_dashboard'))
        self.assertNotContains(response, 'awaiting activation')
        user = get_user_model().objects.create_user('not_activated', is_active=False)
        user.set_unusable_password()
        user.save()
        CollectorProfile.objects.create(user=user)
        CollectorInvitation.objects.create(user=user, email='pending@example.com', token_hash='a' * 64,
                                           expires_at=timezone.now() + timedelta(hours=72))
        response = self.client.get(reverse('system_admin_dashboard'))
        self.assertEqual(response.context['attention_total'], 1)
        self.assertContains(response, '1 user awaiting activation')
        self.assertContains(response, 'Resend invite')
        self.assertContains(response, 'method="post"')
        self.assertContains(response, 'csrfmiddlewaretoken')

    def test_timeline_groups_and_fixed_eight_queries(self):
        now = timezone.now()
        for offset in range(5):
            event = AuditEvent.objects.create(actor=self.admin, action='collector_invitation_resent', entity_type='User', entity_id=str(offset))
            AuditEvent.objects.filter(pk=event.pk).update(created_at=now - timedelta(minutes=offset))
        with self.assertNumQueries(8):
            context = get_dashboard_context()
        self.assertEqual(len(context['activity']), 1)
        self.assertEqual(context['activity'][0]['count'], 5)
        self.assertEqual(context['activity'][0]['sentence'], 'dashboard_admin resent an invitation')
        self.assertEqual(context['activity'][0]['color'], 'blue')
        response = self.client.get(reverse('system_admin_dashboard'))
        self.assertContains(response, '&times;5')
        self.assertNotContains(response, '<table')
        self.assertNotContains(response, 'style="')

    def test_latest_eight_groups_and_safe_unknown_actions(self):
        for index in range(12):
            AuditEvent.objects.create(actor=self.admin, action=f'event_{index}', entity_type='User', entity_id=str(index))
        context = get_dashboard_context()
        self.assertEqual(len(context['activity']), 8)
        self.assertEqual(context['activity'][0]['action'], 'event_11')
        self.admin.username = '<script>alert(1)</script>'
        self.admin.save()
        response = self.client.get(reverse('system_admin_dashboard'))
        self.assertContains(response, '&lt;script&gt;')
        self.assertNotContains(response, '<script>alert(1)</script>')

    def test_access_control_for_dashboard_and_drilldown(self):
        ordinary = get_user_model().objects.create_user('ordinary', is_staff=True)
        self.client.force_login(ordinary)
        for route in ['system_admin_dashboard', 'system_admin_dashboard_records']:
            self.assertEqual(self.client.get(reverse(route)).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get(reverse('system_admin_dashboard')).status_code, 302)

    def test_range_drilldown_matches_hero(self):
        self.payment(self.account(), Decimal('10.00'), timezone.now() - timedelta(days=20))
        self.payment(self.account(), Decimal('15.00'), timezone.now())
        response = self.client.get(reverse('system_admin_dashboard_records'), {'kind': 'payments', 'range': 'today'})
        self.assertEqual(len(response.context['records']), 1)
        self.assertIn('15.00', response.context['records'][0]['detail'])

    def test_empty_models_render_actual_zero(self):
        response = self.client.get(reverse('system_admin_dashboard'))
        self.assertContains(response, 'No collection accounts yet.')
        self.assertContains(response, 'No verified payments in this period.')
        self.assertContains(response, 'No activity recorded in this period.')
        self.assertEqual(response.context['collected'], 'PHP 0.00')
