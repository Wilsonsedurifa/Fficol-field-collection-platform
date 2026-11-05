"""Tests for permissions and client scoping in account status transitions."""

from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser, Permission
from django.test import TestCase

from apps.accounts.models import Account
from apps.accounts.status.constants import (
    CLIENT_ACCESS_REQUIRED,
    PERMISSION_REQUIRED,
)
from apps.accounts.status.exceptions import AccountStatusWorkflowError
from apps.accounts.status.services import change_account_status
from apps.assignments.tests import offer_fixture
from apps.user.models import ClientAccess


class StatusPermissionsTests(TestCase):
    def setUp(self):
        self.collector_user, self.profile, self.client_obj, self.account = offer_fixture()
        self.supervisor = get_user_model().objects.create_user(
            username="test-supervisor-perm", password="password"
        )
        self.perm = Permission.objects.get(
            codename="change_account_status", content_type__app_label="accounts"
        )

    def test_anonymous_actor_rejected(self):
        with self.assertRaises(AccountStatusWorkflowError) as cm:
            change_account_status(
                account_id=self.account.pk,
                to_status=Account.Status.ON_HOLD,
                reason="Attempt by anonymous",
                actor=AnonymousUser(),
                event_id=uuid4(),
            )
        self.assertEqual(cm.exception.code, PERMISSION_REQUIRED)
        self.assertEqual(cm.exception.status, 403)

    def test_inactive_actor_rejected(self):
        self.supervisor.user_permissions.add(self.perm)
        self.supervisor.is_active = False
        self.supervisor.save(update_fields=["is_active"])
        ClientAccess.objects.create(user=self.supervisor, client=self.client_obj, is_active=True)

        with self.assertRaises(AccountStatusWorkflowError) as cm:
            change_account_status(
                account_id=self.account.pk,
                to_status=Account.Status.ON_HOLD,
                reason="Attempt by inactive",
                actor=self.supervisor,
                event_id=uuid4(),
            )
        self.assertEqual(cm.exception.code, PERMISSION_REQUIRED)
        self.assertEqual(cm.exception.status, 403)

    def test_actor_without_permission_rejected(self):
        # Has client access, but lacks accounts.change_account_status
        ClientAccess.objects.create(user=self.supervisor, client=self.client_obj, is_active=True)

        with self.assertRaises(AccountStatusWorkflowError) as cm:
            change_account_status(
                account_id=self.account.pk,
                to_status=Account.Status.ON_HOLD,
                reason="Attempt without permission",
                actor=self.supervisor,
                event_id=uuid4(),
            )
        self.assertEqual(cm.exception.code, PERMISSION_REQUIRED)
        self.assertEqual(cm.exception.status, 403)

    def test_actor_without_client_access_rejected(self):
        # Has permission, but lacks ClientAccess
        self.supervisor.user_permissions.add(self.perm)

        with self.assertRaises(AccountStatusWorkflowError) as cm:
            change_account_status(
                account_id=self.account.pk,
                to_status=Account.Status.ON_HOLD,
                reason="Attempt without client access",
                actor=self.supervisor,
                event_id=uuid4(),
            )
        self.assertEqual(cm.exception.code, CLIENT_ACCESS_REQUIRED)
        self.assertEqual(cm.exception.status, 403)

    def test_inactive_client_rejected(self):
        self.supervisor.user_permissions.add(self.perm)
        ClientAccess.objects.create(user=self.supervisor, client=self.client_obj, is_active=True)

        self.client_obj.is_active = False
        self.client_obj.save(update_fields=["is_active"])

        with self.assertRaises(AccountStatusWorkflowError) as cm:
            change_account_status(
                account_id=self.account.pk,
                to_status=Account.Status.ON_HOLD,
                reason="Attempt on inactive client",
                actor=self.supervisor,
                event_id=uuid4(),
            )
        self.assertEqual(cm.exception.code, CLIENT_ACCESS_REQUIRED)
        self.assertEqual(cm.exception.status, 403)
