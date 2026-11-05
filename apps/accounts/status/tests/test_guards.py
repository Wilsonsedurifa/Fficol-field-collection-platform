"""Tests for collector mutation guards on non-open accounts."""

from decimal import Decimal
from io import BytesIO
from uuid import uuid4

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import Account
from apps.activities.models import FieldActivity
from apps.activities.services import (
    record_contact_attempt,
    schedule_revisit,
    start_visit,
    submit_visit,
)
from apps.assignments.models import AccountAssignment, AccountOffer
from apps.assignments.services import (
    OfferWorkflowError,
    request_assignment_release,
    respond_to_offer,
)
from apps.assignments.tests import offer_fixture
from apps.collection.services import CollectionWorkflowError, report_payment


class CollectorMutationGuardsTests(TestCase):
    def setUp(self):
        self.collector_user, self.profile, self.client_obj, self.account = offer_fixture(second=True)
        now = timezone.now()

        self.assignment = AccountAssignment.objects.create(
            account=self.account,
            collector=self.profile,
            status="active",
            target_address=self.account.collection_address,
            target_position=self.account.collection_address.position,
            commission_rule=self.account.commission_rule,
            policy_snapshot={},
            first_action_due_at=now + timezone.timedelta(days=1),
        )

        self.offer = AccountOffer.objects.create(
            account=self.account,
            collector=self.profile,
            target_address=self.account.collection_address,
            target_position=self.account.collection_address.position,
            collector_position=self.account.collection_address.position,
            location_captured_at=now,
            distance_m=Decimal("150.00"),
            commission_rule=self.account.commission_rule,
            status="pending",
            expires_at=now + timezone.timedelta(hours=1),
            matching_snapshot={
                "policy": {},
                "commission_terms": {},
            },
        )

        # Transition account to paused / non-open status for tests
        self.account.status = Account.Status.ON_HOLD
        self.account.save(update_fields=["status"])

    def test_start_visit_rejected_when_account_not_open(self):
        payload = {
            "event_id": str(uuid4()),
            "location": None,
            "location_issue_reason": "GPS unavailable",
        }
        with self.assertRaises(OfferWorkflowError) as cm:
            start_visit(self.collector_user.pk, self.assignment.pk, payload)
        self.assertEqual(cm.exception.code, "account_not_open")
        self.assertEqual(cm.exception.status, 409)
        self.assertIn("This account is not open for field collection", cm.exception.message)

    def test_submit_visit_rejected_when_account_not_open(self):
        now = timezone.now()
        visit = FieldActivity.objects.create(
            account=self.account,
            assignment=self.assignment,
            recorded_by=self.collector_user,
            event_id=uuid4(),
            kind="visit",
            status=FieldActivity.Status.IN_PROGRESS,
            location_issue_reason="GPS unavailable",
            occurred_at=now,
        )
        payload = {
            "outcome": "contacted",
            "notes": "Spoke to customer at gate.",
            "promise": None,
            "follow_up": None,
        }
        with self.assertRaises(OfferWorkflowError) as cm:
            submit_visit(self.collector_user.pk, self.assignment.pk, visit.pk, payload)
        self.assertEqual(cm.exception.code, "account_not_open")
        self.assertEqual(cm.exception.status, 409)

    def test_schedule_revisit_rejected_when_account_not_open(self):
        now = timezone.now()
        visit = FieldActivity.objects.create(
            account=self.account,
            assignment=self.assignment,
            recorded_by=self.collector_user,
            event_id=uuid4(),
            kind="visit",
            status=FieldActivity.Status.SUBMITTED,
            outcome="unavailable",
            submission_fingerprint="test-fingerprint",
            location_issue_reason="GPS unavailable",
            occurred_at=now,
            ended_at=now,
        )
        payload = {"due_at": (now + timezone.timedelta(days=2)).isoformat()}
        with self.assertRaises(OfferWorkflowError) as cm:
            schedule_revisit(self.collector_user.pk, self.assignment.pk, visit.pk, payload)
        self.assertEqual(cm.exception.code, "account_not_open")
        self.assertEqual(cm.exception.status, 409)

    def test_record_contact_attempt_rejected_when_account_not_open(self):
        payload = {
            "event_id": str(uuid4()),
            "kind": "phone",
            "outcome": "contacted",
            "notes": "Called debtor phone number.",
        }
        with self.assertRaises(OfferWorkflowError) as cm:
            record_contact_attempt(self.collector_user.pk, self.assignment.pk, payload)
        self.assertEqual(cm.exception.code, "account_not_open")
        self.assertEqual(cm.exception.status, 409)

    def test_report_payment_rejected_when_account_not_open(self):
        now = timezone.now()
        doc = SimpleUploadedFile("receipt.jpg", b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 100, content_type="image/jpeg")
        payload = {
            "amount": "1000.00",
            "paid_at": now.isoformat(),
            "method": "bank_transfer",
            "reference": "REF123456",
            "event_id": str(uuid4()),
        }
        with self.assertRaises(CollectionWorkflowError) as cm:
            report_payment(self.collector_user.pk, self.assignment.pk, payload, doc)
        self.assertEqual(cm.exception.code, "account_not_open")
        self.assertEqual(cm.exception.status, 409)

    def test_request_assignment_release_rejected_when_account_not_open(self):
        payload = {
            "event_id": str(uuid4()),
            "reason": "Debtor unreachable and moved away.",
        }
        with self.assertRaises(OfferWorkflowError) as cm:
            request_assignment_release(self.collector_user.pk, self.assignment.pk, payload)
        self.assertEqual(cm.exception.code, "account_not_open")
        self.assertEqual(cm.exception.status, 409)

    def test_respond_to_offer_rejected_when_account_not_open(self):
        for action in ["accept", "decline"]:
            with self.subTest(action=action), self.assertRaises(OfferWorkflowError) as cm:
                respond_to_offer(self.collector_user.pk, self.offer.pk, action)
            self.assertEqual(cm.exception.code, "account_not_open")
            self.assertEqual(cm.exception.status, 409)
