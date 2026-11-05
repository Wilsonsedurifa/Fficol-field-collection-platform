import csv
import io
from datetime import datetime, time, timedelta
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import Resolver404, resolve, reverse
from django.utils import timezone

from .admin_log import audit_context, csv_export, filtered_events
from .models import AuditEvent
from .presentation import action_presentation, humanize_changes


class AuditPresentationTests(SimpleTestCase):
    def test_boolean_and_none(self):
        chips = humanize_changes({'invitation_sent': True, 'disabled': False, 'reason': None})
        self.assertEqual([chip['after'] for chip in chips], ['Yes', 'No', 'None'])
        self.assertEqual(chips[0]['label'], 'Invitation sent')

    def test_before_after_and_status_colors(self):
        chip = humanize_changes({'approval_status': {'before': 'pending', 'after': 'approved'}})[0]
        self.assertTrue(chip['pair'])
        self.assertEqual((chip['before'], chip['after']), ('pending', 'approved'))
        self.assertEqual((chip['before_color'], chip['after_color']), ('amber', 'green'))
        self.assertEqual(humanize_changes({'status': 'rejected'})[0]['after_color'], 'red')

    def test_nested_lists_empty_and_long_values(self):
        self.assertEqual(humanize_changes({}), [])
        self.assertEqual(humanize_changes(None), [])
        self.assertEqual(humanize_changes({'value': {'nested': [True, None]}})[0]['after'], '{"nested": [true, null]}')
        self.assertEqual(humanize_changes({'items': [1, 2]})[0]['after'], '[1, 2]')
        self.assertEqual(len(humanize_changes({'note': 'x' * 500})[0]['after']), 160)

    def test_mapping_and_unknown_fallback(self):
        self.assertEqual(action_presentation('system_admin_user_created')['label'], 'User created')
        self.assertEqual(action_presentation('system_admin_user_created')['color'], 'blue')
        self.assertEqual(action_presentation('future_action')['color'], 'gray')
        self.assertEqual(action_presentation('future_action')['label'], 'Future action')


class AdminAuditLogTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.admin = User.objects.create_superuser('audit_admin', password='test-password')
        cls.other = User.objects.create_user('another_actor', password='test-password')
        cls.today = timezone.localdate()
        cls.created = cls.event('system_admin_user_created', cls.admin,
                                {'invitation_sent': True}, cls.today)
        cls.changed = cls.event('system_admin_collector_status_changed', cls.other,
                                {'approval_status': {'before': 'pending', 'after': 'approved'}}, cls.today - timedelta(days=3))
        cls.old = cls.event('collector_invitation_resent', cls.admin, {}, cls.today - timedelta(days=20))
        cls.ancient = cls.event('system_admin_user_updated', cls.admin, {}, cls.today - timedelta(days=40))

    @classmethod
    def event(cls, action, actor, changes, day):
        event = AuditEvent.objects.create(actor=actor, action=action, entity_type='User',
                                          entity_id=str(cls.other.pk), changes=changes)
        # Fixture timestamps only; production audit paths remain read-only.
        AuditEvent.objects.filter(pk=event.pk).update(created_at=timezone.make_aware(datetime.combine(day, time(12))))
        return event

    def setUp(self):
        self.client.force_login(self.admin)
        self.url = reverse('system_admin_audit')
        self.export = reverse('system_admin_audit_export')

    def ids(self, **params):
        response = self.client.get(self.url, params)
        self.assertEqual(response.status_code, 200)
        return {row['event'].pk for row in response.context['rows']}

    def test_date_ranges_and_custom_inclusive_end(self):
        self.assertEqual(self.ids(), {self.created.pk})
        self.assertEqual(self.ids(range='7d'), {self.created.pk, self.changed.pk})
        self.assertEqual(self.ids(range='30d'), {self.created.pk, self.changed.pk, self.old.pk})
        day = (self.today - timedelta(days=3)).isoformat()
        self.assertEqual(self.ids(range='custom', **{'from': day, 'to': day}), {self.changed.pk})

    def test_action_actor_and_combined_filters(self):
        self.assertEqual(self.ids(action='system_admin_user_created'), {self.created.pk})
        self.assertEqual(self.ids(range='30d', actor=self.other.username), {self.changed.pk})
        self.assertEqual(self.ids(range='7d', actor=self.other.username,
                                  action='system_admin_collector_status_changed'), {self.changed.pk})

    def test_invalid_params_are_ignored(self):
        self.assertEqual(self.ids(range='bad', action='not-real', actor='x' * 151), {self.created.pk})
        for params in [{'from': '2026-02-30', 'to': 'bad'}, {'from': '2099-01-01', 'to': '2000-01-01'}]:
            self.assertEqual(len(self.ids(range='custom', **params)), 4)
        self.assertEqual(self.client.get(self.url, {'page': 'bad'}).status_code, 200)

    def test_summary_is_filtered_and_raw_data_is_escaped(self):
        response = self.client.get(self.url, {'range': '7d'})
        self.assertEqual([card['value'] for card in response.context['summary_cards']], [2, 2, 1, 1])
        response = self.client.get(self.url, {'actor': self.other.username, 'range': '7d'})
        self.assertEqual([card['value'] for card in response.context['summary_cards']], [1, 1, 1, 0])
        self.event('system_admin_user_updated', self.admin, {'note': '<script>alert(1)</script>'}, self.today)
        response = self.client.get(self.url)
        self.assertContains(response, '&lt;script&gt;')
        self.assertNotContains(response, '<script>alert(1)</script>')
        self.assertContains(response, 'View raw data')

    def test_pagination_and_removable_chips_keep_filters(self):
        for _ in range(26):
            self.event('system_admin_user_created', self.admin, {}, self.today)
        params = {'range': 'custom', 'from': self.today.isoformat(), 'to': self.today.isoformat(),
                  'action': 'system_admin_user_created', 'actor': self.admin.username}
        response = self.client.get(self.url, params)
        query = parse_qs(urlsplit(response.context['next_url']).query)
        for key, value in params.items():
            self.assertEqual(query[key], [value])
        actor_chip = next(chip for chip in response.context['active_filters'] if chip['label'].startswith('Actor:'))
        chip_query = parse_qs(urlsplit(actor_chip['url']).query)
        self.assertNotIn('actor', chip_query)
        self.assertEqual(chip_query['action'], ['system_admin_user_created'])

    def test_local_day_boundaries(self):
        with timezone.override(ZoneInfo('Asia/Manila')):
            midnight = timezone.make_aware(datetime.combine(timezone.localdate(), time.min))
            event = AuditEvent.objects.create(action='system_admin_user_updated', entity_type='User', entity_id='0')
            AuditEvent.objects.filter(pk=event.pk).update(created_at=midnight - timedelta(seconds=1))
            self.assertNotIn(event.pk, filtered_events({})[0].values_list('pk', flat=True))
            AuditEvent.objects.filter(pk=event.pk).update(created_at=midnight)
            self.assertIn(event.pk, filtered_events({})[0].values_list('pk', flat=True))

    def test_query_budget_does_not_grow_with_rows(self):
        for _ in range(10):
            self.event('system_admin_user_updated', self.admin, {}, self.today)
        with self.assertNumQueries(5):
            audit_context({})

    def test_csv_columns_filter_and_formula_escaping(self):
        formula_actor = get_user_model().objects.create_user('=SUM(1,2)')
        self.event('system_admin_user_updated', formula_actor, {'is_active': True}, self.today)
        response = self.client.get(self.export, {'actor': formula_actor.username})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.streaming)
        rows = list(csv.reader(io.StringIO(b''.join(response.streaming_content).decode())))
        self.assertEqual(rows[0], ['timestamp', 'actor', 'action code', 'entity', 'changes'])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][1], "'=SUM(1,2)")
        self.assertEqual(rows[1][4], '{"is_active": true}')
        for prefix in ['+', '-', '@', '\t=']:
            formula_actor.username = prefix + 'formula'
            formula_actor.save()
            response = csv_export({'actor': formula_actor.username})
            self.assertIn("'" + formula_actor.username, b''.join(response.streaming_content).decode())

    def test_read_only_routes_and_permissions(self):
        for path in [self.url, self.export]:
            for method in ['post', 'put', 'patch', 'delete']:
                self.assertEqual(getattr(self.client, method)(path).status_code, 405)
        for suffix in ['new/', '1/edit/', '1/delete/']:
            with self.assertRaises(Resolver404):
                resolve(self.url + suffix)
        self.client.force_login(self.other)
        for path in [self.url, self.export]:
            self.assertEqual(self.client.get(path).status_code, 403)
        self.client.logout()
        for path in [self.url, self.export]:
            self.assertEqual(self.client.get(path).status_code, 302)
