"""Tests for account status transition rules, validation, and payment blockers."""

from decimal import Decimal
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import Account, AccountStatusChange
from apps.accounts.status.constants import (
    INVALID_STATUS_TRANSITION,
    REASON_REQUIRED,
    UNVERIFIED_PAYMENTS_EXIST,
    ZERO_BALANCE_REQUIRED,
)
from apps.accounts.status.exceptions import AccountStatusWorkflowError
from apps.accounts.status.services import change_account_status
from apps.assignments.tests import offer_fixture
from apps.audit.models import AuditEvent
from apps.collection.models import Payment
from apps.user.models import ClientAccess


class StatusTransitionTests(TestCase):
    def setUp(self):
        self.collector_user, self.profile, self.client_obj, self.account = offer_fixture()
        self.supervisor = get_user_model().objects.create_user(
            username="test-supervisor", password="password"
        )
        perm = Permission.objects.get(
            codename="change_account_status", content_type__app_label="accounts"
        )
        self.supervisor.user_permissions.add(perm)
        self.supervisor_access = ClientAccess.objects.create(
            user=self.supervisor, client=self.client_obj, is_active=True
        )
        self.account.balance_snapshot = Decimal("0.00")
        self.account.save(update_fields=["balance_snapshot"])

    def _change_status(self, account, to_status, reason="Operational transition"):
        return change_account_status(
            account_id=account.pk,
            to_status=to_status,
            reason=reason,
            actor=self.supervisor,
            event_id=uuid4(),
        )

    def test_open_to_all_allowed_targets(self):
        targets = [
            Account.Status.ON_HOLD,
            Account.Status.DISPUTED,
            Account.Status.LEGAL,
            Account.Status.RESOLVED,
            Account.Status.CLOSED,
            Account.Status.INVALID,
        ]
        for target in targets:
            with self.subTest(target=target):
                self.account.status = Account.Status.OPEN
                self.account.save(update_fields=["status"])
                result = self._change_status(self.account, target, reason=f"Move to {target}")
                self.assertTrue(result["saved"])
                self.assertTrue(result["created"])
                self.account.refresh_from_db()
                self.assertEqual(self.account.status, target)

    def test_on_hold_allowed_transitions(self):
        for target in [Account.Status.OPEN, Account.Status.CLOSED]:
            with self.subTest(target=target):
                self.account.status = Account.Status.ON_HOLD
                self.account.save(update_fields=["status"])
                result = self._change_status(self.account, target)
                self.assertTrue(result["saved"])
                self.account.refresh_from_db()
                self.assertEqual(self.account.status, target)

    def test_disputed_allowed_transitions(self):
        for target in [Account.Status.OPEN, Account.Status.LEGAL, Account.Status.CLOSED]:
            with self.subTest(target=target):
                self.account.status = Account.Status.DISPUTED
                self.account.save(update_fields=["status"])
                result = self._change_status(self.account, target)
                self.assertTrue(result["saved"])
                self.account.refresh_from_db()
                self.assertEqual(self.account.status, target)

    def test_legal_allowed_transitions(self):
        for target in [Account.Status.OPEN, Account.Status.CLOSED]:
            with self.subTest(target=target):
                self.account.status = Account.Status.LEGAL
                self.account.save(update_fields=["status"])
                result = self._change_status(self.account, target)
                self.assertTrue(result["saved"])
                self.account.refresh_from_db()
                self.assertEqual(self.account.status, target)

    def test_resolved_allowed_transitions(self):
        for target in [Account.Status.CLOSED, Account.Status.OPEN]:
            with self.subTest(target=target):
                self.account.status = Account.Status.RESOLVED
                self.account.save(update_fields=["status"])
                result = self._change_status(self.account, target)
                self.assertTrue(result["saved"])
                self.account.refresh_from_db()
                self.assertEqual(self.account.status, target)

    def test_closed_allowed_transitions(self):
        self.account.status = Account.Status.CLOSED
        self.account.save(update_fields=["status"])
        result = self._change_status(self.account, Account.Status.OPEN)
        self.assertTrue(result["saved"])
        self.account.refresh_from_db()
        self.assertEqual(self.account.status, Account.Status.OPEN)

    def test_invalid_allowed_transitions(self):
        self.account.status = Account.Status.INVALID
        self.account.save(update_fields=["status"])
        result = self._change_status(self.account, Account.Status.OPEN)
        self.assertTrue(result["saved"])
        self.account.refresh_from_db()
        self.assertEqual(self.account.status, Account.Status.OPEN)

    def test_disallowed_transitions_rejected(self):
        disallowed = [
            (Account.Status.OPEN, Account.Status.OPEN),
            (Account.Status.CLOSED, Account.Status.RESOLVED),
            (Account.Status.ON_HOLD, Account.Status.RESOLVED),
            (Account.Status.INVALID, Account.Status.CLOSED),
            (Account.Status.LEGAL, Account.Status.ON_HOLD),
        ]
        for from_status, to_status in disallowed:
            with self.subTest(from_status=from_status, to_status=to_status):
                self.account.status = from_status
                self.account.save(update_fields=["status"])
                with self.assertRaises(AccountStatusWorkflowError) as cm:
                    self._change_status(self.account, to_status)
                self.assertEqual(cm.exception.code, INVALID_STATUS_TRANSITION)
                self.assertEqual(cm.exception.status, 400)

    def test_reason_validation(self):
        for bad_reason in ["", "   ", None, "x" * 5001]:
            with self.subTest(reason=bad_reason), self.assertRaises(AccountStatusWorkflowError) as cm:
                change_account_status(
                    account_id=self.account.pk,
                    to_status=Account.Status.ON_HOLD,
                    reason=bad_reason,
                    actor=self.supervisor,
                    event_id=uuid4(),
                )
            self.assertEqual(cm.exception.code, REASON_REQUIRED)

    def test_resolved_or_closed_requires_zero_source_balance_snapshot(self):
        self.account.balance_snapshot = Decimal("1.00")
        self.account.save(update_fields=["balance_snapshot"])
        for target in [Account.Status.RESOLVED, Account.Status.CLOSED]:
            with self.subTest(target=target), self.assertRaises(AccountStatusWorkflowError) as cm:
                self._change_status(self.account, target)
            self.assertEqual(cm.exception.code, ZERO_BALANCE_REQUIRED)
            self.assertEqual(cm.exception.status, 409)

    def test_reason_trimmed_and_status_change_recorded(self):
        event_id = uuid4()
        result = change_account_status(
            account_id=self.account.pk,
            to_status=Account.Status.ON_HOLD,
            reason="  Important business hold  ",
            actor=self.supervisor,
            event_id=event_id,
        )
        change_record = result["status_change"]
        self.assertEqual(change_record.reason, "Important business hold")
        self.assertEqual(change_record.from_status, Account.Status.OPEN)
        self.assertEqual(change_record.to_status, Account.Status.ON_HOLD)
        self.assertEqual(change_record.changed_by, self.supervisor)
        self.assertEqual(change_record.event_id, event_id)

    def test_audit_event_recorded(self):
        self._change_status(self.account, Account.Status.ON_HOLD, reason="Audited transition")
        audit = AuditEvent.objects.filter(
            action="account_status_changed", entity_id=str(self.account.pk)
        ).first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.actor, self.supervisor)
        self.assertEqual(audit.account, self.account)
        self.assertEqual(audit.changes["from_status"], "open")
        self.assertEqual(audit.changes["to_status"], "on_hold")
        self.assertEqual(audit.changes["reason"], "Audited transition")

    def test_unverified_payments_block_resolved_and_closed(self):
        Payment.objects.create(
            account=self.account,
            reported_by=self.collector_user,
            amount=Decimal("500.00"),
            paid_at=timezone.now(),
            method=Payment.Method.BANK_TRANSFER,
            status=Payment.Status.REPORTED,
        )

        for target in [Account.Status.RESOLVED, Account.Status.CLOSED]:
            with self.subTest(target=target), self.assertRaises(AccountStatusWorkflowError) as cm:
                self._change_status(self.account, target)
            self.assertEqual(cm.exception.code, UNVERIFIED_PAYMENTS_EXIST)
            self.assertEqual(cm.exception.status, 409)

        # But other transitions like on_hold are NOT blocked
        result = self._change_status(self.account, Account.Status.ON_HOLD)
        self.assertTrue(result["saved"])
