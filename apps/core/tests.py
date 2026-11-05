from decimal import Decimal
import json
import os
import struct

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser, Permission
from django.contrib.gis.geos import Point
from django.core.exceptions import PermissionDenied
from django.template.loader import render_to_string
from django.test import Client as HttpClient, RequestFactory, SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Account, Client, Customer, CustomerAddress
from apps.assignments.models import AccountAssignment, AccountOffer
from apps.collection.models import CommissionRule
from apps.core.context_processors import unread_notifications
from apps.core.models import Notification
from apps.user.models import ClientAccess, CollectorProfile

User = get_user_model()


class NotificationInboxTests(TestCase):
    def setUp(self):
        self.collector_user = User.objects.create_user(
            username="collector1", password="password123", email="c1@example.test"
        )
        self.other_collector_user = User.objects.create_user(
            username="collector2", password="password123", email="c2@example.test"
        )
        self.backoffice_user = User.objects.create_user(
            username="backoffice1", password="password123", email="bo1@example.test"
        )
        self.no_portal_user = User.objects.create_user(
            username="noportal", password="password123", email="no@example.test"
        )

        self.admin_user = User.objects.create_superuser(
            username="admin1", password="password123", email="admin@example.test"
        )

        perm = Permission.objects.get(codename="validate_account_information")
        self.backoffice_user.user_permissions.add(perm)

        now = timezone.now()
        self.collector_profile = CollectorProfile.objects.create(
            user=self.collector_user,
            approval_status=CollectorProfile.ApprovalStatus.APPROVED,
            availability=CollectorProfile.Availability.ONLINE,
            approved_by=self.admin_user,
            approved_at=now,
        )
        self.other_collector_profile = CollectorProfile.objects.create(
            user=self.other_collector_user,
            approval_status=CollectorProfile.ApprovalStatus.APPROVED,
            availability=CollectorProfile.Availability.ONLINE,
            approved_by=self.admin_user,
            approved_at=now,
        )

        self.client = Client.objects.create(code="CL01", name="Client One", is_active=True)
        ClientAccess.objects.create(user=self.collector_user, client=self.client, is_active=True)
        ClientAccess.objects.create(user=self.backoffice_user, client=self.client, is_active=True)

        self.customer = Customer.objects.create(
            client=self.client, external_id="CUST-001", display_name="Juan Cruz"
        )
        self.address = CustomerAddress.objects.create(
            customer=self.customer,
            kind="residential",
            address_line="123 Test St",
            position=Point(120.9850, 14.6000, srid=4326),
        )
        self.rule = CommissionRule.objects.create(
            client=self.client,
            code="CR01",
            version=1,
            name="Default Rule",
            kind="percentage",
            percentage=Decimal("5.0000"),
            earning_basis="verified_payment",
        )
        self.account = Account.objects.create(
            client=self.client,
            customer=self.customer,
            external_id="ACC-001",
            reference="REF-001",
            balance_snapshot=Decimal("1000.00"),
            balance_as_of=timezone.now(),
            collection_address=self.address,
            commission_rule=self.rule,
        )

    def test_anonymous_user_redirected_to_login(self):
        client = HttpClient()
        response = client.get(reverse("notifications:inbox"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)

    def test_user_without_portal_access_forbidden(self):
        client = HttpClient()
        client.force_login(self.no_portal_user)
        response = client.get(reverse("notifications:inbox"))
        self.assertEqual(response.status_code, 403)

    def test_inbox_shows_only_authenticated_recipient_notifications(self):
        n1 = Notification.objects.create(
            recipient=self.collector_user,
            kind="offer",
            message="Offer for collector 1",
            dedup_key="key1",
        )
        n2 = Notification.objects.create(
            recipient=self.other_collector_user,
            kind="offer",
            message="Offer for collector 2",
            dedup_key="key2",
        )

        client = HttpClient()
        client.force_login(self.collector_user)
        response = client.get(reverse("notifications:inbox"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Offer for collector 1")
        self.assertNotContains(response, "Offer for collector 2")

        items = [i["notification"] for i in response.context["notification_items"]]
        self.assertIn(n1, items)
        self.assertNotIn(n2, items)

    def test_unread_count_recipient_scoping_and_context_processor(self):
        Notification.objects.create(
            recipient=self.collector_user,
            kind="system",
            message="Unread for collector 1",
            dedup_key="unread_c1",
        )
        Notification.objects.create(
            recipient=self.collector_user,
            kind="system",
            message="Read for collector 1",
            read_at=timezone.now(),
            dedup_key="read_c1",
        )
        Notification.objects.create(
            recipient=self.other_collector_user,
            kind="system",
            message="Unread for collector 2",
            dedup_key="unread_c2",
        )

        factory = RequestFactory()

        # Collector 1 request
        req1 = factory.get("/")
        req1.user = self.collector_user
        ctx1 = unread_notifications(req1)
        self.assertEqual(ctx1["unread_notifications_count"], 1)

        # Collector 2 request
        req2 = factory.get("/")
        req2.user = self.other_collector_user
        ctx2 = unread_notifications(req2)
        self.assertEqual(ctx2["unread_notifications_count"], 1)

        # Anonymous request
        req_anon = factory.get("/")
        req_anon.user = AnonymousUser()
        ctx_anon = unread_notifications(req_anon)
        self.assertEqual(ctx_anon["unread_notifications_count"], 0)

    def test_anonymous_context_is_safe_and_query_free(self):
        factory = RequestFactory()
        req_anon = factory.get("/")
        req_anon.user = AnonymousUser()
        with self.assertNumQueries(0):
            ctx_anon = unread_notifications(req_anon)
        self.assertEqual(ctx_anon["unread_notifications_count"], 0)
        self.assertEqual(ctx_anon["notification_dropdown_items"], [])
        self.assertIsNone(ctx_anon["notification_dropdown_offer_summary"])

    def test_navbar_context_recipient_scoped_and_at_most_three_visual_rows(self):
        for i in range(5):
            Notification.objects.create(
                recipient=self.collector_user,
                kind="system",
                message=f"Collector msg {i}",
                dedup_key=f"c1_msg_{i}",
            )
        for i in range(2):
            Notification.objects.create(
                recipient=self.other_collector_user,
                kind="system",
                message=f"Other collector msg {i}",
                dedup_key=f"c2_msg_{i}",
            )

        factory = RequestFactory()
        req = factory.get("/")
        req.user = self.collector_user

        ctx = unread_notifications(req)
        self.assertEqual(ctx["unread_notifications_count"], 5)
        # At most three preview rows
        self.assertEqual(len(ctx["notification_dropdown_items"]), 3)
        for item in ctx["notification_dropdown_items"]:
            self.assertEqual(item["notification"].recipient, self.collector_user)
            self.assertIn("Collector msg", item["notification"].message)

    def test_collector_qualifying_offers_become_exactly_one_dropdown_summary_and_sources_omitted(self):
        offer1 = self._create_offer()
        offer2 = self._create_offer()
        n_offer1 = Notification.objects.create(
            recipient=self.collector_user,
            kind="offer",
            offer=offer1,
            message="Offer 1 notice",
            dedup_key="dd_qual_1",
        )
        n_offer2 = Notification.objects.create(
            recipient=self.collector_user,
            kind="offer",
            offer=offer2,
            message="Offer 2 notice",
            dedup_key="dd_qual_2",
        )
        n_sys1 = Notification.objects.create(
            recipient=self.collector_user,
            kind="system",
            message="System notice 1",
            dedup_key="dd_sys_1",
        )
        n_sys2 = Notification.objects.create(
            recipient=self.collector_user,
            kind="system",
            message="System notice 2",
            dedup_key="dd_sys_2",
        )
        n_sys3 = Notification.objects.create(
            recipient=self.collector_user,
            kind="system",
            message="System notice 3",
            dedup_key="dd_sys_3",
        )

        factory = RequestFactory()
        req = factory.get("/")
        req.user = self.collector_user

        ctx = unread_notifications(req)
        # 2 offer notices + 3 system notices = 5 total unread
        self.assertEqual(ctx["unread_notifications_count"], 5)
        # Offer summary counts as 1 visual row
        self.assertIsNotNone(ctx["notification_dropdown_offer_summary"])
        self.assertEqual(ctx["notification_dropdown_offer_summary"]["count"], 2)
        # Remaining slots for other notifications = 2 (total 3 visual rows max)
        self.assertEqual(len(ctx["notification_dropdown_items"]), 2)
        # Source offer notifications must be omitted from items
        item_notif_ids = {item["notification"].pk for item in ctx["notification_dropdown_items"]}
        self.assertNotIn(n_offer1.pk, item_notif_ids)
        self.assertNotIn(n_offer2.pk, item_notif_ids)

    def test_opening_dropdown_offer_summary_marks_only_qualifying_current_offer_notices_read(self):
        offer1 = self._create_offer()
        offer2 = self._create_offer()
        n_offer1 = Notification.objects.create(
            recipient=self.collector_user,
            kind="offer",
            offer=offer1,
            message="Dropdown offer 1",
            dedup_key="dd_open_1",
        )
        n_offer2 = Notification.objects.create(
            recipient=self.collector_user,
            kind="offer",
            offer=offer2,
            message="Dropdown offer 2",
            dedup_key="dd_open_2",
        )

        # Non-pending offer
        declined_offer = self._create_offer(status="declined")
        n_declined = Notification.objects.create(
            recipient=self.collector_user,
            kind="offer",
            offer=declined_offer,
            message="Declined offer",
            dedup_key="dd_open_declined",
        )

        # Expired offer
        expired_offer = self._create_offer()
        past_created = timezone.now() - timezone.timedelta(hours=2)
        past_expired = timezone.now() - timezone.timedelta(hours=1)
        AccountOffer.objects.filter(pk=expired_offer.pk).update(created_at=past_created, expires_at=past_expired)
        expired_offer.refresh_from_db()
        n_expired = Notification.objects.create(
            recipient=self.collector_user,
            kind="offer",
            offer=expired_offer,
            message="Expired offer",
            dedup_key="dd_open_expired",
        )

        # Out-of-scope client offer
        client2 = Client.objects.create(code="CL02", name="Client Two", is_active=True)
        cust2 = Customer.objects.create(client=client2, external_id="CUST-002", display_name="Other Client")
        addr2 = CustomerAddress.objects.create(
            customer=cust2, kind="residential", address_line="456 Other St",
            position=Point(120.9850, 14.6000, srid=4326)
        )
        acc2 = Account.objects.create(
            client=client2, customer=cust2, external_id="ACC-002", reference="REF-002",
            balance_snapshot=Decimal("2000.00"), balance_as_of=timezone.now(),
            collection_address=addr2, commission_rule=self.rule
        )
        out_of_scope_offer = self._create_offer(account=acc2)
        n_out_of_scope = Notification.objects.create(
            recipient=self.collector_user,
            kind="offer",
            offer=out_of_scope_offer,
            message="Out of scope offer",
            dedup_key="dd_open_out_of_scope",
        )

        # Unrelated system notice
        n_system = Notification.objects.create(
            recipient=self.collector_user,
            kind="system",
            message="System announcement",
            dedup_key="dd_open_system",
        )

        client = HttpClient()
        client.force_login(self.collector_user)

        resp = client.post(reverse("notifications:open_offers"))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, reverse("assignments:account_offers"))

        n_offer1.refresh_from_db()
        n_offer2.refresh_from_db()
        n_declined.refresh_from_db()
        n_expired.refresh_from_db()
        n_out_of_scope.refresh_from_db()
        n_system.refresh_from_db()

        self.assertIsNotNone(n_offer1.read_at)
        self.assertIsNotNone(n_offer2.read_at)
        self.assertIsNone(n_declined.read_at)
        self.assertIsNone(n_expired.read_at)
        self.assertIsNone(n_out_of_scope.read_at)
        self.assertIsNone(n_system.read_at)

    def test_dropdown_renders_for_both_collector_and_backoffice_navbars(self):
        client = HttpClient()

        # Collector navbar with unread items
        client.force_login(self.collector_user)
        n_col = Notification.objects.create(
            recipient=self.collector_user,
            kind="system",
            message="Collector alert for dropdown",
            dedup_key="col_dd_render",
        )
        resp_col = client.get(reverse("notifications:inbox"))
        self.assertEqual(resp_col.status_code, 200)
        self.assertContains(resp_col, "top-notif-dropdown")
        self.assertContains(resp_col, "top-notif-menu")
        self.assertContains(resp_col, "Show all notifications")
        self.assertContains(resp_col, "Collector alert for dropdown")

        # Collector empty state
        n_col.read_at = timezone.now()
        n_col.save()
        resp_col_empty = client.get(reverse("notifications:inbox"))
        self.assertContains(resp_col_empty, "No unread notifications")
        self.assertContains(resp_col_empty, "Show all notifications")

        # Back Office navbar with unread items
        client.force_login(self.backoffice_user)
        n_bo = Notification.objects.create(
            recipient=self.backoffice_user,
            kind="system",
            message="Backoffice alert for dropdown",
            dedup_key="bo_dd_render",
        )
        resp_bo = client.get(reverse("notifications:inbox"))
        self.assertEqual(resp_bo.status_code, 200)
        self.assertContains(resp_bo, "bo-notif-dropdown")
        self.assertContains(resp_bo, "bo-notif-menu")
        self.assertContains(resp_bo, "Show all notifications")
        self.assertContains(resp_bo, "Backoffice alert for dropdown")

        # Back Office empty state
        n_bo.read_at = timezone.now()
        n_bo.save()
        resp_bo_empty = client.get(reverse("notifications:inbox"))
        self.assertContains(resp_bo_empty, "No unread notifications")
        self.assertContains(resp_bo_empty, "Show all notifications")

    def test_backoffice_dropdown_shows_newest_three_without_collector_offer_behavior(self):
        for i in range(4):
            Notification.objects.create(
                recipient=self.backoffice_user,
                kind="system",
                message=f"BO Notice {i}",
                dedup_key=f"bo_notice_{i}",
            )
        factory = RequestFactory()
        req = factory.get("/")
        req.user = self.backoffice_user

        ctx = unread_notifications(req)
        self.assertEqual(ctx["unread_notifications_count"], 4)
        self.assertIsNone(ctx["notification_dropdown_offer_summary"])
        self.assertEqual(len(ctx["notification_dropdown_items"]), 3)

    def _create_offer(self, account=None, collector=None, status="pending", expires_in_minutes=30, responded_at=None):
        if collector is None:
            collector = self.collector_profile
        if account is None:
            self._offer_counter = getattr(self, "_offer_counter", 0) + 1
            account = Account.objects.create(
                client=self.client,
                customer=self.customer,
                external_id=f"ACC-OFFER-{self._offer_counter}",
                reference=f"REF-OFFER-{self._offer_counter}",
                balance_snapshot=Decimal("1500.00"),
                balance_as_of=timezone.now(),
                collection_address=self.address,
                commission_rule=self.rule,
            )
        now = timezone.now()
        expires_at = now + timezone.timedelta(minutes=expires_in_minutes)
        if status in ["accepted", "declined"] and responded_at is None:
            responded_at = now
        return AccountOffer.objects.create(
            account=account,
            collector=collector,
            target_address=self.address,
            target_position=Point(120.9850, 14.6000, srid=4326),
            commission_rule=self.rule,
            status=status,
            expires_at=expires_at,
            responded_at=responded_at,
            collector_position=Point(120.9850, 14.6000, srid=4326),
            location_captured_at=now,
            distance_m=Decimal("100.00"),
            matching_snapshot={},
        )

    def test_all_and_unread_filtering(self):
        n_unread = Notification.objects.create(
            recipient=self.collector_user,
            kind="system",
            message="Pending unread system notice",
            dedup_key="filt_unread",
        )
        n_read = Notification.objects.create(
            recipient=self.collector_user,
            kind="system",
            message="Completed read notice",
            read_at=timezone.now(),
            dedup_key="filt_read",
        )

        client = HttpClient()
        client.force_login(self.collector_user)

        # Default filter = unread
        resp_default = client.get(reverse("notifications:inbox"))
        self.assertEqual(resp_default.context["filter_type"], "unread")
        self.assertContains(resp_default, "Pending unread system notice")
        self.assertNotContains(resp_default, "Completed read notice")

        # Invalid filter defaults to unread
        resp_invalid = client.get(reverse("notifications:inbox") + "?filter=gibberish")
        self.assertEqual(resp_invalid.context["filter_type"], "unread")
        self.assertContains(resp_invalid, "Pending unread system notice")
        self.assertNotContains(resp_invalid, "Completed read notice")

        # Filter = all
        resp_all = client.get(reverse("notifications:inbox") + "?filter=all")
        self.assertEqual(resp_all.context["filter_type"], "all")
        self.assertContains(resp_all, "Pending unread system notice")
        self.assertContains(resp_all, "Completed read notice")

        # Filter = unread
        resp_unread = client.get(reverse("notifications:inbox") + "?filter=unread")
        self.assertEqual(resp_unread.context["filter_type"], "unread")
        self.assertContains(resp_unread, "Pending unread system notice")
        self.assertNotContains(resp_unread, "Completed read notice")

    def test_mark_read_post_only(self):
        n = Notification.objects.create(
            recipient=self.collector_user,
            kind="system",
            message="Notice to mark",
            dedup_key="post_only",
        )
        client = HttpClient()
        client.force_login(self.collector_user)

        # GET method is rejected
        resp_get = client.get(reverse("notifications:mark_read", args=[n.pk]))
        self.assertEqual(resp_get.status_code, 405)

        # POST marks read
        resp_post = client.post(reverse("notifications:mark_read", args=[n.pk]))
        self.assertEqual(resp_post.status_code, 302)
        n.refresh_from_db()
        self.assertIsNotNone(n.read_at)

    def test_mark_read_foreign_notification_returns_404(self):
        foreign_notification = Notification.objects.create(
            recipient=self.other_collector_user,
            kind="system",
            message="Foreign notice",
            dedup_key="foreign",
        )
        client = HttpClient()
        client.force_login(self.collector_user)

        response = client.post(reverse("notifications:mark_read", args=[foreign_notification.pk]))
        self.assertEqual(response.status_code, 404)
        foreign_notification.refresh_from_db()
        self.assertIsNone(foreign_notification.read_at)

    def test_mark_read_idempotency(self):
        read_time = timezone.now()
        n = Notification.objects.create(
            recipient=self.collector_user,
            kind="system",
            message="Already read",
            read_at=read_time,
            dedup_key="idempotent",
        )
        client = HttpClient()
        client.force_login(self.collector_user)

        response = client.post(reverse("notifications:mark_read", args=[n.pk]))
        self.assertEqual(response.status_code, 302)
        n.refresh_from_db()
        self.assertEqual(n.read_at, read_time)

    def test_mark_all_read_affects_only_current_user(self):
        n1 = Notification.objects.create(
            recipient=self.collector_user,
            kind="system",
            message="Notice 1",
            dedup_key="all_1",
        )
        n2 = Notification.objects.create(
            recipient=self.collector_user,
            kind="system",
            message="Notice 2",
            dedup_key="all_2",
        )
        other_n = Notification.objects.create(
            recipient=self.other_collector_user,
            kind="system",
            message="Other user notice",
            dedup_key="all_other",
        )

        client = HttpClient()
        client.force_login(self.collector_user)

        # GET is rejected
        resp_get = client.get(reverse("notifications:mark_all_read"))
        self.assertEqual(resp_get.status_code, 405)

        # POST updates only current user's unread
        resp_post = client.post(reverse("notifications:mark_all_read"))
        self.assertEqual(resp_post.status_code, 302)

        n1.refresh_from_db()
        n2.refresh_from_db()
        other_n.refresh_from_db()

        self.assertIsNotNone(n1.read_at)
        self.assertIsNotNone(n2.read_at)
        self.assertIsNone(other_n.read_at)

    def test_collector_and_backoffice_inbox_layouts(self):
        client = HttpClient()

        # Collector sees collector template with collector navbar
        client.force_login(self.collector_user)
        resp_c = client.get(reverse("notifications:inbox"))
        self.assertEqual(resp_c.status_code, 200)
        self.assertTemplateUsed(resp_c, "notifications/collector_inbox.html")
        self.assertTemplateUsed(resp_c, "components/sidebar.html")
        self.assertTemplateUsed(resp_c, "components/navbar.html")
        self.assertEqual(resp_c.context["portal"], "collector")

        # Back Office sees backoffice template with backoffice navbar
        client.force_login(self.backoffice_user)
        resp_bo = client.get(reverse("notifications:inbox"))
        self.assertEqual(resp_bo.status_code, 200)
        self.assertTemplateUsed(resp_bo, "notifications/backoffice_inbox.html")
        self.assertTemplateUsed(resp_bo, "backoffice/components/sidebar.html")
        self.assertTemplateUsed(resp_bo, "backoffice/components/navbar.html")
        self.assertEqual(resp_bo.context["portal"], "backoffice")

    def test_empty_state_when_no_notifications(self):
        client = HttpClient()
        client.force_login(self.collector_user)

        # Default unread empty state
        resp = client.get(reverse("notifications:inbox"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "No unread notifications")

        # Filter all empty state
        resp_all = client.get(reverse("notifications:inbox") + "?filter=all")
        self.assertEqual(resp_all.status_code, 200)
        self.assertContains(resp_all, "No notifications yet")

    def test_safe_action_links_for_collector(self):
        # 1. Offer notification links to offers via offer summary
        offer = self._create_offer()
        n_offer = Notification.objects.create(
            recipient=self.collector_user,
            kind="offer",
            offer=offer,
            message="Nearby offer available",
            dedup_key="offer_link_test",
        )

        # 2. Account notification with active assignment links to account detail
        assignment = AccountAssignment.objects.create(
            account=self.account,
            collector=self.collector_profile,
            target_address=self.address,
            target_position=Point(120.9850, 14.6000, srid=4326),
            first_action_due_at=timezone.now() + timezone.timedelta(hours=24),
            policy_snapshot={},
            commission_rule=self.rule,
            status="active",
        )
        n_account_active = Notification.objects.create(
            recipient=self.collector_user,
            kind="system",
            account=self.account,
            message="Account assigned to you",
            dedup_key="account_active_test",
        )

        # 3. Account notification for an account not assigned to collector
        other_account = Account.objects.create(
            client=self.client,
            customer=self.customer,
            external_id="ACC-002",
            reference="REF-002",
            balance_snapshot=Decimal("2000.00"),
            balance_as_of=timezone.now(),
            collection_address=self.address,
            commission_rule=self.rule,
        )
        n_account_inactive = Notification.objects.create(
            recipient=self.collector_user,
            kind="system",
            account=other_account,
            message="Old account unassigned",
            dedup_key="account_inactive_test",
        )

        client = HttpClient()
        client.force_login(self.collector_user)
        response = client.get(reverse("notifications:inbox"))
        self.assertEqual(response.status_code, 200)

        # Grouped offer summary is present with open_offers route
        self.assertContains(response, reverse("notifications:open_offers"))
        self.assertContains(response, "1 account offer is available")
        self.assertContains(response, "View your offers before they expire.")

        # Active assignment open route and card overlay are present
        account_open_url = reverse("notifications:open", args=[n_account_active.pk])
        self.assertContains(response, f'action="{account_open_url}"')
        self.assertContains(response, "Open account: Account assigned to you")
        # Duplicate visible Open account button is removed
        self.assertNotContains(response, "inbox-action-button")

        # Inactive assignment has no action destination, but as an unread card has open form with "Mark as read"
        items_by_notif = {i["notification"].pk: i for i in response.context["notification_items"]}
        self.assertIsNone(items_by_notif[n_account_inactive.pk]["action_url"])
        self.assertContains(response, reverse("notifications:open", args=[n_account_inactive.pk]))
        self.assertContains(response, 'aria-label="Mark as read: Old account unassigned"')

    def test_unread_informational_card_is_clickable_and_read_history_card_is_not(self):
        n_system = Notification.objects.create(
            recipient=self.collector_user,
            kind="system",
            message="System announcement with no destination",
            dedup_key="no_destination_test",
        )
        n_read = Notification.objects.create(
            recipient=self.collector_user,
            kind="system",
            message="Read notice in history",
            read_at=timezone.now(),
            dedup_key="read_history_test",
        )
        client = HttpClient()
        client.force_login(self.collector_user)

        # Unread filter: informational unread card is clickable and has overlay
        response_unread = client.get(reverse("notifications:inbox"))
        self.assertEqual(response_unread.status_code, 200)
        self.assertContains(response_unread, reverse("notifications:open", args=[n_system.pk]))
        self.assertContains(response_unread, "inbox-card-open-overlay")
        self.assertContains(response_unread, "is-clickable")
        self.assertContains(response_unread, 'aria-label="Mark as read: System announcement with no destination"')

        # All filter: read card is not rendered as a full-card action
        response_all = client.get(reverse("notifications:inbox") + "?filter=all")
        self.assertEqual(response_all.status_code, 200)
        self.assertNotContains(response_all, reverse("notifications:open", args=[n_read.pk]))

    def test_open_notification_get_returns_405(self):
        n = Notification.objects.create(
            recipient=self.collector_user,
            kind="system",
            message="Notice to open",
            dedup_key="open_get_405",
        )
        client = HttpClient()
        client.force_login(self.collector_user)
        response = client.get(reverse("notifications:open", args=[n.pk]))
        self.assertEqual(response.status_code, 405)
        n.refresh_from_db()
        self.assertIsNone(n.read_at)

    def test_open_safe_own_notification_marks_read_and_redirects(self):
        offer = self._create_offer()
        n_offer = Notification.objects.create(
            recipient=self.collector_user,
            kind="offer",
            offer=offer,
            message="Offer for collector",
            dedup_key="open_offer_test",
        )
        assignment = AccountAssignment.objects.create(
            account=self.account,
            collector=self.collector_profile,
            target_address=self.address,
            target_position=Point(120.9850, 14.6000, srid=4326),
            first_action_due_at=timezone.now() + timezone.timedelta(hours=24),
            policy_snapshot={},
            commission_rule=self.rule,
            status="active",
        )
        n_account = Notification.objects.create(
            recipient=self.collector_user,
            kind="system",
            account=self.account,
            message="Account assigned to you",
            dedup_key="open_account_test",
        )

        client = HttpClient()
        client.force_login(self.collector_user)

        # Open offer notification directly
        resp_offer = client.post(reverse("notifications:open", args=[n_offer.pk]))
        self.assertEqual(resp_offer.status_code, 302)
        self.assertEqual(resp_offer.url, reverse("assignments:account_offers"))
        n_offer.refresh_from_db()
        self.assertIsNotNone(n_offer.read_at)

        # Open account notification
        resp_account = client.post(reverse("notifications:open", args=[n_account.pk]))
        self.assertEqual(resp_account.status_code, 302)
        self.assertEqual(resp_account.url, reverse("assignments:account_detail", args=[assignment.pk]))
        n_account.refresh_from_db()
        self.assertIsNotNone(n_account.read_at)

    def test_open_already_read_notification_is_safe_and_preserves_timestamp(self):
        offer = self._create_offer()
        past_time = timezone.now() - timezone.timedelta(days=1)
        n_read = Notification.objects.create(
            recipient=self.collector_user,
            kind="offer",
            offer=offer,
            message="Previously read offer",
            read_at=past_time,
            dedup_key="open_already_read",
        )
        client = HttpClient()
        client.force_login(self.collector_user)
        response = client.post(reverse("notifications:open", args=[n_read.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("assignments:account_offers"))
        n_read.refresh_from_db()
        self.assertEqual(n_read.read_at, past_time)

    def test_open_foreign_notification_returns_404(self):
        foreign_n = Notification.objects.create(
            recipient=self.other_collector_user,
            kind="system",
            message="Other user's notice",
            dedup_key="open_foreign",
        )
        client = HttpClient()
        client.force_login(self.collector_user)
        response = client.post(reverse("notifications:open", args=[foreign_n.pk]))
        self.assertEqual(response.status_code, 404)
        foreign_n.refresh_from_db()
        self.assertIsNone(foreign_n.read_at)

    def test_open_notification_without_safe_destination_marks_read_and_redirects_to_inbox(self):
        n_system = Notification.objects.create(
            recipient=self.collector_user,
            kind="system",
            message="System notification with no destination",
            dedup_key="open_no_dest",
        )
        other_account = Account.objects.create(
            client=self.client,
            customer=self.customer,
            external_id="ACC-003",
            reference="REF-003",
            balance_snapshot=Decimal("3000.00"),
            balance_as_of=timezone.now(),
            collection_address=self.address,
            commission_rule=self.rule,
        )
        n_unassigned_account = Notification.objects.create(
            recipient=self.collector_user,
            kind="system",
            account=other_account,
            message="Account without assignment",
            dedup_key="open_unassigned",
        )

        client = HttpClient()
        client.force_login(self.collector_user)

        resp1 = client.post(reverse("notifications:open", args=[n_system.pk]))
        self.assertEqual(resp1.status_code, 302)
        self.assertEqual(resp1.url, reverse("notifications:inbox"))
        n_system.refresh_from_db()
        self.assertIsNotNone(n_system.read_at)

        resp2 = client.post(reverse("notifications:open", args=[n_unassigned_account.pk]))
        self.assertEqual(resp2.status_code, 302)
        self.assertEqual(resp2.url, reverse("notifications:inbox"))
        n_unassigned_account.refresh_from_db()
        self.assertIsNotNone(n_unassigned_account.read_at)

    def test_open_notification_requires_login_and_portal_access(self):
        n = Notification.objects.create(
            recipient=self.collector_user,
            kind="system",
            message="System notice",
            dedup_key="open_auth_test",
        )
        client = HttpClient()

        # Anonymous user
        resp_anon = client.post(reverse("notifications:open", args=[n.pk]))
        self.assertEqual(resp_anon.status_code, 302)
        self.assertIn(reverse("login"), resp_anon.url)

        # User without portal access
        client.force_login(self.no_portal_user)
        resp_noportal = client.post(reverse("notifications:open", args=[n.pk]))
        self.assertEqual(resp_noportal.status_code, 403)

    def test_open_notification_unread_filtering_and_count_reduction(self):
        assignment = AccountAssignment.objects.create(
            account=self.account,
            collector=self.collector_profile,
            target_address=self.address,
            target_position=Point(120.9850, 14.6000, srid=4326),
            first_action_due_at=timezone.now() + timezone.timedelta(hours=24),
            policy_snapshot={},
            commission_rule=self.rule,
            status="active",
        )
        n_account = Notification.objects.create(
            recipient=self.collector_user,
            kind="system",
            account=self.account,
            message="Active account to open",
            dedup_key="open_filter_test",
        )
        client = HttpClient()
        client.force_login(self.collector_user)

        # Before opening: 1 unread notification in default inbox
        resp_before = client.get(reverse("notifications:inbox"))
        self.assertEqual(resp_before.context["unread_count"], 1)
        self.assertContains(resp_before, "Active account to open")

        # Open notification
        resp_open = client.post(reverse("notifications:open", args=[n_account.pk]))
        self.assertEqual(resp_open.status_code, 302)

        # After opening: default inbox (unread) no longer shows it
        resp_unread = client.get(reverse("notifications:inbox"))
        self.assertEqual(resp_unread.context["unread_count"], 0)
        self.assertNotContains(resp_unread, "Active account to open")

        # filter=all still shows it as Read with Mark unread action
        resp_all = client.get(reverse("notifications:inbox") + "?filter=all")
        self.assertEqual(resp_all.context["all_count"], 1)
        self.assertEqual(resp_all.context["unread_count"], 0)
        self.assertContains(resp_all, "Active account to open")
        self.assertContains(resp_all, "Read")
        self.assertContains(resp_all, "Mark unread")

        # Notification record is retained in database
        self.assertTrue(Notification.objects.filter(pk=n_account.pk).exists())

    def test_default_inbox_displays_unread_items_only(self):
        n_unread = Notification.objects.create(
            recipient=self.collector_user,
            kind="system",
            message="Unread item for default view",
            dedup_key="def_unread",
        )
        n_read = Notification.objects.create(
            recipient=self.collector_user,
            kind="system",
            message="Read item for history only",
            read_at=timezone.now(),
            dedup_key="def_read",
        )
        client = HttpClient()
        client.force_login(self.collector_user)
        resp = client.get(reverse("notifications:inbox"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["filter_type"], "unread")
        self.assertContains(resp, "Unread item for default view")
        self.assertNotContains(resp, "Read item for history only")

    def test_all_filter_displays_read_history(self):
        Notification.objects.create(
            recipient=self.collector_user,
            kind="system",
            message="Historical read notification",
            read_at=timezone.now(),
            dedup_key="hist_read",
        )
        client = HttpClient()
        client.force_login(self.collector_user)
        resp = client.get(reverse("notifications:inbox") + "?filter=all")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["filter_type"], "all")
        self.assertContains(resp, "Historical read notification")
        self.assertContains(resp, "Read")
        self.assertContains(resp, "Mark unread")

    def test_mark_unread_restores_item_to_default_inbox_and_increments_count(self):
        n = Notification.objects.create(
            recipient=self.collector_user,
            kind="system",
            message="Item to mark unread",
            read_at=timezone.now(),
            dedup_key="mark_unread_test",
        )
        client = HttpClient()
        client.force_login(self.collector_user)

        # Initially unread count is 0
        resp_init = client.get(reverse("notifications:inbox"))
        self.assertEqual(resp_init.context["unread_count"], 0)
        self.assertNotContains(resp_init, "Item to mark unread")

        # Mark unread via POST
        resp_post = client.post(reverse("notifications:mark_unread", args=[n.pk]))
        self.assertEqual(resp_post.status_code, 302)
        n.refresh_from_db()
        self.assertIsNone(n.read_at)

        # Now item is restored to default inbox and unread count is 1
        resp_after = client.get(reverse("notifications:inbox"))
        self.assertEqual(resp_after.context["unread_count"], 1)
        self.assertContains(resp_after, "Item to mark unread")

    def test_mark_unread_foreign_notification_returns_404(self):
        foreign_n = Notification.objects.create(
            recipient=self.other_collector_user,
            kind="system",
            message="Other user's read notice",
            read_at=timezone.now(),
            dedup_key="foreign_unread",
        )
        client = HttpClient()
        client.force_login(self.collector_user)
        resp = client.post(reverse("notifications:mark_unread", args=[foreign_n.pk]))
        self.assertEqual(resp.status_code, 404)
        foreign_n.refresh_from_db()
        self.assertIsNotNone(foreign_n.read_at)

    def test_get_to_mark_unread_and_open_offers_returns_405(self):
        n = Notification.objects.create(
            recipient=self.collector_user,
            kind="system",
            message="Test notice",
            read_at=timezone.now(),
            dedup_key="method_405",
        )
        client = HttpClient()
        client.force_login(self.collector_user)

        resp_unread = client.get(reverse("notifications:mark_unread", args=[n.pk]))
        self.assertEqual(resp_unread.status_code, 405)

        resp_offers = client.get(reverse("notifications:open_offers"))
        self.assertEqual(resp_offers.status_code, 405)

    def test_several_qualifying_unread_offers_render_one_summary_not_repeated_cards(self):
        offer1 = self._create_offer()
        offer2 = self._create_offer()
        Notification.objects.create(
            recipient=self.collector_user,
            kind="offer",
            offer=offer1,
            message="First offer notification",
            dedup_key="offer_notif_1",
        )
        Notification.objects.create(
            recipient=self.collector_user,
            kind="offer",
            offer=offer2,
            message="Second offer notification",
            dedup_key="offer_notif_2",
        )
        client = HttpClient()
        client.force_login(self.collector_user)

        resp = client.get(reverse("notifications:inbox"))
        self.assertEqual(resp.status_code, 200)
        self.assertIsNotNone(resp.context["offer_summary"])
        self.assertEqual(resp.context["offer_summary"]["count"], 2)

        # Renders summary card with count and text
        self.assertContains(resp, "2 account offers are available")
        self.assertContains(resp, "View your offers before they expire.")
        self.assertContains(resp, reverse("notifications:open_offers"))

        # Source notifications are excluded from ordinary list items
        self.assertEqual(len(resp.context["notification_items"]), 0)
        self.assertNotContains(resp, "First offer notification")
        self.assertNotContains(resp, "Second offer notification")

    def test_opening_summary_reads_only_qualifying_current_offer_notices_and_redirects(self):
        offer1 = self._create_offer()
        offer2 = self._create_offer()
        n_offer1 = Notification.objects.create(
            recipient=self.collector_user,
            kind="offer",
            offer=offer1,
            message="Qualifying offer 1",
            dedup_key="qual_1",
        )
        n_offer2 = Notification.objects.create(
            recipient=self.collector_user,
            kind="offer",
            offer=offer2,
            message="Qualifying offer 2",
            dedup_key="qual_2",
        )
        n_other = Notification.objects.create(
            recipient=self.collector_user,
            kind="system",
            message="Unrelated system message",
            dedup_key="unrelated_system",
        )

        client = HttpClient()
        client.force_login(self.collector_user)

        resp = client.post(reverse("notifications:open_offers"))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, reverse("assignments:account_offers"))

        n_offer1.refresh_from_db()
        n_offer2.refresh_from_db()
        n_other.refresh_from_db()

        self.assertIsNotNone(n_offer1.read_at)
        self.assertIsNotNone(n_offer2.read_at)
        self.assertIsNone(n_other.read_at)

    def test_expired_non_pending_and_already_read_offers_excluded_from_summary_and_preserved_in_all(self):
        # 1. Already read offer (still pending & unexpired)
        offer_read = self._create_offer()
        Notification.objects.create(
            recipient=self.collector_user,
            kind="offer",
            offer=offer_read,
            message="Already read offer notice",
            read_at=timezone.now(),
            dedup_key="read_offer_notif",
        )

        # 2. Expired unread offer (expires_at in past, with created_at earlier to satisfy check constraint)
        offer_expired = self._create_offer()
        past_created = timezone.now() - timezone.timedelta(hours=2)
        past_expired = timezone.now() - timezone.timedelta(hours=1)
        AccountOffer.objects.filter(pk=offer_expired.pk).update(created_at=past_created, expires_at=past_expired)
        offer_expired.refresh_from_db()
        Notification.objects.create(
            recipient=self.collector_user,
            kind="offer",
            offer=offer_expired,
            message="Expired offer notice",
            dedup_key="expired_offer_notif",
        )

        # 3. Non-pending (declined) unread offer
        offer_declined = self._create_offer(status="declined")
        Notification.objects.create(
            recipient=self.collector_user,
            kind="offer",
            offer=offer_declined,
            message="Declined offer notice",
            dedup_key="declined_offer_notif",
        )

        client = HttpClient()
        client.force_login(self.collector_user)

        # Default inbox (unread): no qualifying active offers, summary must NOT appear
        resp_unread = client.get(reverse("notifications:inbox"))
        self.assertIsNone(resp_unread.context["offer_summary"])
        self.assertNotContains(resp_unread, "inbox-offer-summary")

        # All filter: all 3 appear as individual records in history
        resp_all = client.get(reverse("notifications:inbox") + "?filter=all")
        self.assertIsNone(resp_all.context["offer_summary"])
        self.assertContains(resp_all, "Already read offer notice")
        self.assertContains(resp_all, "Expired offer notice")
        self.assertContains(resp_all, "Declined offer notice")

    def test_normal_safe_cards_have_no_duplicate_visible_open_button(self):
        assignment = AccountAssignment.objects.create(
            account=self.account,
            collector=self.collector_profile,
            target_address=self.address,
            target_position=Point(120.9850, 14.6000, srid=4326),
            first_action_due_at=timezone.now() + timezone.timedelta(hours=24),
            policy_snapshot={},
            commission_rule=self.rule,
            status="active",
        )
        n_account = Notification.objects.create(
            recipient=self.collector_user,
            kind="system",
            account=self.account,
            message="Assigned account card",
            dedup_key="safe_card_no_dup",
        )
        client = HttpClient()
        client.force_login(self.collector_user)
        resp = client.get(reverse("notifications:inbox"))

        # Has full-card overlay button
        self.assertContains(resp, "inbox-card-open-overlay")
        self.assertContains(resp, f'action="{reverse("notifications:open", args=[n_account.pk])}"')

        # Does NOT have duplicate visible Open account button
        self.assertNotContains(resp, "inbox-action-button")
        self.assertNotContains(resp, "inbox-action-link")

    def test_open_offers_when_all_expired_still_redirects_safely(self):
        offer_expired = self._create_offer()
        past_created = timezone.now() - timezone.timedelta(hours=2)
        past_expired = timezone.now() - timezone.timedelta(hours=1)
        AccountOffer.objects.filter(pk=offer_expired.pk).update(created_at=past_created, expires_at=past_expired)
        offer_expired.refresh_from_db()
        n = Notification.objects.create(
            recipient=self.collector_user,
            kind="offer",
            offer=offer_expired,
            message="Expired offer",
            dedup_key="expired_safely_redirects",
        )
        client = HttpClient()
        client.force_login(self.collector_user)

        resp = client.post(reverse("notifications:open_offers"))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, reverse("assignments:account_offers"))
        n.refresh_from_db()
        self.assertIsNone(n.read_at)

    def test_csrf_token_present_in_inbox_forms(self):
        Notification.objects.create(
            recipient=self.collector_user,
            kind="system",
            message="Unread notification with form",
            dedup_key="csrf_test",
        )
        client = HttpClient(enforce_csrf_checks=True)
        client.force_login(self.collector_user)
        response = client.get(reverse("notifications:inbox"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="csrfmiddlewaretoken"')

    def test_dropdown_css_closed_and_open_selectors_exist(self):
        import os
        from django.conf import settings

        # Collector navbar.css
        navbar_css_path = os.path.join(settings.BASE_DIR, "static", "css", "components", "navbar.css")
        with open(navbar_css_path, "r", encoding="utf-8") as f:
            navbar_css = f.read()

        self.assertIn(".top-notif-dropdown:not([open]) .top-notif-menu", navbar_css)
        self.assertIn(".top-notif-dropdown[open] .top-notif-menu", navbar_css)

        # Back Office portal.css
        portal_css_path = os.path.join(settings.BASE_DIR, "static", "css", "backoffice", "portal.css")
        with open(portal_css_path, "r", encoding="utf-8") as f:
            portal_css = f.read()

        self.assertIn(".bo-notif-dropdown:not([open]) .bo-notif-menu", portal_css)
        self.assertIn(".bo-notif-dropdown[open] .bo-notif-menu", portal_css)

    def test_bounded_query_count_for_collector_dropdown_with_three_preview_rows(self):
        # 1. Offer-related notice: expired offer (so it falls into preview items, not active summary)
        expired_offer = self._create_offer()
        past_created = timezone.now() - timezone.timedelta(hours=2)
        past_expired = timezone.now() - timezone.timedelta(hours=1)
        AccountOffer.objects.filter(pk=expired_offer.pk).update(created_at=past_created, expires_at=past_expired)
        expired_offer.refresh_from_db()
        Notification.objects.create(
            recipient=self.collector_user,
            kind="offer",
            offer=expired_offer,
            message="Expired offer notice",
            dedup_key="bounded_query_offer",
        )

        # 2. Account-related notice: active assignment
        assignment = AccountAssignment.objects.create(
            account=self.account,
            collector=self.collector_profile,
            target_address=self.address,
            target_position=Point(120.9850, 14.6000, srid=4326),
            first_action_due_at=timezone.now() + timezone.timedelta(hours=24),
            policy_snapshot={},
            commission_rule=self.rule,
            status="active",
        )
        Notification.objects.create(
            recipient=self.collector_user,
            kind="system",
            account=self.account,
            message="Assigned account notice",
            dedup_key="bounded_query_account",
        )

        # 3. Informational notice
        Notification.objects.create(
            recipient=self.collector_user,
            kind="system",
            message="System announcement notice",
            dedup_key="bounded_query_system",
        )

        factory = RequestFactory()
        req = factory.get("/")
        req.user = self.collector_user

        # Warm up user permission cache
        unread_notifications(req)

        # Query count is bounded (exactly 6 queries) and does not grow per row
        with self.assertNumQueries(6):
            ctx = unread_notifications(req)

        self.assertEqual(ctx["unread_notifications_count"], 3)
        self.assertEqual(len(ctx["notification_dropdown_items"]), 3)

        # Verify items resolved correctly without additional per-row queries
        items_by_kind = {item["notification"].dedup_key: item for item in ctx["notification_dropdown_items"]}
        self.assertEqual(
            items_by_kind["bounded_query_offer"]["action_url"],
            reverse("assignments:account_offers"),
        )
        self.assertEqual(
            items_by_kind["bounded_query_offer"]["action_label"],
            "View offers",
        )
        self.assertEqual(
            items_by_kind["bounded_query_account"]["action_url"],
            reverse("assignments:account_detail", args=[assignment.pk]),
        )
        self.assertIsNone(items_by_kind["bounded_query_system"]["action_url"])

    def test_individual_expired_declined_withdrawn_and_out_of_scope_offers_redirect_to_account_offers(self):
        # 1. Expired offer
        expired_offer = self._create_offer()
        past_created = timezone.now() - timezone.timedelta(hours=2)
        past_expired = timezone.now() - timezone.timedelta(hours=1)
        AccountOffer.objects.filter(pk=expired_offer.pk).update(created_at=past_created, expires_at=past_expired)
        expired_offer.refresh_from_db()
        n_expired = Notification.objects.create(
            recipient=self.collector_user,
            kind="offer",
            offer=expired_offer,
            message="Expired offer notice",
            dedup_key="route_expired_test",
        )

        # 2. Declined offer
        declined_offer = self._create_offer(status="declined")
        n_declined = Notification.objects.create(
            recipient=self.collector_user,
            kind="offer",
            offer=declined_offer,
            message="Declined offer notice",
            dedup_key="route_declined_test",
        )

        # 3. Withdrawn offer
        withdrawn_offer = self._create_offer(status="withdrawn")
        n_withdrawn = Notification.objects.create(
            recipient=self.collector_user,
            kind="offer",
            offer=withdrawn_offer,
            message="Withdrawn offer notice",
            dedup_key="route_withdrawn_test",
        )

        # 4. Out-of-scope offer (collector has no ClientAccess for this client)
        client_other = Client.objects.create(code="CL_OUT", name="Out Of Scope Client", is_active=True)
        cust_other = Customer.objects.create(client=client_other, external_id="CUST-OUT", display_name="Out Client")
        addr_other = CustomerAddress.objects.create(
            customer=cust_other, kind="residential", address_line="Out St",
            position=Point(120.9850, 14.6000, srid=4326)
        )
        acc_other = Account.objects.create(
            client=client_other, customer=cust_other, external_id="ACC-OUT", reference="REF-OUT",
            balance_snapshot=Decimal("500.00"), balance_as_of=timezone.now(),
            collection_address=addr_other, commission_rule=self.rule
        )
        out_of_scope_offer = self._create_offer(account=acc_other)
        n_out_of_scope = Notification.objects.create(
            recipient=self.collector_user,
            kind="offer",
            offer=out_of_scope_offer,
            message="Out of scope offer notice",
            dedup_key="route_out_of_scope_test",
        )

        # 5. One qualifying active offer to test grouping and open_offers distinction
        qualifying_offer = self._create_offer()
        n_qualifying = Notification.objects.create(
            recipient=self.collector_user,
            kind="offer",
            offer=qualifying_offer,
            message="Qualifying offer notice",
            dedup_key="route_qualifying_test",
        )

        client = HttpClient()
        client.force_login(self.collector_user)

        # Prove all 4 old/out-of-scope notices are excluded from grouped current-offer summary
        resp_inbox = client.get(reverse("notifications:inbox"))
        self.assertEqual(resp_inbox.status_code, 200)
        self.assertIsNotNone(resp_inbox.context["offer_summary"])
        # Exactly 1 qualifying active offer in summary
        self.assertEqual(resp_inbox.context["offer_summary"]["count"], 1)

        # Each individual old/out-of-scope offer notification resolves to assignments:account_offers
        items_by_pk = {item["notification"].pk: item for item in resp_inbox.context["notification_items"]}
        for notif in [n_expired, n_declined, n_withdrawn, n_out_of_scope]:
            self.assertEqual(items_by_pk[notif.pk]["action_url"], reverse("assignments:account_offers"))
            self.assertEqual(items_by_pk[notif.pk]["action_label"], "View offers")

        # Prove open_offers does NOT mark the old/out-of-scope notifications read
        resp_open_offers = client.post(reverse("notifications:open_offers"))
        self.assertEqual(resp_open_offers.status_code, 302)
        self.assertEqual(resp_open_offers.url, reverse("assignments:account_offers"))

        n_qualifying.refresh_from_db()
        n_expired.refresh_from_db()
        n_declined.refresh_from_db()
        n_withdrawn.refresh_from_db()
        n_out_of_scope.refresh_from_db()

        self.assertIsNotNone(n_qualifying.read_at)
        self.assertIsNone(n_expired.read_at)
        self.assertIsNone(n_declined.read_at)
        self.assertIsNone(n_withdrawn.read_at)
        self.assertIsNone(n_out_of_scope.read_at)

        # Prove individual POST open on each old/out-of-scope notification marks it read and redirects to account_offers
        for notif in [n_expired, n_declined, n_withdrawn, n_out_of_scope]:
            resp_open = client.post(reverse("notifications:open", args=[notif.pk]))
            self.assertEqual(resp_open.status_code, 302)
            self.assertEqual(resp_open.url, reverse("assignments:account_offers"))
            notif.refresh_from_db()
            self.assertIsNotNone(notif.read_at)

    def test_read_notification_history_clickability_rules(self):
        # 1. Expired read offer
        expired_offer = self._create_offer()
        past_created = timezone.now() - timezone.timedelta(hours=2)
        past_expired = timezone.now() - timezone.timedelta(hours=1)
        AccountOffer.objects.filter(pk=expired_offer.pk).update(created_at=past_created, expires_at=past_expired)
        expired_offer.refresh_from_db()
        t_expired = timezone.now() - timezone.timedelta(minutes=30)
        n_expired = Notification.objects.create(
            recipient=self.collector_user,
            kind="offer",
            offer=expired_offer,
            message="Expired offer in history",
            read_at=t_expired,
            dedup_key="read_hist_expired",
        )

        # 2. Declined read offer
        declined_offer = self._create_offer(status="declined")
        t_declined = timezone.now() - timezone.timedelta(minutes=25)
        n_declined = Notification.objects.create(
            recipient=self.collector_user,
            kind="offer",
            offer=declined_offer,
            message="Declined offer in history",
            read_at=t_declined,
            dedup_key="read_hist_declined",
        )

        # 3. Withdrawn read offer
        withdrawn_offer = self._create_offer(status="withdrawn")
        t_withdrawn = timezone.now() - timezone.timedelta(minutes=20)
        n_withdrawn = Notification.objects.create(
            recipient=self.collector_user,
            kind="offer",
            offer=withdrawn_offer,
            message="Withdrawn offer in history",
            read_at=t_withdrawn,
            dedup_key="read_hist_withdrawn",
        )

        # 4. Out-of-scope read offer
        client_other = Client.objects.create(code="CL_HIST_OUT", name="Out Client Hist", is_active=True)
        cust_other = Customer.objects.create(client=client_other, external_id="CUST-H-OUT", display_name="Out Hist")
        addr_other = CustomerAddress.objects.create(
            customer=cust_other, kind="residential", address_line="Out Hist St",
            position=Point(120.9850, 14.6000, srid=4326)
        )
        acc_other = Account.objects.create(
            client=client_other, customer=cust_other, external_id="ACC-H-OUT", reference="REF-H-OUT",
            balance_snapshot=Decimal("600.00"), balance_as_of=timezone.now(),
            collection_address=addr_other, commission_rule=self.rule
        )
        out_of_scope_offer = self._create_offer(account=acc_other)
        t_out = timezone.now() - timezone.timedelta(minutes=15)
        n_out = Notification.objects.create(
            recipient=self.collector_user,
            kind="offer",
            offer=out_of_scope_offer,
            message="Out of scope offer in history",
            read_at=t_out,
            dedup_key="read_hist_out_of_scope",
        )

        # 5. Read account notification with active assignment
        assignment = AccountAssignment.objects.create(
            account=self.account,
            collector=self.collector_profile,
            target_address=self.address,
            target_position=Point(120.9850, 14.6000, srid=4326),
            first_action_due_at=timezone.now() + timezone.timedelta(hours=24),
            policy_snapshot={},
            commission_rule=self.rule,
            status="active",
        )
        t_account = timezone.now() - timezone.timedelta(minutes=10)
        n_account = Notification.objects.create(
            recipient=self.collector_user,
            kind="system",
            account=self.account,
            message="Assigned account in history",
            read_at=t_account,
            dedup_key="read_hist_account",
        )

        # 6. Read informational notification without action destination
        t_info = timezone.now() - timezone.timedelta(minutes=5)
        n_info = Notification.objects.create(
            recipient=self.collector_user,
            kind="system",
            message="System announcement in history with no destination",
            read_at=t_info,
            dedup_key="read_hist_info",
        )

        client = HttpClient()
        client.force_login(self.collector_user)

        # Request filter=all
        response = client.get(reverse("notifications:inbox") + "?filter=all")
        self.assertEqual(response.status_code, 200)

        # Clickable read items (all 4 offers + active account notification):
        clickable_read_notifs = [n_expired, n_declined, n_withdrawn, n_out, n_account]
        for notif in clickable_read_notifs:
            open_url = reverse("notifications:open", args=[notif.pk])
            mark_unread_url = reverse("notifications:mark_unread", args=[notif.pk])
            self.assertContains(response, open_url)
            self.assertContains(response, mark_unread_url)

        # Non-clickable read informational notice:
        info_open_url = reverse("notifications:open", args=[n_info.pk])
        info_mark_unread_url = reverse("notifications:mark_unread", args=[n_info.pk])
        self.assertNotContains(response, info_open_url)
        self.assertContains(response, info_mark_unread_url)

        # Verify no duplicate visible Open account or View offers buttons
        self.assertNotContains(response, "inbox-action-button")
        self.assertNotContains(response, "inbox-action-link")

        # Verify POST to notifications:open for clickable read notices redirects safely and preserves read_at
        # 1. Old/out-of-scope offers redirect to account_offers
        for notif, expected_time in [
            (n_expired, t_expired),
            (n_declined, t_declined),
            (n_withdrawn, t_withdrawn),
            (n_out, t_out),
        ]:
            resp_open = client.post(reverse("notifications:open", args=[notif.pk]))
            self.assertEqual(resp_open.status_code, 302)
            self.assertEqual(resp_open.url, reverse("assignments:account_offers"))
            notif.refresh_from_db()
            self.assertEqual(notif.read_at, expected_time)

        # 2. Read active account notification redirects to account_detail
        resp_acc = client.post(reverse("notifications:open", args=[n_account.pk]))
        self.assertEqual(resp_acc.status_code, 302)
        self.assertEqual(resp_acc.url, reverse("assignments:account_detail", args=[assignment.pk]))
        n_account.refresh_from_db()
        self.assertEqual(n_account.read_at, t_account)


class PWATests(SimpleTestCase):
    def test_public_pwa_routes_accessible_without_login(self):
        client = HttpClient()

        resp_manifest = client.get("/manifest.webmanifest")
        self.assertEqual(resp_manifest.status_code, 200)
        self.assertEqual(resp_manifest.headers["Content-Type"], "application/manifest+json")

        resp_sw = client.get("/service-worker.js")
        self.assertEqual(resp_sw.status_code, 200)
        self.assertEqual(resp_sw.headers["Content-Type"], "application/javascript")

        resp_offline = client.get("/offline/")
        self.assertEqual(resp_offline.status_code, 200)
        self.assertIn("text/html", resp_offline.headers["Content-Type"])

    def test_no_cache_headers_on_manifest_and_worker(self):
        client = HttpClient()

        resp_manifest = client.get("/manifest.webmanifest")
        cache_control_manifest = resp_manifest.headers.get("Cache-Control", "")
        self.assertIn("no-cache", cache_control_manifest)

        resp_sw = client.get("/service-worker.js")
        cache_control_sw = resp_sw.headers.get("Cache-Control", "")
        self.assertIn("no-cache", cache_control_sw)
        self.assertEqual(resp_sw.headers.get("Service-Worker-Allowed"), "/")

    def test_manifest_required_fields_and_icon_references(self):
        client = HttpClient()
        resp = client.get("/manifest.webmanifest")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content.decode("utf-8"))

        self.assertEqual(data.get("name"), "FiCol")
        self.assertEqual(data.get("short_name"), "FiCol")
        self.assertEqual(data.get("start_url"), "/login/")
        self.assertEqual(data.get("scope"), "/")
        self.assertEqual(data.get("display"), "standalone")

        blue_palette = {"#1d4ed8", "#1e40af", "#0b192c", "#0f172a", "#93c5fd"}
        self.assertIn(data.get("theme_color", "").lower(), blue_palette)
        self.assertIn(data.get("background_color", "").lower(), blue_palette)

        icons = data.get("icons", [])
        self.assertTrue(len(icons) >= 2)
        icon_sizes = {icon.get("sizes"): icon.get("src") for icon in icons}
        self.assertIn("192x192", icon_sizes)
        self.assertIn("512x512", icon_sizes)

        # Verify generated PNG files physically exist on disk and have valid PNG dimensions
        icon_192_path = os.path.join(settings.BASE_DIR, "static", "img", "pwa", "icon-192.png")
        icon_512_path = os.path.join(settings.BASE_DIR, "static", "img", "pwa", "icon-512.png")
        self.assertTrue(os.path.exists(icon_192_path), "icon-192.png must exist")
        self.assertTrue(os.path.exists(icon_512_path), "icon-512.png must exist")

        with open(icon_192_path, "rb") as f:
            header = f.read(24)
            self.assertEqual(header[:8], b"\x89PNG\r\n\x1a\n")
            w, h = struct.unpack(">II", header[16:24])
            self.assertEqual((w, h), (192, 192))

        with open(icon_512_path, "rb") as f:
            header = f.read(24)
            self.assertEqual(header[:8], b"\x89PNG\r\n\x1a\n")
            w, h = struct.unpack(">II", header[16:24])
            self.assertEqual((w, h), (512, 512))

    def test_service_worker_safety_rules_no_mutation_or_dynamic_data_caching(self):
        client = HttpClient()
        resp = client.get("/service-worker.js")
        self.assertEqual(resp.status_code, 200)
        sw_code = resp.content.decode("utf-8")

        # 1. Never cache mutations (POST, PUT, PATCH, DELETE)
        self.assertIn("event.request.method !== 'GET'", sw_code)

        # 2. Precaching is restricted to offline and safe icons
        self.assertIn("'/offline/'", sw_code)
        self.assertNotIn("'/collector/'", sw_code)
        self.assertNotIn("'/back-office/'", sw_code)
        self.assertNotIn("'/accounts/'", sw_code)

        # 3. Dynamic navigation requests fallback to offline without caching dynamic HTML
        self.assertIn("event.request.mode === 'navigate'", sw_code)
        self.assertIn("caches.match('/offline/')", sw_code)

        # 4. Same origin static asset caching
        self.assertIn("url.pathname.startsWith('/static/')", sw_code)

        # 5. Skip waiting message handling
        self.assertIn("SKIP_WAITING", sw_code)
        install_handler = sw_code.split("self.addEventListener('install'")[1].split("});")[0]
        self.assertNotIn("self.skipWaiting()", install_handler)

    def test_base_template_includes_pwa_shell_elements_and_hidden_update_notice(self):
        html = render_to_string("base.html", context={})
        self.assertIn('<link rel="manifest" href="/manifest.webmanifest">', html)
        self.assertIn('<meta name="theme-color" content="#1D4ED8">', html)
        self.assertIn('static/css/components/pwa.css', html)
        self.assertIn('static/js/pwa/pwa-register.js', html)

        # Update notice initially hidden
        self.assertIn('id="pwa-update-notice"', html)
        self.assertIn('hidden', html)
        self.assertIn('Reload to update', html)

    def test_offline_page_content_is_safe_and_generic(self):
        client = HttpClient()
        resp = client.get("/offline/")
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode("utf-8")

        self.assertIn("You are currently offline", content)
        self.assertIn("FiCol requires an active internet connection", content)
        self.assertIn("Try again", content)

        # Ensure no sensitive business or account data is present
        sensitive_keywords = ["balance", "collector", "commission", "payment", "customer", "assignment", "back-office"]
        for keyword in sensitive_keywords:
            self.assertNotIn(f"data-{keyword}", content.lower())


class SettingsHostConfigurationTests(SimpleTestCase):
    def test_default_allowed_hosts_includes_localhost_defaults(self):
        self.assertIn("localhost", settings.ALLOWED_HOSTS)
        self.assertIn("127.0.0.1", settings.ALLOWED_HOSTS)
        self.assertIn("[::1]", settings.ALLOWED_HOSTS)

    def test_no_hardcoded_ngrok_hosts_in_settings(self):
        # Verify no hardcoded temporary/personal ngrok domains exist in committed source
        for host in settings.ALLOWED_HOSTS:
            self.assertNotIn("pseudocolumellar", host)
        for origin in settings.CSRF_TRUSTED_ORIGINS:
            self.assertNotIn("pseudocolumellar", origin)

    def test_host_and_origin_parsing_logic(self):
        from config.settings import _split_csv_setting

        # Empty or absent inputs produce []
        self.assertEqual(_split_csv_setting(None), [])
        self.assertEqual(_split_csv_setting(""), [])
        self.assertEqual(_split_csv_setting("   "), [])

        # Parses comma-separated values, strips whitespace, and discards empty items
        raw_hosts = "localhost, 127.0.0.1, [::1], my-tunnel.ngrok-free.app, "
        self.assertEqual(
            _split_csv_setting(raw_hosts),
            ["localhost", "127.0.0.1", "[::1]", "my-tunnel.ngrok-free.app"],
        )

        raw_origins = " https://my-tunnel.ngrok-free.app , , https://other-tunnel.ngrok-free.app "
        self.assertEqual(
            _split_csv_setting(raw_origins),
            ["https://my-tunnel.ngrok-free.app", "https://other-tunnel.ngrok-free.app"],
        )
