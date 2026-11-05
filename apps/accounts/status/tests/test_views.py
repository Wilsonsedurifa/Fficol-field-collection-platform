"""Tests for Back Office status transition views and template integration."""

from uuid import uuid4
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import Client, TestCase
from django.urls import reverse

from apps.accounts.models import Account, AccountStatusChange
from apps.assignments.tests import offer_fixture
from apps.user.models import ClientAccess


class StatusViewTests(TestCase):
    def setUp(self):
        self.collector_user, self.profile, self.client_obj, self.account = offer_fixture()
        self.supervisor = get_user_model().objects.create_user(
            username="test-view-supervisor", password="password"
        )
        self.perm = Permission.objects.get(
            codename="change_account_status", content_type__app_label="accounts"
        )
        assign_perm = Permission.objects.get(
            codename="manage_account_assignments", content_type__app_label="assignments"
        )
        self.supervisor.user_permissions.add(self.perm, assign_perm)
        self.client_access = ClientAccess.objects.create(
            user=self.supervisor, client=self.client_obj, is_active=True
        )
        self.account.balance_snapshot = Decimal("0.00")
        self.account.save(update_fields=["balance_snapshot"])

        self.web_client = Client()
        self.url = reverse("backoffice_change_account_status", args=[self.account.pk])
        self.detail_url = reverse("backoffice_account_assignment_detail", args=[self.account.pk])

    def test_unauthenticated_redirects_to_login(self):
        response = self.web_client.post(self.url, {})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.url)

    def test_collector_forbidden(self):
        self.web_client.force_login(self.collector_user)
        response = self.web_client.post(self.url, {})
        self.assertEqual(response.status_code, 403)

    def test_supervisor_without_permission_forbidden(self):
        self.supervisor.user_permissions.remove(self.perm)
        self.web_client.force_login(self.supervisor)
        response = self.web_client.post(self.url, {})
        self.assertEqual(response.status_code, 403)

    def test_valid_status_change_redirects_and_updates_account(self):
        self.web_client.force_login(self.supervisor)
        payload = {
            "to_status": Account.Status.ON_HOLD,
            "reason": "Temporary investigation hold",
            "event_id": str(uuid4()),
        }
        response = self.web_client.post(self.url, payload, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertRedirects(response, self.detail_url)
        self.account.refresh_from_db()
        self.assertEqual(self.account.status, Account.Status.ON_HOLD)

    def test_terminal_transition_without_confirm_fails(self):
        self.web_client.force_login(self.supervisor)
        payload = {
            "to_status": Account.Status.RESOLVED,
            "reason": "Debt resolved",
            "event_id": str(uuid4()),
            # confirm_terminal NOT checked
        }
        response = self.web_client.post(self.url, payload, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertRedirects(response, self.detail_url)
        self.account.refresh_from_db()
        self.assertEqual(self.account.status, Account.Status.OPEN)
        messages = list(response.context["messages"])
        self.assertTrue(any("confirm" in str(m).lower() for m in messages))

    def test_terminal_transition_with_confirm_succeeds(self):
        self.web_client.force_login(self.supervisor)
        payload = {
            "to_status": Account.Status.RESOLVED,
            "reason": "Debt resolved fully",
            "confirm_terminal": "true",
            "event_id": str(uuid4()),
        }
        response = self.web_client.post(self.url, payload, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertRedirects(response, self.detail_url)
        self.account.refresh_from_db()
        self.assertEqual(self.account.status, Account.Status.RESOLVED)

    def test_invalid_transition_shows_error_message(self):
        self.account.status = Account.Status.CLOSED
        self.account.save(update_fields=["status"])

        self.web_client.force_login(self.supervisor)
        payload = {
            "to_status": Account.Status.RESOLVED,
            "reason": "Try moving closed to resolved",
            "confirm_terminal": "true",
            "event_id": str(uuid4()),
        }
        response = self.web_client.post(self.url, payload, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertRedirects(response, self.detail_url)
        self.account.refresh_from_db()
        self.assertEqual(self.account.status, Account.Status.CLOSED)

    def test_detail_view_renders_status_card_and_history(self):
        AccountStatusChange.objects.create(
            account=self.account,
            from_status=Account.Status.OPEN,
            to_status=Account.Status.ON_HOLD,
            reason="Historical test hold",
            changed_by=self.supervisor,
            event_id=uuid4(),
        )
        self.account.status = Account.Status.ON_HOLD
        self.account.save(update_fields=["status"])

        self.web_client.force_login(self.supervisor)
        response = self.web_client.get(self.detail_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Account Lifecycle Status")
        self.assertContains(response, "Historical test hold")
        self.assertContains(response, "Transition To")
