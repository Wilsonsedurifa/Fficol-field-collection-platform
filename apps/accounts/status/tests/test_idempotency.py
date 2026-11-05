"""Tests for idempotency of account status transitions."""

from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase

from apps.accounts.models import Account, AccountStatusChange
from apps.accounts.status.constants import EVENT_CONFLICT
from apps.accounts.status.exceptions import AccountStatusWorkflowError
from apps.accounts.status.services import change_account_status
from apps.assignments.tests import offer_fixture
from apps.audit.models import AuditEvent
from apps.user.models import ClientAccess


class StatusIdempotencyTests(TestCase):
    def test_invalid_uuid_is_a_workflow_error(self):
        with self.assertRaises(AccountStatusWorkflowError) as caught:
            change_account_status(self.account.pk, Account.Status.ON_HOLD, "Hold", self.supervisor, "bad-uuid")
        self.assertEqual(caught.exception.status, 400)

    def test_retry_requires_current_client_access(self):
        event_id = uuid4()
        change_account_status(self.account.pk, Account.Status.ON_HOLD, "Hold", self.supervisor, event_id)
        ClientAccess.objects.filter(user=self.supervisor).update(is_active=False)
        with self.assertRaises(AccountStatusWorkflowError) as caught:
            change_account_status(self.account.pk, Account.Status.ON_HOLD, "Hold", self.supervisor, event_id)
        self.assertEqual(caught.exception.status, 403)

    def test_another_actor_cannot_reuse_event(self):
        event_id = uuid4()
        change_account_status(self.account.pk, Account.Status.ON_HOLD, "Hold", self.supervisor, event_id)
        other = get_user_model().objects.create_superuser(username="other-status-admin", password="test")
        ClientAccess.objects.create(user=other, client=self.client_obj)
        with self.assertRaises(AccountStatusWorkflowError) as caught:
            change_account_status(self.account.pk, Account.Status.ON_HOLD, "Hold", other, event_id)
        self.assertEqual(caught.exception.status, 409)

    def setUp(self):
        self.collector_user, self.profile, self.client_obj, self.account = offer_fixture()
        self.supervisor = get_user_model().objects.create_user(
            username="test-supervisor-idemp", password="password"
        )
        perm = Permission.objects.get(
            codename="change_account_status", content_type__app_label="accounts"
        )
        self.supervisor.user_permissions.add(perm)
        ClientAccess.objects.create(
            user=self.supervisor, client=self.client_obj, is_active=True
        )

    def test_repeated_call_same_event_id_is_idempotent(self):
        event_id = uuid4()
        first = change_account_status(
            account_id=self.account.pk,
            to_status=Account.Status.ON_HOLD,
            reason="Temporary legal review",
            actor=self.supervisor,
            event_id=event_id,
        )
        self.assertTrue(first["saved"])
        self.assertTrue(first["created"])
        self.assertEqual(AccountStatusChange.objects.filter(event_id=event_id).count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action="account_status_changed", entity_id=str(self.account.pk)).count(), 1)

        # Replay exact same request
        second = change_account_status(
            account_id=self.account.pk,
            to_status=Account.Status.ON_HOLD,
            reason="Temporary legal review",
            actor=self.supervisor,
            event_id=event_id,
        )
        self.assertTrue(second["saved"])
        self.assertFalse(second["created"])
        self.assertEqual(first["status_change"].pk, second["status_change"].pk)
        # No extra records created
        self.assertEqual(AccountStatusChange.objects.filter(event_id=event_id).count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action="account_status_changed", entity_id=str(self.account.pk)).count(), 1)

    def test_conflicting_call_same_event_id_rejected(self):
        event_id = uuid4()
        change_account_status(
            account_id=self.account.pk,
            to_status=Account.Status.ON_HOLD,
            reason="First reason",
            actor=self.supervisor,
            event_id=event_id,
        )

        # Same event_id with different reason
        with self.assertRaises(AccountStatusWorkflowError) as cm:
            change_account_status(
                account_id=self.account.pk,
                to_status=Account.Status.ON_HOLD,
                reason="Different reason",
                actor=self.supervisor,
                event_id=event_id,
            )
        self.assertEqual(cm.exception.code, EVENT_CONFLICT)
        self.assertEqual(cm.exception.status, 409)

        # Same event_id with different target status
        with self.assertRaises(AccountStatusWorkflowError) as cm:
            change_account_status(
                account_id=self.account.pk,
                to_status=Account.Status.DISPUTED,
                reason="First reason",
                actor=self.supervisor,
                event_id=event_id,
            )
        self.assertEqual(cm.exception.code, EVENT_CONFLICT)
        self.assertEqual(cm.exception.status, 409)
