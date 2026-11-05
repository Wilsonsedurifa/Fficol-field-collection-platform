from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase

from apps.accounts.models import Client

from .models import CommissionRule, CommissionTier, Payment, PromiseToPay
from .services import validate_commission_terms


class PromiseValidationTests(SimpleTestCase):
    def test_positive_two_decimal_amount_is_required(self):
        field=PromiseToPay._meta.get_field('amount')
        self.assertEqual(field.clean('250.00',None),Decimal('250.00'))
        for amount in ['0','-1','1.001','1000000000000']:
            with self.assertRaises(ValidationError):field.clean(amount,None)

    def test_promise_requires_a_submitted_promise_activity(self):
        from apps.activities.models import FieldActivity
        activity=FieldActivity(pk=1,status='in_progress',outcome='')
        promise=PromiseToPay(activity=activity,amount=Decimal('10'))
        with self.assertRaises(ValidationError):promise.clean()
        activity.status='submitted';activity.outcome='promise_to_pay';promise.clean()


class CommissionFoundationValidationTests(SimpleTestCase):
    def rule(self, **values):
        defaults = dict(pk=1, client_id=1, code="TEST", version=1, name="Provisional terms", earning_basis="verified_payment", kind="percentage", percentage=Decimal("10"))
        defaults.update(values)
        return CommissionRule(**defaults)

    def test_percentage_and_fixed_configurations(self):
        validate_commission_terms(self.rule(), tiers=[])
        validate_commission_terms(self.rule(kind="fixed", percentage=None, fixed_amount=Decimal("250"), fixed_frequency="once_per_account"), tiers=[])

    def test_mixed_kinds_or_negative_percentages_are_rejected(self):
        for rule in [self.rule(fixed_amount=Decimal("1")), self.rule(percentage=Decimal("101")), self.rule(percentage=Decimal("-1")), self.rule(kind="fixed", percentage=None, fixed_amount=Decimal("250"))]:
            with self.subTest(kind=rule.kind), self.assertRaises(ValidationError):
                validate_commission_terms(rule, tiers=[])

    def test_adjacent_tiers_cover_zero_through_unbounded_upper(self):
        rule = self.rule(kind="tiered", percentage=None, tier_mode="marginal")
        bands = [CommissionTier(rule=rule, lower_amount=Decimal("0"), upper_amount=Decimal("5000"), percentage=Decimal("8")), CommissionTier(rule=rule, lower_amount=Decimal("5000"), upper_amount=None, percentage=Decimal("12"))]
        validate_commission_terms(rule, tiers=list(reversed(bands)))

    def test_gap_overlap_and_missing_final_coverage_are_rejected(self):
        rule = self.rule(kind="tiered", percentage=None, tier_mode="whole_amount")
        for lower, upper in [(Decimal("5001"), None), (Decimal("4999"), None), (Decimal("5000"), Decimal("10000"))]:
            bands = [CommissionTier(rule=rule, lower_amount=Decimal("0"), upper_amount=Decimal("5000"), percentage=Decimal("8")), CommissionTier(rule=rule, lower_amount=lower, upper_amount=upper, percentage=Decimal("12"))]
            with self.subTest(lower=lower, upper=upper), self.assertRaises(ValidationError):
                validate_commission_terms(rule, tiers=bands)

    def test_tiers_cannot_belong_to_another_version(self):
        rule = self.rule(kind="tiered", percentage=None, tier_mode="whole_amount")
        band = CommissionTier(rule_id=2, lower_amount=Decimal("0"), percentage=Decimal("10"))
        with self.assertRaises(ValidationError):
            validate_commission_terms(rule, tiers=[band])


