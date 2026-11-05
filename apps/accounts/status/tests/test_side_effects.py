"""Tests for side effects of account status transitions."""

from decimal import Decimal
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import Account
from apps.accounts.status.services import change_account_status
from apps.activities.models import FieldActivity, FollowUpTask
from apps.assignments.models import (
    AccountAssignment,
    AccountOffer,
    AssignmentReleaseRequest,
)
from apps.assignments.tests import offer_fixture
from apps.audit.models import AuditEvent
from apps.collection.models import PromiseToPay
from apps.core.models import Notification
from apps.user.models import ClientAccess


class StatusSideEffectsTests(TestCase):
    def setUp(self):
        self.collector_user, self.profile, self.client_obj, self.account = offer_fixture(second=True)
        self.supervisor = get_user_model().objects.create_user(
            username="test-supervisor-effects", password="password"
        )
        perm = Permission.objects.get(
            codename="change_account_status", content_type__app_label="accounts"
        )
        self.supervisor.user_permissions.add(perm)
        ClientAccess.objects.create(
            user=self.supervisor, client=self.client_obj, is_active=True
        )
        self.account.balance_snapshot = Decimal("0.00")
        self.account.save(update_fields=["balance_snapshot"])

    def _setup_full_account_activity(self):
        """Create assignment, offer, followup, release request, in-progress visit, and promise."""
        now = timezone.now()
        # Active assignment
        assignment = AccountAssignment.objects.create(
            account=self.account,
            collector=self.profile,
            status="active",
            target_address=self.account.collection_address,
            target_position=self.account.collection_address.position,
            commission_rule=self.account.commission_rule,
            policy_snapshot={},
            first_action_due_at=now + timezone.timedelta(days=1),
        )

        # Pending offer (for another collector)
        other_user = get_user_model().objects.get(username="gps-other")
        other_profile = other_user.collector_profile
        offer = AccountOffer.objects.create(
            account=self.account,
            collector=other_profile,
            target_address=self.account.collection_address,
            target_position=self.account.collection_address.position,
            collector_position=self.account.collection_address.position,
            location_captured_at=now,
            distance_m=Decimal("150.00"),
            commission_rule=self.account.commission_rule,
            status="pending",
            expires_at=now + timezone.timedelta(hours=1),
            matching_snapshot={},
        )

        # Pending followup task
        followup = FollowUpTask.objects.create(
            account=self.account,
            assignment=assignment,
            assignee=self.collector_user,
            kind="call",
            status="pending",
            due_at=now + timezone.timedelta(days=1),
        )

        # Pending release request
        release_req = AssignmentReleaseRequest.objects.create(
            assignment=assignment,
            requested_by=self.collector_user,
            event_id=uuid4(),
            reason="Collector requesting release",
            status="requested",
        )

        # In-progress field activity
        visit = FieldActivity.objects.create(
            account=self.account,
            assignment=assignment,
            recorded_by=self.collector_user,
            event_id=uuid4(),
            kind="visit",
            status=FieldActivity.Status.IN_PROGRESS,
            location_issue_reason="GPS unavailable",
            occurred_at=now,
        )

        # Submitted activity with an open promise
        submitted_activity = FieldActivity.objects.create(
            account=self.account,
            assignment=assignment,
            recorded_by=self.collector_user,
            event_id=uuid4(),
            kind="visit",
            status=FieldActivity.Status.SUBMITTED,
            outcome="promise_to_pay",
            submission_fingerprint="test-submission-fingerprint",
            location_issue_reason="GPS unavailable",
            occurred_at=now,
            ended_at=now,
        )
        promise = PromiseToPay.objects.create(
            activity=submitted_activity,
            amount=Decimal("1500.00"),
            due_date=(now + timezone.timedelta(days=7)).date(),
            status="open",
        )

        return assignment, offer, followup, release_req, visit, promise

    def test_terminal_transition_cleans_up_all_child_entities(self):
        assignment, offer, followup, release_req, visit, promise = self._setup_full_account_activity()

        change_account_status(
            account_id=self.account.pk,
            to_status=Account.Status.RESOLVED,
            reason="Debtor fully settled debt",
            actor=self.supervisor,
            event_id=uuid4(),
        )

        # Assignment completed
        assignment.refresh_from_db()
        self.assertEqual(assignment.status, "completed")
        self.assertIsNotNone(assignment.ended_at)
        self.assertIn("resolved", assignment.end_reason)
        self.assertIsNone(assignment.next_action_due_at)

        # Pending offer withdrawn
        offer.refresh_from_db()
        self.assertEqual(offer.status, "withdrawn")

        # FollowUpTask cancelled
        followup.refresh_from_db()
        self.assertEqual(followup.status, "cancelled")
        self.assertIsNone(followup.completed_at)

        # Release request superseded
        release_req.refresh_from_db()
        self.assertEqual(release_req.status, "superseded")
        self.assertEqual(release_req.reviewed_by, self.supervisor)
        self.assertIsNotNone(release_req.reviewed_at)

        # In-progress visit cancelled
        visit.refresh_from_db()
        self.assertEqual(visit.status, "cancelled")
        self.assertEqual(visit.cancel_reason, "account_status_changed")
        self.assertIsNotNone(visit.ended_at)

        # Promise cancelled on terminal transition
        promise.refresh_from_db()
        self.assertEqual(promise.status, "cancelled")
        self.assertIsNotNone(promise.resolved_at)

        # Collector notified
        notif = Notification.objects.filter(
            recipient=self.collector_user, account=self.account
        ).first()
        self.assertIsNotNone(notif)
        self.assertIn("completed", notif.message)

        # Audit events verified
        self.assertTrue(AuditEvent.objects.filter(action="assignment_completed", entity_id=str(assignment.pk)).exists())
        self.assertTrue(AuditEvent.objects.filter(action="offer_withdrawn", entity_id=str(offer.pk)).exists())
        self.assertTrue(AuditEvent.objects.filter(action="followup_cancelled", entity_id=str(followup.pk)).exists())
        self.assertTrue(AuditEvent.objects.filter(action="release_superseded", entity_id=str(release_req.pk)).exists())
        self.assertTrue(AuditEvent.objects.filter(action="visit_cancelled", entity_id=str(visit.pk)).exists())
        self.assertTrue(AuditEvent.objects.filter(action="promise_cancelled", entity_id=str(promise.pk)).exists())

    def test_paused_transition_releases_assignment_and_cancels_promises(self):
        assignment, offer, followup, release_req, visit, promise = self._setup_full_account_activity()

        change_account_status(
            account_id=self.account.pk,
            to_status=Account.Status.ON_HOLD,
            reason="Client requested hold",
            actor=self.supervisor,
            event_id=uuid4(),
        )

        # Assignment released (NOT completed)
        assignment.refresh_from_db()
        self.assertEqual(assignment.status, "released")
        self.assertIsNotNone(assignment.ended_at)
        self.assertIn("on_hold", assignment.end_reason)

        # Pending offer withdrawn
        offer.refresh_from_db()
        self.assertEqual(offer.status, "withdrawn")

        # FollowUpTask cancelled
        followup.refresh_from_db()
        self.assertEqual(followup.status, "cancelled")

        # Release request superseded
        release_req.refresh_from_db()
        self.assertEqual(release_req.status, "superseded")

        # In-progress visit cancelled
        visit.refresh_from_db()
        self.assertEqual(visit.status, "cancelled")

        # Paused targets also cancel promises, as required by the workflow.
        promise.refresh_from_db()
        self.assertEqual(promise.status, "cancelled")
        self.assertIsNotNone(promise.resolved_at)

        # Collector notified
        notif = Notification.objects.filter(
            recipient=self.collector_user, account=self.account
        ).first()
        self.assertIsNotNone(notif)
        self.assertIn("released", notif.message)
        self.assertIn("Client requested hold", notif.message)

        # Audit event verified
        self.assertTrue(AuditEvent.objects.filter(action="assignment_released", entity_id=str(assignment.pk)).exists())

    def test_reopen_transition_has_no_side_effects(self):
        self.account.status = Account.Status.ON_HOLD
        self.account.save(update_fields=["status"])

        change_account_status(
            account_id=self.account.pk,
            to_status=Account.Status.OPEN,
            reason="Hold removed by creditor",
            actor=self.supervisor,
            event_id=uuid4(),
        )

        self.account.refresh_from_db()
        self.assertEqual(self.account.status, Account.Status.OPEN)
        # No assignments or offers created automatically
        self.assertEqual(self.account.assignments.count(), 0)
        self.assertEqual(self.account.offers.count(), 0)
