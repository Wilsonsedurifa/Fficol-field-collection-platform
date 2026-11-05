"""Database checks for monitoring permissions and client isolation."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from django.db import connection
from django.test.utils import CaptureQueriesContext
from urllib.parse import parse_qs, urlsplit

from apps.assignments.models import AccountAssignment
from apps.assignments.tests import offer_fixture
from apps.user.models import ClientAccess
from apps.user.models import CollectorProfile
from apps.accounts.models import Client, Customer, Account
from apps.activities.models import FollowUpTask, FieldActivity
from apps.collection.models import Payment

from .queries import scoped_collectors, workload


class CollectorMonitoringTests(TestCase):
    def test_activity_sort_uses_same_timestamp_as_display(self):
        user = get_user_model().objects.create_user(username="idle-sort-fixture")
        profile = CollectorProfile.objects.create(user=user)
        ClientAccess.objects.create(user=user, client=self.client_obj)
        payment = Payment.objects.create(account=self.account, reported_by=self.collector,
            amount=10, paid_at=timezone.now(), method="bank_transfer")
        self.client.force_login(self.viewer)
        response = self.client.get(self.url)
        self.assertEqual(response.context["rows"][0].pk, profile.pk)
        response = self.client.get(self.url, {"sort": "activity", "dir": "desc"})
        first = response.context["rows"][0]
        self.assertEqual(first.pk, self.profile.pk)
        self.assertEqual(first.sort_activity, first.last_activity)
        self.assertEqual(first.last_activity, payment.created_at)
        self.assertContains(response, "ago")

    def test_sort_and_card_links_preserve_filters_and_invalid_sort_is_safe(self):
        self.client.force_login(self.viewer)
        response = self.client.get(self.url, {"q": self.collector.username, "client": self.client_obj.pk,
            "sort": "overdue", "dir": "desc", "approval": "approved"})
        link = parse_qs(urlsplit(response.context["card_links"]["online"]).query)
        self.assertEqual(link["q"], [self.collector.username])
        self.assertEqual(link["sort"], ["overdue"])
        self.assertEqual(link["availability"], ["online"])
        self.assertEqual(link["approval"], ["approved"])
        reset = parse_qs(urlsplit(response.context["card_links"]["approved"]).query)
        self.assertNotIn("availability", reset)
        self.assertEqual(reset["client"], [str(self.client_obj.pk)])
        for sort in ("collector", "active", "activity", "overdue", "unknown"):
            result = self.client.get(self.url, {"sort": sort, "dir": "invalid"})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.context["sort"], "default")

    def test_default_sort_puts_overdue_first_and_reasons_are_visible(self):
        self.assignment()
        user = get_user_model().objects.create_user(username="aaa-idle-fictional")
        CollectorProfile.objects.create(user=user)
        ClientAccess.objects.create(user=user, client=self.client_obj)
        self.client.force_login(self.viewer)
        result = self.client.get(self.url)
        self.assertEqual(result.context["rows"][0].pk, self.profile.pk)
        self.assertContains(result, "First action")
        result = self.client.get(self.url, {"sort": "collector", "dir": "asc"})
        self.assertEqual(result.context["rows"][0].user_id, user.pk)
        result = self.client.get(self.url, {"sort": "active", "dir": "desc"})
        self.assertEqual(result.context["rows"][0].pk, self.profile.pk)

    def test_query_count_does_not_grow_per_collector(self):
        self.client.force_login(self.viewer)
        with CaptureQueriesContext(connection) as single:
            self.client.get(self.url)
        for index in range(6):
            user = get_user_model().objects.create_user(username=f"query-fixture-{index}")
            CollectorProfile.objects.create(user=user)
            ClientAccess.objects.create(user=user, client=self.client_obj)
        with CaptureQueriesContext(connection) as multiple:
            self.client.get(self.url)
        self.assertEqual(len(single), len(multiple))

    def assignment(self):
        """Create a fictional assignment whose first-action deadline has passed."""
        item = AccountAssignment.objects.create(
            account=self.account, collector=self.profile, target_address=self.account.collection_address,
            target_position=self.account.collection_address.position, commission_rule=self.account.commission_rule,
            first_action_due_at=timezone.now() + timedelta(hours=1), policy_snapshot={},
        )
        old = timezone.now() - timedelta(days=2)
        AccountAssignment.objects.filter(pk=item.pk).update(created_at=old, first_action_due_at=old + timedelta(hours=1))
        return item

    def test_requested_followup_is_not_overdue_and_submitted_contact_counts(self):
        item = self.assignment()
        now = timezone.now()
        FieldActivity.objects.create(account=self.account, assignment=item, recorded_by=self.collector,
            kind="phone", status="submitted", outcome="contacted", occurred_at=now, ended_at=now,
            submission_fingerprint="test", notes="Fictional test call")
        FollowUpTask.objects.create(account=self.account, assignment=item, assignee=self.collector,
            kind="revisit", status="requested", due_at=now - timedelta(hours=1))
        _, clients = scoped_collectors(self.viewer)
        result = workload(clients).get(pk=item.pk)
        self.assertFalse(result.first_overdue)
        self.assertFalse(result.followup_overdue)

    def test_two_overdue_reasons_count_one_assignment(self):
        item = self.assignment()
        now = timezone.now()
        FollowUpTask.objects.create(account=self.account, assignment=item, assignee=self.collector,
            kind="revisit", status="pending", available_from=now - timedelta(hours=3),
            due_at=now - timedelta(hours=1), authorized_by=self.viewer, authorized_at=now)
        self.client.force_login(self.viewer)
        response = self.client.get(self.url)
        self.assertEqual(response.context["summary"]["overdue"], 1)
        self.assertEqual(response.context["rows"][0].overdue_count, 1)

    def test_foreign_client_payment_does_not_leak_into_last_activity(self):
        foreign = Client.objects.create(code="MONITOR-OTHER", name="Fictional hidden client")
        customer = Customer.objects.create(client=foreign, display_name="Fictional hidden customer")
        account = Account.objects.create(client=foreign, customer=customer, external_id="HIDDEN",
            reference="HIDDEN-ACCOUNT", balance_snapshot=100, balance_as_of=timezone.now())
        Payment.objects.create(account=account, reported_by=self.collector, amount=10,
            paid_at=timezone.now(), method="bank_transfer")
        self.client.force_login(self.viewer)
        response = self.client.get(self.url)
        self.assertIsNone(response.context["rows"][0].last_activity)
        response = self.client.get(self.url, {"client": foreign.pk})
        self.assertEqual(response.context["page_obj"].paginator.count, 0)
        self.assertNotContains(response, foreign.name)

    def test_filters_summary_and_pagination_preserve_parameters(self):
        for number in range(21):
            user = get_user_model().objects.create_user(username=f"fictional-monitor-{number:02}")
            CollectorProfile.objects.create(user=user, approval_status="approved", approved_by=self.viewer,
                approved_at=timezone.now(), availability="online")
            ClientAccess.objects.create(user=user, client=self.client_obj)
        self.client.force_login(self.viewer)
        params = {"q": "fictional-monitor", "availability": "online", "approval": "approved", "client": str(self.client_obj.pk)}
        first = self.client.get(self.url, params)
        self.assertEqual(first.context["summary"]["online"], 21)
        self.assertEqual(first.context["page_obj"].paginator.count, 21)
        self.assertEqual(len(first.context["rows"]), 20)
        for key, value in params.items():
            self.assertIn(f"{key}={value}", first.context["pagination_query"])
        second = self.client.get(self.url, {**params, "page": 2})
        self.assertEqual(len(second.context["rows"]), 1)
        self.assertEqual(first.context["summary"], second.context["summary"])
        self.assertContains(second, "Previous")

    def test_single_page_and_empty_state(self):
        self.client.force_login(self.viewer)
        response = self.client.get(self.url)
        self.assertContains(response, "1 collector")
        self.assertNotContains(response, 'aria-label="Collector pages"')
        response = self.client.get(self.url, {"q": "nonexistent-fictional-collector"})
        self.assertContains(response, "Clear filters")
        self.assertContains(response, "0 collectors")

    def setUp(self):
        self.collector, self.profile, self.client_obj, self.account = offer_fixture()
        self.viewer = get_user_model().objects.create_user(username="monitor-viewer")
        self.permission = Permission.objects.get(content_type__app_label="user", codename="view_collector_monitoring")
        self.viewer.user_permissions.add(self.permission)
        ClientAccess.objects.create(user=self.viewer, client=self.client_obj)
        self.url = reverse("backoffice_collectors")

    def test_permission_and_scoped_page_with_no_activity(self):
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.client.force_login(self.collector)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.client.force_login(self.viewer)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.collector.username)
        self.assertContains(response, "No recorded activity")
        self.assertEqual(response.context["page_obj"].paginator.count, 1)

    def test_revoked_scope_hides_collector(self):
        ClientAccess.objects.filter(user=self.viewer).update(is_active=False)
        profiles, _ = scoped_collectors(self.viewer)
        self.assertFalse(profiles.exists())

    def test_backoffice_identity_excluded_even_with_collector_profile(self):
        self.collector.user_permissions.add(self.permission)
        profiles, _ = scoped_collectors(self.viewer)
        self.assertFalse(profiles.exists())

    def test_first_action_deadline_and_scope(self):
        assignment = AccountAssignment.objects.create(
            account=self.account, collector=self.profile,
            target_address=self.account.collection_address,
            target_position=self.account.collection_address.position,
            commission_rule=self.account.commission_rule,
            first_action_due_at=timezone.now() + timedelta(hours=1), policy_snapshot={},
        )
        # Preserve the constraint that the deadline follows assignment creation.
        old = timezone.now() - timedelta(days=2)
        AccountAssignment.objects.filter(pk=assignment.pk).update(created_at=old, first_action_due_at=old + timedelta(hours=1))
        _, clients = scoped_collectors(self.viewer)
        item = workload(clients).get(pk=assignment.pk)
        self.assertTrue(item.first_overdue)
        self.client.force_login(self.viewer)
        response = self.client.get(self.url, {"overdue": "1"})
        self.assertEqual(response.context["summary"]["overdue"], 1)
        self.assertContains(response, "First action overdue")
        self.assertNotContains(response, reverse("backoffice_account_assignment_detail", args=[self.account.pk]))
