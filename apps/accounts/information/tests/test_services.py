"""Phase 2 service tests. Audit persistence is integrated in Phase 3."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.contrib.gis.geos import Point
from django.db import close_old_connections, connection
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from decimal import Decimal
from django.utils import timezone

from apps.assignments.models import AccountAssignment
from apps.collection.models import Commission, CommissionRule, InformationCategory, InformationEarningRule, Payment, PromiseToPay, PointRule, PointTransaction
from apps.user.models import ClientAccess, CollectorProfile
from apps.audit.models import AuditEvent
from apps.audit.services import validate_audit_changes

from ..models import AccountInformationProposal as Proposal, CustomerContact
from ..normalizers import normalize_address, normalize_email, normalize_phone, payload_fingerprint
from ..services import (
    InformationWorkflowError, can_validate_information, collector_assignment,
    normalized_proposed_payload, review_proposal, reviewable_proposals, submit_proposal,
)
from ...models import Account, Client, Customer, CustomerAddress


class InformationPureTests(SimpleTestCase):
    def test_conservative_normalization(self):
        self.assertEqual(normalize_email(" Person@EXAMPLE.COM "), "Person@example.com")
        self.assertEqual(normalize_phone("+63 (917) 123-4567"), "+639171234567")
        self.assertNotEqual(normalize_phone("09171234567"), normalize_phone("+639171234567"))
        self.assertEqual(normalize_address("  Block  1\nManila "), "Block 1 Manila")

    def test_fingerprint_is_order_independent_and_payload_sensitive(self):
        self.assertEqual(payload_fingerprint({"b": 2, "a": 1}), payload_fingerprint({"a": 1, "b": 2}))
        self.assertNotEqual(payload_fingerprint({"explanation": "one"}), payload_fingerprint({"explanation": "two"}))
        with self.assertRaises(ValueError):
            payload_fingerprint({"value": float("nan")})

    def test_contact_payload_normalizes(self):
        self.assertEqual(normalized_proposed_payload("phone", {"value": "0917-123-4567"}), {"value": "09171234567"})
        self.assertEqual(normalized_proposed_payload("email", {"value": "x@EXAMPLE.COM"}), {"value": "x@example.com"})

    def test_invalid_payloads_fail(self):
        invalid = [("unknown", {}), ("phone", {"value": "abc"}), ("phone", {"value": "++123"}),
                   ("email", {"value": "not-an-email"}), ("contact_person", {"value": " "}),
                   ("email", {"value": "x@example.com", "extra": "ignored?"}),
                   ("payment_arrangement", {"amount": 100}),
                   ("address", {"kind": "residential", "address_line": "Street", "latitude": 14, "longitude": None}),
                   ("address", {"kind": "residential", "address_line": "Street", "latitude": float("nan"), "longitude": 120})]
        for category, payload in invalid:
            with self.subTest(category=category, payload=payload), self.assertRaises(InformationWorkflowError):
                normalized_proposed_payload(category, payload)

    def test_anonymous_is_denied_without_queries(self):
        with self.assertRaises(InformationWorkflowError) as caught:
            collector_assignment(AnonymousUser(), 1)
        self.assertEqual(caught.exception.status, 403)
        self.assertFalse(can_validate_information(AnonymousUser()))

    def test_rejection_requires_reason_before_queries(self):
        with self.assertRaises(InformationWorkflowError):
            review_proposal(AnonymousUser(), 1, decision="reject", reason="  ")

    def test_backoffice_context_permissions(self):
        from unittest.mock import MagicMock
        from ..services import information_backoffice_context
        user = MagicMock(is_authenticated=True, is_active=True, is_superuser=False)
        user.has_perm.side_effect = lambda p: p == "accounts.validate_account_information"
        with patch("apps.accounts.information.services._active_user", return_value=user):
            context = information_backoffice_context(user)
            self.assertTrue(context["can_validate_information"])
            self.assertIn("can_manage_earning_rules", context)
            self.assertIn("can_review_commissions", context)
            self.assertIn("can_manage_payouts", context)
            self.assertIn("can_manage_point_rules", context)

    def test_award_points_for_proposal_guards_against_pending_and_rejected(self):
        from unittest.mock import MagicMock
        from apps.collection.services import award_points_for_proposal

        pending_prop = MagicMock(status=Proposal.Status.PENDING, category="email")
        self.assertIsNone(award_points_for_proposal(pending_prop))

        rejected_prop = MagicMock(status=Proposal.Status.REJECTED, category="email")
        self.assertIsNone(award_points_for_proposal(rejected_prop))




def information_fixture():
    User = get_user_model()
    collector = User.objects.create_user(username="information-collector")
    reviewer = User.objects.create_superuser(username="information-reviewer", password="test-only")
    client = Client.objects.create(code="INFO-TEST", name="Fictional information client")
    profile = CollectorProfile.objects.create(user=collector, approval_status="approved", approved_by=reviewer, approved_at=timezone.now())
    for user in (collector, reviewer):
        ClientAccess.objects.create(user=user, client=client)
    customer = Customer.objects.create(client=client, source_system="TEST", display_name="Fictional customer")
    address = CustomerAddress.objects.create(customer=customer, kind="residential", address_line="Old fictional street",
        position=Point(120, 14, srid=4326), verification_status="verified", verified_by=reviewer, verified_at=timezone.now())
    rule = CommissionRule.objects.create(client=client, code="INFO", version=1, name="Test terms", kind="percentage", percentage=10, earning_basis="verified_payment")
    account = Account.objects.create(client=client, customer=customer, source_system="TEST", external_id="INFO-1", reference="INFO-1",
        balance_snapshot=1000, balance_as_of=timezone.now(), collection_address=address, commission_rule=rule)
    assignment = AccountAssignment.objects.create(account=account, collector=profile, target_address=address, commission_rule=rule,
        target_position=address.position, policy_snapshot={"test": True}, first_action_due_at=timezone.now() + timedelta(days=1))
    return collector, reviewer, account, assignment


class InformationServiceDatabaseTests(TestCase):
    def setUp(self):
        self.collector, self.reviewer, self.account, self.assignment = information_fixture()
        # Phase 3 adds the real audit allowlist; services always invoke the audit boundary.
        self.audit = patch("apps.accounts.information.services.append_audit_event").start()
        self.addCleanup(patch.stopall)

    def submit(self, **overrides):
        data = dict(event_id=uuid.uuid4(), category="email", proposed_payload={"value": "new@example.test"}, explanation="Customer confirmed this value")
        data.update(overrides)
        return submit_proposal(self.collector, self.assignment.pk, **data)

    def assert_error(self, code, function, *args, **kwargs):
        with self.assertRaises(InformationWorkflowError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_exact_normalized_retry_has_one_proposal_and_audit_call(self):
        token = uuid.uuid4()
        first = self.submit(event_id=token, proposed_payload={"value": "new@EXAMPLE.TEST"})
        second = self.submit(event_id=token)
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["proposal"].pk, second["proposal"].pk)
        self.assertEqual(Proposal.objects.count(), 1)
        self.assertEqual(self.audit.call_count, 1)

    def test_changed_token_payload_conflicts(self):
        token = uuid.uuid4()
        self.submit(event_id=token)
        with self.assertRaises(InformationWorkflowError) as caught:
            self.submit(event_id=token, explanation="Changed explanation")
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(Proposal.objects.count(), 1)
        self.assertEqual(self.audit.call_count, 1)

    def test_pending_and_rejection_leave_authoritative_information_unchanged(self):
        proposal = self.submit()["proposal"]
        self.assertFalse(CustomerContact.objects.exists())
        review_proposal(self.reviewer, proposal.pk, decision="reject", reason="Unable to verify")
        self.assertFalse(CustomerContact.objects.exists())
        self.assertEqual(CustomerAddress.objects.count(), 1)
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance_snapshot, 1000)

    def test_contact_approval_preserves_provenance(self):
        proposal = self.submit()["proposal"]
        reviewed = review_proposal(self.reviewer, proposal.pk, decision="approve")
        contact = CustomerContact.objects.get()
        self.assertEqual(contact.validated_from_proposal_id, proposal.pk)
        self.assertEqual(contact.validated_by_id, self.reviewer.pk)
        self.assertEqual(contact.validated_at, reviewed.reviewed_at)
        self.assertEqual(contact.value, "new@example.test")

    def test_address_approval_adds_address_without_changing_target(self):
        original = self.assignment.target_position.clone()
        old_address_id = self.account.collection_address_id
        proposal = self.submit(category="address", proposed_payload={"kind": "residential", "address_line": "New fictional street", "latitude": 15, "longitude": 121})["proposal"]
        reviewed = review_proposal(self.reviewer, proposal.pk, decision="approve")
        self.assignment.refresh_from_db()
        self.account.refresh_from_db()
        self.assertEqual(CustomerAddress.objects.count(), 2)
        self.assertNotEqual(reviewed.approved_address_id, old_address_id)
        self.assertEqual(self.account.collection_address_id, old_address_id)
        self.assertEqual(self.assignment.target_address_id, old_address_id)
        self.assertEqual(self.assignment.target_position, original)

    def test_payment_arrangement_has_no_financial_or_ownership_side_effects(self):
        account_before = Account.objects.values().get(pk=self.account.pk)
        assignment_before = AccountAssignment.objects.values().get(pk=self.assignment.pk)
        proposal = self.submit(category="payment_arrangement", proposed_payload={"description": "Customer proposes discussing monthly payments"})["proposal"]
        review_proposal(self.reviewer, proposal.pk, decision="approve")
        self.assertEqual(Account.objects.values().get(pk=self.account.pk), account_before)
        self.assertEqual(AccountAssignment.objects.values().get(pk=self.assignment.pk), assignment_before)
        self.assertEqual(Payment.objects.count(), 0)
        self.assertEqual(PromiseToPay.objects.count(), 0)
        self.assertEqual(CustomerContact.objects.count(), 0)

    def test_self_review_is_denied(self):
        proposal = self.submit()["proposal"]
        get_user_model().objects.filter(pk=self.collector.pk).update(is_superuser=True)
        self.assert_error("self_review", review_proposal, self.collector, proposal.pk, decision="approve")

    def test_final_decision_cannot_be_overwritten_or_repeated(self):
        proposal = self.submit()["proposal"]
        review_proposal(self.reviewer, proposal.pk, decision="approve")
        self.assert_error("review_conflict", review_proposal, self.reviewer, proposal.pk, decision="reject", reason="Changed mind")
        self.assert_error("review_conflict", review_proposal, self.reviewer, proposal.pk, decision="approve")
        self.assertEqual(CustomerContact.objects.count(), 1)
        self.assertEqual(self.audit.call_count, 2)

    def test_former_owner_cannot_read_or_retry_but_staff_can_review(self):
        token = uuid.uuid4()
        proposal = self.submit(event_id=token)["proposal"]
        AccountAssignment.objects.filter(pk=self.assignment.pk).update(status="released", ended_at=timezone.now())
        self.assert_error("not_found", collector_assignment, self.collector, self.assignment.pk)
        self.assert_error("not_found", self.submit, event_id=token)
        review_proposal(self.reviewer, proposal.pk, decision="approve")

    def test_wrong_client_submission_and_review_are_denied(self):
        ClientAccess.objects.filter(user=self.collector).update(is_active=False)
        self.assert_error("client_access_required", self.submit)
        ClientAccess.objects.filter(user=self.collector).update(is_active=True)
        proposal = self.submit()["proposal"]
        ClientAccess.objects.filter(user=self.reviewer).update(is_active=False)
        self.assert_error("client_access_required", review_proposal, self.reviewer, proposal.pk, decision="approve")
        self.assertFalse(reviewable_proposals(self.reviewer).exists())

    def test_stale_current_values_block_approval(self):
        first = self.submit()["proposal"]
        second = self.submit(proposed_payload={"value": "other@example.test"})["proposal"]
        review_proposal(self.reviewer, first.pk, decision="approve")
        self.assert_error("stale_information", review_proposal, self.reviewer, second.pk, decision="approve")
        second.refresh_from_db()
        self.assertEqual(second.status, "pending")

    def test_audit_failure_rolls_back_submission(self):
        self.audit.side_effect = RuntimeError("Audit unavailable")
        with self.assertRaises(RuntimeError):
            self.submit()
        self.assertFalse(Proposal.objects.exists())

    def test_audit_failure_rolls_back_contact_and_decision(self):
        proposal = self.submit()["proposal"]
        self.audit.side_effect = RuntimeError("Audit unavailable")
        with self.assertRaises(RuntimeError):
            review_proposal(self.reviewer, proposal.pk, decision="approve")
        self.assertFalse(CustomerContact.objects.exists())
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, "pending")

    def test_audit_metadata_does_not_include_customer_values(self):
        proposal = self.submit()["proposal"]
        self.assertEqual(self.audit.call_args.kwargs["changes"], {"category": "email", "status": "pending"})
        review_proposal(self.reviewer, proposal.pk, decision="approve")
        self.assertEqual(self.audit.call_args.kwargs["changes"], {"category": "email", "status": "approved"})

    def test_proposal_approval_with_active_matching_rule_creates_one_commission(self):
        rule = InformationEarningRule.objects.create(
            client=self.account.client,
            category=InformationCategory.EMAIL,
            amount=Decimal("150.00"),
            currency="PHP",
            version=1,
            is_active=True,
            created_by=self.reviewer,
        )
        proposal = self.submit(category="email")["proposal"]
        reviewed = review_proposal(self.reviewer, proposal.pk, decision="approve")
        self.assertEqual(reviewed.status, "approved")

        commissions = Commission.objects.filter(proposal=proposal)
        self.assertEqual(commissions.count(), 1)
        commission = commissions.first()
        self.assertEqual(commission.client, self.account.client)
        self.assertEqual(commission.collector, self.assignment.collector)
        self.assertEqual(commission.category, "email")
        self.assertEqual(commission.amount, Decimal("150.00"))
        self.assertEqual(commission.currency, "PHP")
        self.assertEqual(commission.rule_version, 1)
        self.assertEqual(commission.earning_rule, rule)
        self.assertEqual(commission.status, Commission.Status.PENDING)

    def test_proposal_approval_with_no_active_rate_creates_no_commission(self):
        proposal = self.submit(category="email")["proposal"]
        reviewed = review_proposal(self.reviewer, proposal.pk, decision="approve")
        self.assertEqual(reviewed.status, "approved")
        self.assertFalse(Commission.objects.filter(proposal=proposal).exists())

    def test_proposal_rejection_creates_no_commission(self):
        InformationEarningRule.objects.create(
            client=self.account.client,
            category=InformationCategory.EMAIL,
            amount=Decimal("150.00"),
            currency="PHP",
            version=1,
            is_active=True,
            created_by=self.reviewer,
        )
        proposal = self.submit(category="email")["proposal"]
        reviewed = review_proposal(self.reviewer, proposal.pk, decision="reject", reason="Unable to verify")
        self.assertEqual(reviewed.status, "rejected")
        self.assertFalse(Commission.objects.filter(proposal=proposal).exists())

    def test_proposal_approval_with_active_point_rule_creates_one_point_transaction(self):
        point_rule = PointRule.objects.create(
            category=InformationCategory.EMAIL,
            version=1,
            points=1,
            is_active=True,
            created_by=self.reviewer,
        )
        proposal = self.submit(category="email")["proposal"]
        reviewed = review_proposal(self.reviewer, proposal.pk, decision="approve")
        self.assertEqual(reviewed.status, "approved")

        txns = PointTransaction.objects.filter(proposal=proposal)
        self.assertEqual(txns.count(), 1)
        txn = txns.first()
        self.assertEqual(txn.collector, self.assignment.collector)
        self.assertEqual(txn.category, "email")
        self.assertEqual(txn.points, 1)
        self.assertEqual(txn.rule_version, 1)
        self.assertEqual(txn.point_rule, point_rule)
        self.assertEqual(txn.approved_by, self.reviewer)

    def test_proposal_approval_with_no_active_point_rule_creates_no_points_and_succeeds(self):
        proposal = self.submit(category="email")["proposal"]
        reviewed = review_proposal(self.reviewer, proposal.pk, decision="approve")
        self.assertEqual(reviewed.status, "approved")
        self.assertFalse(PointTransaction.objects.filter(proposal=proposal).exists())

    def test_proposal_rejection_creates_no_point_transaction(self):
        PointRule.objects.create(
            category=InformationCategory.EMAIL,
            version=1,
            points=1,
            is_active=True,
            created_by=self.reviewer,
        )
        proposal = self.submit(category="email")["proposal"]
        reviewed = review_proposal(self.reviewer, proposal.pk, decision="reject", reason="Unable to verify")
        self.assertEqual(reviewed.status, "rejected")
        self.assertFalse(PointTransaction.objects.filter(proposal=proposal).exists())

    def test_award_points_service_directly_rejects_pending_proposal(self):
        from apps.collection.services import award_points_for_proposal
        PointRule.objects.create(
            category=InformationCategory.EMAIL,
            version=1,
            points=1,
            is_active=True,
            created_by=self.reviewer,
        )
        proposal = self.submit(category="email")["proposal"]
        self.assertEqual(proposal.status, Proposal.Status.PENDING)
        self.assertIsNone(award_points_for_proposal(proposal, user=self.reviewer))
        self.assertFalse(PointTransaction.objects.filter(proposal=proposal).exists())


    def test_points_do_not_affect_commissions_or_account_balances(self):
        InformationEarningRule.objects.create(
            client=self.account.client,
            category=InformationCategory.EMAIL,
            amount=Decimal("150.00"),
            currency="PHP",
            version=1,
            is_active=True,
            created_by=self.reviewer,
        )
        PointRule.objects.create(
            category=InformationCategory.EMAIL,
            version=1,
            points=1,
            is_active=True,
            created_by=self.reviewer,
        )
        initial_balance = self.account.balance_snapshot
        proposal = self.submit(category="email")["proposal"]
        review_proposal(self.reviewer, proposal.pk, decision="approve")

        commission = Commission.objects.get(proposal=proposal)
        self.assertEqual(commission.amount, Decimal("150.00"))

        point_txn = PointTransaction.objects.get(proposal=proposal)
        self.assertEqual(point_txn.points, 1)

        self.account.refresh_from_db()
        self.assertEqual(self.account.balance_snapshot, initial_balance)


class InformationConcurrencyTests(TransactionTestCase):
    def test_competing_reviewers_only_finalize_once(self):
        if connection.vendor != "postgresql":
            self.skipTest("Requires disposable PostGIS database")
        collector, reviewer, account, assignment = information_fixture()
        other = get_user_model().objects.create_superuser(username="information-other-reviewer", password="test-only")
        ClientAccess.objects.create(user=other, client=account.client)
        with patch("apps.accounts.information.services.append_audit_event"):
            proposal = submit_proposal(collector, assignment.pk, event_id=uuid.uuid4(), category="email", proposed_payload={"value": "one@example.test"}, explanation="Confirmed")["proposal"]
        barrier = Barrier(2)

        def decide(user_id):
            close_old_connections()
            try:
                user = get_user_model().objects.get(pk=user_id)
                barrier.wait(timeout=10)
                try:
                    review_proposal(user, proposal.pk, decision="approve")
                    return "approved"
                except InformationWorkflowError as exc:
                    return exc.code
            finally:
                close_old_connections()

        with patch("apps.accounts.information.services.append_audit_event") as audit:
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(decide, [reviewer.pk, other.pk]))
        self.assertCountEqual(results, ["approved", "review_conflict"])
        self.assertEqual(CustomerContact.objects.count(), 1)
        self.assertEqual(audit.call_count, 1)


class InformationAuditIntegrationTests(TestCase):
    def setUp(self):
        self.collector, self.reviewer, self.account, self.assignment = information_fixture()

    def submit(self, token):
        return submit_proposal(self.collector, self.assignment.pk, event_id=token, category="email",
            proposed_payload={"value": "private@example.test"}, explanation="Customer confirmed private information")

    def test_real_audit_submission_retry_and_approval(self):
        token = uuid.uuid4()
        proposal = self.submit(token)["proposal"]
        self.submit(token)
        self.assertEqual(AuditEvent.objects.count(), 1)
        review_proposal(self.reviewer, proposal.pk, decision="approve")
        events = list(AuditEvent.objects.order_by("pk"))
        self.assertEqual([event.action for event in events], ["information_proposal_submitted", "information_proposal_approved"])
        for event, status, actor in zip(events, ["pending", "approved"], [self.collector, self.reviewer]):
            self.assertEqual(event.changes, {"category": "email", "status": status})
            self.assertEqual(event.actor_id, actor.pk)
            self.assertEqual(event.account_id, self.account.pk)
            self.assertEqual(event.entity_id, str(proposal.pk))
            self.assertEqual(event.request_id, token)

    def test_real_rejection_audit_and_finalized_retry(self):
        proposal = self.submit(uuid.uuid4())["proposal"]
        review_proposal(self.reviewer, proposal.pk, decision="reject", reason="Unable to independently confirm")
        with self.assertRaises(InformationWorkflowError):
            review_proposal(self.reviewer, proposal.pk, decision="reject", reason="Unable to independently confirm")
        self.assertEqual(AuditEvent.objects.count(), 2)
        self.assertEqual(AuditEvent.objects.latest("pk").action, "information_proposal_rejected")
        self.assertFalse(CustomerContact.objects.exists())

    def test_audit_allowlists_reject_private_fields(self):
        for action in ("information_proposal_submitted", "information_proposal_approved", "information_proposal_rejected"):
            for field in ("address", "email", "phone", "payment_terms", "customer", "explanation", "review_reason"):
                with self.subTest(action=action, field=field), self.assertRaises(ValidationError):
                    validate_audit_changes(action, {field: "Private value"})

    def test_real_audit_insert_failure_rolls_back_approval_and_contact(self):
        proposal = self.submit(uuid.uuid4())["proposal"]
        with patch("apps.audit.services.AuditEvent.save", side_effect=RuntimeError("Audit persistence failed")):
            with self.assertRaises(RuntimeError):
                review_proposal(self.reviewer, proposal.pk, decision="approve")
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, "pending")
        self.assertFalse(CustomerContact.objects.exists())
        self.assertEqual(AuditEvent.objects.count(), 1)