class CommissionFoundationDatabaseTests(TestCase):
    def setUp(self):
        client = Client.objects.create(code="TEST-RATES", name="Fictional Client")
        self.rule = CommissionRule.objects.create(client=client, code="TEST", version=1, name="Provisional tiered terms", kind="tiered", tier_mode="whole_amount", earning_basis="verified_payment")

    def test_database_rejects_overlap_and_accepts_adjacent_bands(self):
        CommissionTier.objects.create(rule=self.rule, lower_amount=0, upper_amount=5000, percentage=8)
        with self.assertRaises(IntegrityError), transaction.atomic():
            CommissionTier.objects.create(rule=self.rule, lower_amount=4999, upper_amount=None, percentage=12)
        CommissionTier.objects.create(rule=self.rule, lower_amount=5000, upper_amount=None, percentage=12)
        validate_commission_terms(self.rule)

    def test_database_rejects_invalid_kind_configuration(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            CommissionRule.objects.create(client=self.rule.client, code="BAD", version=1, name="Invalid", kind="percentage", fixed_amount=1, earning_basis="verified_payment")


class PaymentModelValidationTests(SimpleTestCase):
    def test_payment_amount_validation(self):
        field = Payment._meta.get_field("amount")
        self.assertEqual(field.clean("1500.00", None), Decimal("1500.00"))
        for bad in ["0", "-100", "0.001", "10000000000000"]:
            with self.assertRaises(ValidationError):
                field.clean(bad, None)

    def test_payment_clean_checks_account_consistency(self):
        from apps.accounts.models import Account
        from apps.assignments.models import AccountAssignment

        acc1 = Account(pk=101)
        acc2 = Account(pk=102)
        assign = AccountAssignment(pk=501, account=acc2)

        pmt = Payment(account=acc1, assignment=assign, amount=Decimal("500"), status="reported")
        with self.assertRaises(ValidationError):
            pmt.clean()

    def test_payment_clean_requires_reasons(self):
        from apps.accounts.models import Account
        acc = Account(pk=101)
        pmt_rejected = Payment(account=acc, amount=Decimal("500"), status="rejected", rejection_reason="")
        with self.assertRaises(ValidationError):
            pmt_rejected.clean()

        pmt_reversed = Payment(account=acc, amount=Decimal("500"), status="reversed", reversal_reason="")
        with self.assertRaises(ValidationError):
            pmt_reversed.clean()


class DocumentUploadValidationTests(SimpleTestCase):
    def test_valid_pdf_upload(self):
        from .services import validate_uploaded_document
        content = b"%PDF-1.4\n" + b"A" * 200
        file_obj = SimpleUploadedFile("receipt.pdf", content, content_type="application/pdf")
        meta = validate_uploaded_document(file_obj)
        self.assertEqual(meta["mime_type"], "application/pdf")
        self.assertEqual(meta["original_name"], "receipt.pdf")
        self.assertGreater(meta["size_bytes"], 100)
        self.assertTrue(len(meta["sha256"]) == 64)

    def test_valid_jpeg_and_png_upload(self):
        from .services import validate_uploaded_document
        jpeg_content = b"\xff\xd8\xff\xe0" + b"B" * 200
        f_jpg = SimpleUploadedFile("slip.jpg", jpeg_content, content_type="image/jpeg")
        m_jpg = validate_uploaded_document(f_jpg)
        self.assertEqual(m_jpg["mime_type"], "image/jpeg")

        png_content = b"\x89PNG\r\n\x1a\n" + b"C" * 200
        f_png = SimpleUploadedFile("slip.png", png_content, content_type="image/png")
        m_png = validate_uploaded_document(f_png)
        self.assertEqual(m_png["mime_type"], "image/png")

    def test_rejection_of_tiny_corrupted_or_oversized_file(self):
        from .services import validate_uploaded_document, CollectionWorkflowError
        tiny_file = SimpleUploadedFile("tiny.jpg", b"\xff\xd8\xff", content_type="image/jpeg")
        with self.assertRaises(CollectionWorkflowError):
            validate_uploaded_document(tiny_file)

        with self.assertRaises(CollectionWorkflowError):
            validate_uploaded_document(None)

    def test_rejection_of_unsupported_extension_or_fake_content(self):
        from .services import validate_uploaded_document, CollectionWorkflowError
        # .exe extension rejected
        exe_file = SimpleUploadedFile("malware.exe", b"%PDF-1.4" + b"X" * 200, content_type="application/pdf")
        with self.assertRaises(CollectionWorkflowError):
            validate_uploaded_document(exe_file)

        # fake pdf with jpg extension
        fake_jpg = SimpleUploadedFile("fake.jpg", b"%PDF-1.4" + b"X" * 200, content_type="image/jpeg")
        with self.assertRaises(CollectionWorkflowError):
            validate_uploaded_document(fake_jpg)


class PaymentReportingValidationTests(SimpleTestCase):
    def test_validate_payment_report_valid(self):
        from .services import validate_payment_report
        from uuid import uuid4
        from django.utils import timezone

        now = timezone.now()
        pdf_content = b"%PDF-1.4\n" + b"Z" * 200
        file_obj = SimpleUploadedFile("proof.pdf", pdf_content, content_type="application/pdf")

        payload = {
            "event_id": str(uuid4()),
            "amount": "2500.50",
            "paid_at": now.isoformat(),
            "method": "bank_transfer",
            "reference": "DEP-12345",
            "notes": "Direct bank deposit proof",
        }
        validated = validate_payment_report(payload, file_obj, now)
        self.assertEqual(validated["amount"], Decimal("2500.50"))
        self.assertEqual(validated["method"], "bank_transfer")
        self.assertEqual(validated["reference"], "DEP-12345")

    def test_validate_payment_report_future_rejection(self):
        from .services import validate_payment_report, CollectionWorkflowError
        from uuid import uuid4
        from datetime import timedelta
        from django.utils import timezone

        now = timezone.now()
        pdf_content = b"%PDF-1.4\n" + b"Z" * 200
        file_obj = SimpleUploadedFile("proof.pdf", pdf_content, content_type="application/pdf")

        payload = {
            "event_id": str(uuid4()),
            "amount": "500.00",
            "paid_at": (now + timedelta(hours=2)).isoformat(),
            "method": "e_wallet",
        }
        with self.assertRaises(CollectionWorkflowError):
            validate_payment_report(payload, file_obj, now)


class PaymentReviewValidationTests(SimpleTestCase):
    def test_review_validation(self):
        from .services import validate_payment_review, CollectionWorkflowError

        self.assertEqual(validate_payment_review("verify", "Matched deposit"), "Matched deposit")
        self.assertEqual(validate_payment_review("verify", ""), "")

        # Rejection requires reason
        with self.assertRaises(CollectionWorkflowError):
            validate_payment_review("reject", "")

        self.assertEqual(validate_payment_review("reject", "Unclear deposit slip"), "Unclear deposit slip")

        # Invalid decision
        with self.assertRaises(CollectionWorkflowError):
            validate_payment_review("delete", "some reason")


class DocumentAccessPermissionTests(SimpleTestCase):
    def test_access_rules(self):
        from unittest.mock import MagicMock, patch
        from .services import can_access_document
        from apps.activities.models import Document
        from apps.accounts.models import Account, Client
        from apps.user.models import User

        client = Client(pk=1, is_active=True)
        acc = Account(pk=10, client=client)
        uploader = User(pk=1, is_active=True)
        other_user = User(pk=2, is_active=True)
        anon = User(pk=3, is_active=False)

        doc = Document(pk=99, account=acc, uploaded_by=uploader)

        # Inactive user denied
        self.assertFalse(can_access_document(anon, doc))

        # Former collector whose assignment is released (or not active) is DENIED even though they uploaded it
        with patch("apps.collection.services.has_backoffice_access", return_value=False), \
             patch("apps.collection.services.CollectorProfile.objects.filter") as mock_prof, \
             patch("apps.collection.services.AccountAssignment.objects.filter") as mock_assign, \
             patch("apps.collection.services.ClientAccess.objects.filter") as mock_access:
            
            mock_prof.return_value.first.return_value = MagicMock()
            # No active assignment (e.g. released)
            mock_assign.return_value.exists.return_value = False
            mock_access.return_value.exists.return_value = True

            self.assertFalse(can_access_document(uploader, doc))

        # Active collector with active assignment and active client access is ALLOWED
        with patch("apps.collection.services.has_backoffice_access", return_value=False), \
             patch("apps.collection.services.CollectorProfile.objects.filter") as mock_prof, \
             patch("apps.collection.services.AccountAssignment.objects.filter") as mock_assign, \
             patch("apps.collection.services.ClientAccess.objects.filter") as mock_access:
            
            mock_prof.return_value.first.return_value = MagicMock()
            mock_assign.return_value.exists.return_value = True
            mock_access.return_value.exists.return_value = True

            self.assertTrue(can_access_document(uploader, doc))

        # Back Office user with active ClientAccess is ALLOWED
        bo_user = User(pk=5, is_active=True)
        with patch("apps.collection.services.has_backoffice_access", return_value=True), \
             patch("apps.collection.services.ClientAccess.objects.filter") as mock_access:
            
            mock_access.return_value.exists.return_value = True
            self.assertTrue(can_access_document(bo_user, doc))

            mock_access.return_value.exists.return_value = False
            self.assertFalse(can_access_document(bo_user, doc))


# ---------------------------------------------------------------------------
# Finance Models & Services Tests
# ---------------------------------------------------------------------------

from django.utils import timezone
from .models import InformationCategory, InformationEarningRule, Commission, Payout, PayoutItem, PointRule, PointTransaction
from .services import (
    can_manage_earning_rules,
    can_review_commissions,
    can_manage_payouts,
    can_manage_point_rules,
    publish_earning_rule,
    publish_point_rule,
    review_commission,
    create_payout,
    approve_payout,
    mark_payout_paid,
    collector_earnings_summary,
    collector_points_summary,
    collector_point_transactions_list,
    reviewable_point_rules,
    create_commission_for_proposal,
    award_points_for_proposal,
    CollectionWorkflowError,
)


class InformationEarningRuleModelTests(SimpleTestCase):
    def test_amount_positive_validation(self):
        rule = InformationEarningRule(amount=Decimal("0.00"), currency="PHP", version=1, is_active=True)
        with self.assertRaises(ValidationError):
            rule.clean()

    def test_currency_must_be_php(self):
        rule = InformationEarningRule(amount=Decimal("100.00"), currency="USD", version=1, is_active=True)
        with self.assertRaises(ValidationError):
            rule.clean()

    def test_version_must_be_positive(self):
        rule = InformationEarningRule(amount=Decimal("100.00"), currency="PHP", version=0, is_active=True)
        with self.assertRaises(ValidationError):
            rule.clean()

    def test_retirement_consistency(self):
        now = timezone.now()
        # Active rule cannot have retirement metadata
        rule = InformationEarningRule(amount=Decimal("100.00"), currency="PHP", version=1, is_active=True, retired_at=now)
        with self.assertRaises(ValidationError):
            rule.clean()
        # Inactive rule must have retired_at
        rule = InformationEarningRule(amount=Decimal("100.00"), currency="PHP", version=1, is_active=False, retired_at=None)
        with self.assertRaises(ValidationError):
            rule.clean()


class CommissionModelTests(SimpleTestCase):
    def test_amount_positive_validation(self):
        comm = Commission(amount=Decimal("0.00"), currency="PHP", status="pending")
        with self.assertRaises(ValidationError):
            comm.clean()

    def test_currency_must_be_php(self):
        comm = Commission(amount=Decimal("50.00"), currency="USD", status="pending")
        with self.assertRaises(ValidationError):
            comm.clean()

    def test_pending_commission_cannot_have_review_metadata(self):
        from apps.user.models import User
        reviewer = User(pk=1)
        comm = Commission(amount=Decimal("50.00"), currency="PHP", status="pending", reviewed_by=reviewer)
        with self.assertRaises(ValidationError):
            comm.clean()

    def test_reviewed_commission_must_have_review_metadata(self):
        comm = Commission(amount=Decimal("50.00"), currency="PHP", status="approved")
        with self.assertRaises(ValidationError):
            comm.clean()

    def test_rejection_reason_only_for_rejected_commissions(self):
        from apps.user.models import User
        reviewer = User(pk=1)
        now = timezone.now()
        comm = Commission(amount=Decimal("50.00"), currency="PHP", status="approved", reviewed_by=reviewer, reviewed_at=now, rejection_reason="Bad")
        with self.assertRaises(ValidationError):
            comm.clean()


class PayoutModelTests(SimpleTestCase):
    def test_payout_amount_nonnegative(self):
        payout = Payout(amount=Decimal("-1.00"), currency="PHP", status="draft")
        with self.assertRaises(ValidationError):
            payout.clean()

    def test_payout_currency_must_be_php(self):
        payout = Payout(amount=Decimal("100.00"), currency="EUR", status="draft")
        with self.assertRaises(ValidationError):
            payout.clean()

    def test_draft_payout_cannot_have_approval_or_payment(self):
        from apps.user.models import User
        user = User(pk=1)
        now = timezone.now()
        payout = Payout(amount=Decimal("100.00"), currency="PHP", status="draft", approved_by=user, approved_at=now)
        with self.assertRaises(ValidationError):
            payout.clean()

    def test_approved_payout_must_have_approval_and_no_payment(self):
        from apps.user.models import User
        user = User(pk=1)
        now = timezone.now()
        payout = Payout(amount=Decimal("100.00"), currency="PHP", status="approved")
        with self.assertRaises(ValidationError):
            payout.clean()
        payout = Payout(amount=Decimal("100.00"), currency="PHP", status="approved", approved_by=user, approved_at=now, paid_by=user, paid_at=now)
        with self.assertRaises(ValidationError):
            payout.clean()

    def test_paid_payout_must_have_approval_and_payment(self):
        from apps.user.models import User
        user = User(pk=1)
        now = timezone.now()
        payout = Payout(amount=Decimal("100.00"), currency="PHP", status="paid", approved_by=user, approved_at=now)
        with self.assertRaises(ValidationError):
            payout.clean()


class PayoutItemModelTests(SimpleTestCase):
    def test_item_amount_must_be_positive(self):
        item = PayoutItem(amount=Decimal("0.00"))
        with self.assertRaises(ValidationError):
            item.clean()


class FinanceServicesValidationTests(SimpleTestCase):
    def test_permission_helpers(self):
        from unittest.mock import MagicMock
        user = MagicMock()
        user.is_authenticated = True
        user.is_active = True
        user.is_superuser = False

        user.has_perm.side_effect = lambda p: p == "collection.manage_earning_rules"
        self.assertTrue(can_manage_earning_rules(user))
        self.assertFalse(can_review_commissions(user))
        self.assertFalse(can_manage_payouts(user))

        user.has_perm.side_effect = lambda p: p == "collection.review_commissions"
        self.assertFalse(can_manage_earning_rules(user))
        self.assertTrue(can_review_commissions(user))
        self.assertFalse(can_manage_payouts(user))

        user.has_perm.side_effect = lambda p: p == "collection.manage_payouts"
        self.assertFalse(can_manage_earning_rules(user))
        self.assertFalse(can_review_commissions(user))
        self.assertTrue(can_manage_payouts(user))

    def test_collector_earnings_summary(self):
        from unittest.mock import patch, MagicMock
        collector = MagicMock(pk=1)
        with patch("apps.collection.services.Commission.objects.filter") as mock_filter:
            qs = mock_filter.return_value
            qs.filter.return_value.aggregate.side_effect = [
                {"s": Decimal("150.00")},
                {"s": Decimal("300.00")},
                {"s": Decimal("450.00")},
            ]
            summary = collector_earnings_summary(collector)
            self.assertEqual(summary["awaiting_approval"], Decimal("150.00"))
            self.assertEqual(summary["approved_unpaid"], Decimal("300.00"))
            self.assertEqual(summary["paid_out"], Decimal("450.00"))
            self.assertEqual(summary["total"], Decimal("900.00"))

    def test_publish_earning_rule_validations(self):
        from unittest.mock import MagicMock, patch
        user = MagicMock(is_authenticated=True, is_active=True, is_superuser=False)
        user.has_perm.return_value = False

        with self.assertRaises(CollectionWorkflowError) as ctx:
            publish_earning_rule(user, client_id=1, category="address", amount="100.00")
        self.assertEqual(ctx.exception.code, "permission_required")

        user.has_perm.return_value = True
        with patch("apps.collection.services.Client.objects.filter") as mock_client, \
             patch("apps.collection.services.ClientAccess.objects.filter") as mock_access:
            mock_client.return_value.first.return_value = MagicMock(pk=1)
            mock_access.return_value.exists.return_value = True

            with self.assertRaises(CollectionWorkflowError) as ctx:
                publish_earning_rule(user, client_id=1, category="invalid_cat", amount="100.00")
            self.assertEqual(ctx.exception.code, "invalid_category")

            for bad_amount in ["0", "-50", "abc"]:
                with self.assertRaises(CollectionWorkflowError) as ctx:
                    publish_earning_rule(user, client_id=1, category="address", amount=bad_amount)
                self.assertEqual(ctx.exception.code, "invalid_amount")

    def test_review_commission_validations(self):
        from unittest.mock import MagicMock
        user = MagicMock(is_authenticated=True, is_active=True, is_superuser=True)

        with self.assertRaises(CollectionWorkflowError) as ctx:
            review_commission(user, 1, decision="invalid")
        self.assertEqual(ctx.exception.code, "invalid_decision")

        with self.assertRaises(CollectionWorkflowError) as ctx:
            review_commission(user, 1, decision="reject", reason="")
        self.assertEqual(ctx.exception.code, "reason_required")

    def test_mark_payout_paid_validations(self):
        from unittest.mock import MagicMock
        user = MagicMock(is_authenticated=True, is_active=True, is_superuser=True)

        with self.assertRaises(CollectionWorkflowError) as ctx:
            mark_payout_paid(user, 1, reference="")
        self.assertEqual(ctx.exception.code, "reference_required")

        with self.assertRaises(CollectionWorkflowError) as ctx:
            mark_payout_paid(user, 1, reference="x" * 101)
        self.assertEqual(ctx.exception.code, "reference_too_long")


class FinanceDatabaseTests(TestCase):
    def setUp(self):
        import uuid
        from datetime import timedelta
        from django.contrib.auth import get_user_model
        from django.contrib.gis.geos import Point
        from apps.accounts.models import Account, Client, Customer, CustomerAddress
        from apps.assignments.models import AccountAssignment
        from apps.user.models import ClientAccess, CollectorProfile

        User = get_user_model()
        self.client_obj = Client.objects.create(code="FIN-TEST", name="Finance Test Client")
        self.reviewer = User.objects.create_superuser(username="fin-reviewer", password="test-only")
        self.collector_user = User.objects.create_user(username="fin-collector")
        self.collector = CollectorProfile.objects.create(
            user=self.collector_user,
            approval_status="approved",
            approved_by=self.reviewer,
            approved_at=timezone.now(),
        )
        ClientAccess.objects.create(user=self.reviewer, client=self.client_obj, is_active=True)
        ClientAccess.objects.create(user=self.collector_user, client=self.client_obj, is_active=True)

        self.customer = Customer.objects.create(
            client=self.client_obj,
            source_system="TEST",
            display_name="Finance Customer",
        )
        self.address = CustomerAddress.objects.create(
            customer=self.customer,
            kind="residential",
            address_line="123 Financial Way",
            position=Point(120.9842, 14.5995, srid=4326),
            verification_status="verified",
            verified_by=self.reviewer,
            verified_at=timezone.now(),
        )
        self.account = Account.objects.create(
            client=self.client_obj,
            customer=self.customer,
            source_system="TEST",
            external_id="FIN-ACC-1",
            reference="FIN-ACC-1",
            balance_snapshot=Decimal("10000.00"),
            balance_as_of=timezone.now(),
            collection_address=self.address,
        )
        self.assignment = AccountAssignment.objects.create(
            account=self.account,
            collector=self.collector,
            target_address=self.address,
            target_position=self.address.position,
            policy_snapshot={"test": True},
            first_action_due_at=timezone.now() + timedelta(days=1),
        )

    def _create_proposal(self, category="email", payload=None):
        import uuid
        from apps.accounts.information.models import AccountInformationProposal
        if payload is None:
            payload = {"value": f"user-{uuid.uuid4().hex[:6]}@example.com"}
        return AccountInformationProposal.objects.create(
            account=self.account,
            customer=self.customer,
            assignment=self.assignment,
            submitted_by=self.collector_user,
            category=category,
            current_snapshot={},
            proposed_payload=payload,
            explanation="Customer confirmed value",
            event_id=uuid.uuid4(),
            payload_fingerprint=uuid.uuid4().hex,
            status=AccountInformationProposal.Status.APPROVED,
            reviewed_by=self.reviewer,
            reviewed_at=timezone.now(),
        )

    def test_later_rule_version_retires_earlier_rule_and_preserves_existing_commission_snapshot(self):
        rule_v1 = publish_earning_rule(self.reviewer, client_id=self.client_obj.pk, category="email", amount="200.00")
        self.assertEqual(rule_v1.version, 1)
        self.assertTrue(rule_v1.is_active)

        proposal1 = self._create_proposal("email")
        comm1 = create_commission_for_proposal(proposal1, user=self.reviewer)
        self.assertEqual(comm1.amount, Decimal("200.00"))
        self.assertEqual(comm1.rule_version, 1)
        self.assertEqual(comm1.earning_rule_id, rule_v1.pk)

        # Publish version 2
        rule_v2 = publish_earning_rule(self.reviewer, client_id=self.client_obj.pk, category="email", amount="350.00")
        self.assertEqual(rule_v2.version, 2)
        self.assertTrue(rule_v2.is_active)

        rule_v1.refresh_from_db()
        self.assertFalse(rule_v1.is_active)
        self.assertIsNotNone(rule_v1.retired_at)
        self.assertEqual(rule_v1.retired_by, self.reviewer)

        # Existing commission preserves its original snapshot
        comm1.refresh_from_db()
        self.assertEqual(comm1.amount, Decimal("200.00"))
        self.assertEqual(comm1.rule_version, 1)
        self.assertEqual(comm1.earning_rule_id, rule_v1.pk)

        # Next proposal receives the new v2 rate
        proposal2 = self._create_proposal("email")
        comm2 = create_commission_for_proposal(proposal2, user=self.reviewer)
        self.assertEqual(comm2.amount, Decimal("350.00"))
        self.assertEqual(comm2.rule_version, 2)
        self.assertEqual(comm2.earning_rule_id, rule_v2.pk)

    def test_authorized_reviewer_may_review_commission_tied_to_own_collector_profile(self):
        # Give collector user review permissions and Back Office access
        self.collector_user.is_staff = True
        self.collector_user.is_superuser = True
        self.collector_user.save()

        publish_earning_rule(self.reviewer, client_id=self.client_obj.pk, category="phone", amount="150.00")
        proposal = self._create_proposal("phone", payload={"value": "09171234567"})
        commission = create_commission_for_proposal(proposal, user=self.reviewer)
        self.assertEqual(commission.collector, self.collector)
        self.assertEqual(commission.status, Commission.Status.PENDING)

        # The collector user (who is also an authorized reviewer) reviews their own commission
        reviewed = review_commission(self.collector_user, commission.pk, decision="approve")
        self.assertEqual(reviewed.status, Commission.Status.APPROVED)
        self.assertEqual(reviewed.reviewed_by, self.collector_user)

    def test_payout_includes_only_approved_and_unbatched_commissions_for_client_and_collector(self):
        from apps.accounts.models import Client
        from apps.user.models import CollectorProfile
        from django.contrib.auth import get_user_model

        User = get_user_model()
        other_client = Client.objects.create(code="FIN-OTHER", name="Other Client")
        ClientAccess.objects.create(user=self.reviewer, client=other_client, is_active=True)

        other_user = User.objects.create_user(username="other-collector")
        other_collector = CollectorProfile.objects.create(
            user=other_user,
            approval_status="approved",
            approved_by=self.reviewer,
            approved_at=timezone.now(),
        )

        rule = publish_earning_rule(self.reviewer, client_id=self.client_obj.pk, category="email", amount="100.00")
        other_rule = publish_earning_rule(self.reviewer, client_id=other_client.pk, category="email", amount="100.00")

        # Create commissions
        # 1. Approved for self.client_obj, self.collector
        p1 = self._create_proposal("email")
        c1 = create_commission_for_proposal(p1, user=self.reviewer)
        c1.status = Commission.Status.APPROVED
        c1.reviewed_by = self.reviewer
        c1.reviewed_at = timezone.now()
        c1.save()

        # 2. Approved for self.client_obj, self.collector
        p2 = self._create_proposal("email")
        c2 = create_commission_for_proposal(p2, user=self.reviewer)
        c2.status = Commission.Status.APPROVED
        c2.reviewed_by = self.reviewer
        c2.reviewed_at = timezone.now()
        c2.save()

        # 3. Pending for self.client_obj, self.collector (should NOT be included)
        p3 = self._create_proposal("email")
        c3 = create_commission_for_proposal(p3, user=self.reviewer)

        # 4. Rejected for self.client_obj, self.collector (should NOT be included)
        p4 = self._create_proposal("email")
        c4 = create_commission_for_proposal(p4, user=self.reviewer)
        c4.status = Commission.Status.REJECTED
        c4.reviewed_by = self.reviewer
        c4.reviewed_at = timezone.now()
        c4.rejection_reason = "Duplicate update"
        c4.save()

        # 5. Approved for other client (should NOT be included)
        p5 = self._create_proposal("email")
        c5 = Commission.objects.create(
            proposal=p5,
            client=other_client,
            collector=self.collector,
            earning_rule=other_rule,
            category="email",
            amount=Decimal("100.00"),
            currency="PHP",
            rule_version=1,
            status=Commission.Status.APPROVED,
            reviewed_by=self.reviewer,
            reviewed_at=timezone.now(),
        )

        # 6. Approved for other collector (should NOT be included)
        p6 = self._create_proposal("email")
        c6 = Commission.objects.create(
            proposal=p6,
            client=self.client_obj,
            collector=other_collector,
            earning_rule=rule,
            category="email",
            amount=Decimal("100.00"),
            currency="PHP",
            rule_version=1,
            status=Commission.Status.APPROVED,
            reviewed_by=self.reviewer,
            reviewed_at=timezone.now(),
        )

        payout = create_payout(self.reviewer, client_id=self.client_obj.pk, collector_id=self.collector.pk)
        self.assertEqual(payout.status, Payout.Status.DRAFT)
        self.assertEqual(payout.client, self.client_obj)
        self.assertEqual(payout.collector, self.collector)
        self.assertEqual(payout.amount, Decimal("200.00"))

        included_comm_ids = set(payout.items.values_list("commission_id", flat=True))
        self.assertEqual(included_comm_ids, {c1.pk, c2.pk})

    def test_duplicate_payout_inclusion_is_prevented(self):
        publish_earning_rule(self.reviewer, client_id=self.client_obj.pk, category="email", amount="250.00")
        proposal = self._create_proposal("email")
        comm = create_commission_for_proposal(proposal, user=self.reviewer)
        comm.status = Commission.Status.APPROVED
        comm.reviewed_by = self.reviewer
        comm.reviewed_at = timezone.now()
        comm.save()

        payout1 = create_payout(self.reviewer, client_id=self.client_obj.pk, collector_id=self.collector.pk)
        self.assertEqual(payout1.items.filter(commission=comm).count(), 1)

        # Second payout cannot include the same commission because payout_item__isnull=True filters it out
        with self.assertRaises(CollectionWorkflowError) as ctx:
            create_payout(self.reviewer, client_id=self.client_obj.pk, collector_id=self.collector.pk)
        self.assertEqual(ctx.exception.code, "no_commissions")

        # Database OneToOne constraint enforces uniqueness
        from apps.collection.models import PayoutItem
        payout2 = Payout.objects.create(
            client=self.client_obj,
            collector=self.collector,
            currency="PHP",
            amount=Decimal("250.00"),
            status=Payout.Status.DRAFT,
            created_by=self.reviewer,
        )
        with self.assertRaises(IntegrityError):
            PayoutItem.objects.create(payout=payout2, commission=comm, amount=comm.amount)

    def test_marking_payout_paid_marks_only_its_included_commissions_paid(self):
        publish_earning_rule(self.reviewer, client_id=self.client_obj.pk, category="email", amount="100.00")

        # Commissions 1 & 2 will be included in payout
        p1 = self._create_proposal("email")
        c1 = create_commission_for_proposal(p1, user=self.reviewer)
        c1.status = Commission.Status.APPROVED
        c1.reviewed_by = self.reviewer
        c1.reviewed_at = timezone.now()
        c1.save()

        p2 = self._create_proposal("email")
        c2 = create_commission_for_proposal(p2, user=self.reviewer)
        c2.status = Commission.Status.APPROVED
        c2.reviewed_by = self.reviewer
        c2.reviewed_at = timezone.now()
        c2.save()

        # Commission 3 is approved but excluded from payout
        p3 = self._create_proposal("email")
        c3 = create_commission_for_proposal(p3, user=self.reviewer)
        c3.status = Commission.Status.APPROVED
        c3.reviewed_by = self.reviewer
        c3.reviewed_at = timezone.now()
        c3.save()

        payout = create_payout(
            self.reviewer,
            client_id=self.client_obj.pk,
            collector_id=self.collector.pk,
            commission_ids=[c1.pk, c2.pk],
        )
        approve_payout(self.reviewer, payout.pk)
        mark_payout_paid(self.reviewer, payout.pk, reference="DISB-TEST-2026")

        payout.refresh_from_db()
        self.assertEqual(payout.status, Payout.Status.PAID)
        self.assertEqual(payout.reference, "DISB-TEST-2026")
        self.assertIsNotNone(payout.paid_at)
        self.assertEqual(payout.paid_by, self.reviewer)

        c1.refresh_from_db()
        c2.refresh_from_db()
        c3.refresh_from_db()

        # Only c1 and c2 are paid; c3 remains approved
        self.assertEqual(c1.status, Commission.Status.PAID)
        self.assertEqual(c2.status, Commission.Status.PAID)
        self.assertEqual(c3.status, Commission.Status.APPROVED)


class PointRuleModelTests(SimpleTestCase):
    def test_category_must_be_approved(self):
        rule = PointRule(category="invalid", version=1, points=1, is_active=True)
        with self.assertRaises(ValidationError):
            rule.clean()

    def test_version_must_be_positive_integer(self):
        rule = PointRule(category="address", version=0, points=1, is_active=True)
        with self.assertRaises(ValidationError):
            rule.clean()

    def test_points_must_be_exactly_one(self):
        # Valid when points=1
        valid_rule = PointRule(category="email", version=1, points=1, is_active=True)
        valid_rule.clean()

        # Invalid when points != 1
        for pts in [0, 2, -1, None]:
            rule = PointRule(category="email", version=1, points=pts, is_active=True)
            with self.assertRaises(ValidationError):
                rule.clean()

    def test_pointrule_model_constraint_enforces_exactly_one_point(self):
        from django.db import models
        constraint_names = {c.name: c for c in PointRule._meta.constraints}
        self.assertIn("pointrule_exactly_one_point", constraint_names)
        c = constraint_names["pointrule_exactly_one_point"]
        self.assertIsInstance(c, models.CheckConstraint)
        self.assertEqual(c.condition, models.Q(points=1))

    def test_pointrule_generated_migration_enforces_exactly_one_point(self):
        import importlib
        from django.db import models
        from django.db.migrations import AddConstraint
        m0006 = importlib.import_module("apps.collection.migrations.0006_verified_points")
        found = False
        for op in m0006.Migration.operations:
            if isinstance(op, AddConstraint) and op.model_name == "pointrule":
                if getattr(op.constraint, "name", None) == "pointrule_exactly_one_point":
                    found = True
                    condition = getattr(op.constraint, "condition", None) or getattr(op.constraint, "check", None)
                    self.assertEqual(condition, models.Q(points=1))
        self.assertTrue(found, "Migration 0006 must contain pointrule_exactly_one_point constraint")

    def test_active_rule_cannot_have_retirement_metadata(self):
        from apps.user.models import User
        now = timezone.now()
        staff = User(pk=1)
        rule = PointRule(category="phone", version=1, points=1, is_active=True, retired_at=now, retired_by=staff)
        with self.assertRaises(ValidationError):
            rule.clean()

    def test_retired_rule_must_have_retirement_metadata(self):
        rule = PointRule(category="phone", version=1, points=1, is_active=False)
        with self.assertRaises(ValidationError):
            rule.clean()


class PointTransactionModelTests(SimpleTestCase):
    def test_category_must_be_approved(self):
        txn = PointTransaction(category="invalid", points=1, rule_version=1)
        with self.assertRaises(ValidationError):
            txn.clean()

    def test_points_must_be_positive(self):
        txn = PointTransaction(category="email", points=0, rule_version=1)
        with self.assertRaises(ValidationError):
            txn.clean()

    def test_rule_version_must_be_positive(self):
        txn = PointTransaction(category="email", points=1, rule_version=0)
        with self.assertRaises(ValidationError):
            txn.clean()


class PointServicesValidationTests(SimpleTestCase):
    def test_can_manage_point_rules_helper(self):
        from unittest.mock import MagicMock
        user = MagicMock(is_authenticated=True, is_active=True, is_superuser=False)
        user.has_perm.side_effect = lambda p: p == "collection.manage_point_rules"
        self.assertTrue(can_manage_point_rules(user))

        user.has_perm.side_effect = lambda p: False
        self.assertFalse(can_manage_point_rules(user))

        user.is_superuser = True
        self.assertTrue(can_manage_point_rules(user))

    def test_publish_point_rule_requires_permission(self):
        from unittest.mock import MagicMock
        user = MagicMock(is_authenticated=True, is_active=True, is_superuser=False)
        user.has_perm.return_value = False
        with self.assertRaises(CollectionWorkflowError) as ctx:
            publish_point_rule(user, category="address")
        self.assertEqual(ctx.exception.code, "permission_required")

    def test_publish_point_rule_rejects_unapproved_category(self):
        from unittest.mock import MagicMock
        user = MagicMock(is_authenticated=True, is_active=True, is_superuser=True)
        with self.assertRaises(CollectionWorkflowError) as ctx:
            publish_point_rule(user, category="social_media")
        self.assertEqual(ctx.exception.code, "invalid_category")

    def test_publish_point_rule_rejects_non_one_point(self):
        from unittest.mock import MagicMock
        user = MagicMock(is_authenticated=True, is_active=True, is_superuser=True)
        with self.assertRaises(CollectionWorkflowError) as ctx:
            publish_point_rule(user, category="email", points=2)
        self.assertEqual(ctx.exception.code, "invalid_points")

    def test_collector_points_summary_structure(self):
        from unittest.mock import patch, MagicMock
        collector = MagicMock(pk=1)
        with patch("apps.collection.services.PointTransaction.objects.filter") as mock_filter:
            qs = mock_filter.return_value
            qs.aggregate.return_value = {"s": 3}
            qs.filter.return_value.aggregate.side_effect = [
                {"s": 2},
                {"s": 1},
                {"s": 0},
                {"s": 0},
                {"s": 0},
            ]
            summary = collector_points_summary(collector)
            self.assertEqual(summary["total"], 3)
            self.assertEqual(len(summary["breakdown"]), 5)
            self.assertEqual(summary["breakdown"]["address"]["points"], 2)
            self.assertEqual(summary["breakdown"]["email"]["points"], 1)
            self.assertEqual(summary["breakdown"]["phone"]["points"], 0)
            self.assertEqual(summary["breakdown"]["contact_person"]["points"], 0)
            self.assertEqual(summary["breakdown"]["payment_arrangement"]["points"], 0)

    def test_award_points_rejects_pending_proposal(self):
        from unittest.mock import MagicMock
        from apps.accounts.information.models import AccountInformationProposal
        proposal = MagicMock(status=AccountInformationProposal.Status.PENDING, category="email")
        self.assertIsNone(award_points_for_proposal(proposal))

    def test_award_points_rejects_rejected_proposal(self):
        from unittest.mock import MagicMock
        from apps.accounts.information.models import AccountInformationProposal
        proposal = MagicMock(status=AccountInformationProposal.Status.REJECTED, category="email")
        self.assertIsNone(award_points_for_proposal(proposal))

    def test_award_points_approved_proposal_earns_one_point(self):
        from unittest.mock import MagicMock, patch
        from apps.accounts.information.models import AccountInformationProposal
        from apps.assignments.models import AccountAssignment
        from apps.accounts.models import Account
        from apps.user.models import CollectorProfile

        account = Account(pk=10)
        collector = CollectorProfile(pk=5)
        assignment = AccountAssignment(pk=20, account=account, collector=collector)
        proposal = AccountInformationProposal(
            pk=42,
            status=AccountInformationProposal.Status.APPROVED,
            category="email",
            account=account,
            assignment=assignment,
        )
        active_rule = PointRule(pk=1, category="email", version=1, points=1, is_active=True)
        with patch("apps.collection.services.PointRule.objects.filter") as mock_filter_rule, \
             patch("apps.collection.services.PointTransaction.objects.filter") as mock_filter_txn, \
             patch("apps.collection.services.PointTransaction.save"), \
             patch("apps.collection.services.PointTransaction.full_clean"), \
             patch("apps.collection.services.append_audit_event") as mock_audit:
            mock_filter_rule.return_value.first.return_value = active_rule
            mock_filter_txn.return_value.exists.return_value = False

            txn = award_points_for_proposal(proposal)
            self.assertIsNotNone(txn)
            self.assertEqual(txn.points, 1)
            self.assertEqual(txn.rule_version, 1)
            self.assertEqual(mock_audit.call_count, 1)
            self.assertEqual(mock_audit.call_args.kwargs["action"], "point_awarded")



class PointPersistenceTests(TestCase):
    def setUp(self):
        from apps.accounts.models import Client, Customer, CustomerAddress, Account
        from apps.assignments.models import AccountAssignment
        from apps.user.models import CollectorProfile, ClientAccess
        from django.contrib.auth import get_user_model
        from django.contrib.gis.geos import Point
        from datetime import timedelta

        User = get_user_model()
        self.client_obj = Client.objects.create(code="PT-CLIENT", name="Point Test Client")
        self.staff_user = User.objects.create_superuser(username="pt-staff", password="test-only")
        self.collector_user = User.objects.create_user(username="pt-collector")
        self.collector = CollectorProfile.objects.create(
            user=self.collector_user,
            approval_status="approved",
            approved_by=self.staff_user,
            approved_at=timezone.now(),
        )
        ClientAccess.objects.create(user=self.staff_user, client=self.client_obj, is_active=True)
        ClientAccess.objects.create(user=self.collector_user, client=self.client_obj, is_active=True)

        self.customer = Customer.objects.create(client=self.client_obj, source_system="TEST", display_name="Point Customer")
        self.address = CustomerAddress.objects.create(
            customer=self.customer,
            kind="residential",
            address_line="100 Point St",
            position=Point(120.9842, 14.5995, srid=4326),
            verification_status="verified",
            verified_by=self.staff_user,
            verified_at=timezone.now(),
        )
        self.account = Account.objects.create(
            client=self.client_obj,
            customer=self.customer,
            source_system="TEST",
            external_id="PT-ACC-1",
            reference="PT-ACC-1",
            balance_snapshot=Decimal("5000.00"),
            balance_as_of=timezone.now(),
            collection_address=self.address,
        )
        self.assignment = AccountAssignment.objects.create(
            account=self.account,
            collector=self.collector,
            target_address=self.address,
            target_position=self.address.position,
            policy_snapshot={"test": True},
            first_action_due_at=timezone.now() + timedelta(days=1),
        )

    def _create_proposal(self, category="email", payload=None):
        import uuid
        from apps.accounts.information.models import AccountInformationProposal
        if payload is None:
            payload = {"value": f"user-{uuid.uuid4().hex[:6]}@example.com"}
        return AccountInformationProposal.objects.create(
            account=self.account,
            customer=self.customer,
            assignment=self.assignment,
            submitted_by=self.collector_user,
            category=category,
            current_snapshot={},
            proposed_payload=payload,
            explanation="Customer confirmed value",
            event_id=uuid.uuid4(),
            payload_fingerprint=uuid.uuid4().hex,
            status=AccountInformationProposal.Status.APPROVED,
            reviewed_by=self.staff_user,
            reviewed_at=timezone.now(),
        )

    def test_publish_point_rule_versioning_and_retires_previous_version(self):
        rule_v1 = publish_point_rule(self.staff_user, category="email")
        self.assertEqual(rule_v1.version, 1)
        self.assertEqual(rule_v1.points, 1)
        self.assertTrue(rule_v1.is_active)

        rule_v2 = publish_point_rule(self.staff_user, category="email")
        self.assertEqual(rule_v2.version, 2)
        self.assertTrue(rule_v2.is_active)

        rule_v1.refresh_from_db()
        self.assertFalse(rule_v1.is_active)
        self.assertIsNotNone(rule_v1.retired_at)
        self.assertEqual(rule_v1.retired_by, self.staff_user)

    def test_award_points_creates_one_transaction_and_is_idempotent(self):
        rule = publish_point_rule(self.staff_user, category="email")
        proposal = self._create_proposal("email")

        initial_balance = self.account.balance_snapshot
        txn1 = award_points_for_proposal(proposal, user=self.staff_user)
        self.assertIsNotNone(txn1)
        self.assertEqual(txn1.points, 1)
        self.assertEqual(txn1.rule_version, 1)
        self.assertEqual(txn1.collector, self.collector)
        self.assertEqual(txn1.category, "email")
        self.assertEqual(txn1.point_rule, rule)

        # Repeating award does not duplicate transaction (idempotency)
        txn2 = award_points_for_proposal(proposal, user=self.staff_user)
        self.assertEqual(txn1.pk, txn2.pk)
        self.assertEqual(PointTransaction.objects.filter(proposal=proposal).count(), 1)

        # Points never affect balance or commissions
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance_snapshot, initial_balance)
        self.assertFalse(Commission.objects.filter(proposal=proposal).exists())

    def test_missing_rule_awards_no_points(self):
        proposal = self._create_proposal("phone")
        txn = award_points_for_proposal(proposal, user=self.staff_user)
        self.assertIsNone(txn)
        self.assertEqual(PointTransaction.objects.filter(proposal=proposal).count(), 0)

    def test_collector_points_summary_aggregates(self):
        publish_point_rule(self.staff_user, category="email")
        publish_point_rule(self.staff_user, category="address")

        p_email = self._create_proposal("email")
        p_addr = self._create_proposal("address", payload={"address_line": "New Street"})

        award_points_for_proposal(p_email, user=self.staff_user)
        award_points_for_proposal(p_addr, user=self.staff_user)

        summary = collector_points_summary(self.collector)
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["breakdown"]["email"]["points"], 1)
        self.assertEqual(summary["breakdown"]["address"]["points"], 1)
        self.assertEqual(summary["breakdown"]["phone"]["points"], 0)
        self.assertEqual(summary["breakdown"]["contact_person"]["points"], 0)
        self.assertEqual(summary["breakdown"]["payment_arrangement"]["points"], 0)

    def test_award_points_rejects_pending_and_rejected_proposals(self):
        from apps.accounts.information.models import AccountInformationProposal
        publish_point_rule(self.staff_user, category="email")
        proposal = self._create_proposal("email")

        # Pending proposal cannot receive points
        proposal.status = AccountInformationProposal.Status.PENDING
        self.assertIsNone(award_points_for_proposal(proposal, user=self.staff_user))
        self.assertEqual(PointTransaction.objects.filter(proposal=proposal).count(), 0)

        # Rejected proposal cannot receive points
        proposal.status = AccountInformationProposal.Status.REJECTED
        self.assertIsNone(award_points_for_proposal(proposal, user=self.staff_user))
        self.assertEqual(PointTransaction.objects.filter(proposal=proposal).count(), 0)

        # Approved proposal earns 1 point
        proposal.status = AccountInformationProposal.Status.APPROVED
        txn = award_points_for_proposal(proposal, user=self.staff_user)
        self.assertIsNotNone(txn)
        self.assertEqual(txn.points, 1)
        self.assertEqual(PointTransaction.objects.filter(proposal=proposal).count(), 1)

    def test_point_rule_validation_rejects_non_one_point(self):
        rule = PointRule(category="email", version=99, points=2, is_active=True, created_by=self.staff_user)
        with self.assertRaises(ValidationError):
            rule.full_clean()


