from datetime import datetime, timezone as datetime_timezone
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.gis.geos import Point
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from apps.collection.models import CommissionRule

from .models import Account, Client, Customer, CustomerAddress
from .services import validate_account_for_offers


class AccountFoundationValidationTests(SimpleTestCase):
    def setUp(self):
        self.customer = Customer(pk=1, client_id=1, display_name="Fictional Customer")
        self.address = CustomerAddress(pk=1, customer=self.customer, kind="residential", address_line="Fictional address")
        self.rule = CommissionRule(pk=1, client_id=1, kind="percentage", percentage=Decimal("10"), currency="PHP")
        self.account = Account(client_id=1, customer=self.customer, collection_address=self.address, commission_rule=self.rule, currency="PHP")

    def test_consistent_relationships_pass(self):
        self.account.clean()

    def test_cross_client_customer_is_rejected(self):
        self.customer.client_id = 2
        with self.assertRaises(ValidationError):
            self.account.clean()

    def test_another_customers_address_is_rejected(self):
        self.address.customer_id = 2
        with self.assertRaises(ValidationError):
            self.account.clean()

    def test_cross_client_or_currency_rule_is_rejected(self):
        self.rule.client_id = 2
        with self.assertRaises(ValidationError):
            self.account.clean()
        self.rule.client_id = 1
        self.rule.currency = "USD"
        with self.assertRaises(ValidationError):
            self.account.clean()

    def test_unknown_position_stays_empty(self):
        self.address.clean()
        self.assertIsNone(self.address.position)

    def test_position_requires_valid_two_dimensional_wgs84(self):
        for point in [Point(181, 14, srid=4326), Point(120, -91, srid=4326), Point(120, 14, srid=3857), Point(120, 14, 1, srid=4326)]:
            with self.subTest(point=point):
                self.address.position = point
                with self.assertRaises(ValidationError):
                    self.address.clean()
        self.address.position = Point(120.9842, 14.5995, srid=4326)
        self.address.clean()

    def test_verified_address_requires_review_evidence(self):
        self.address.verification_status = "verified"
        with self.assertRaises(ValidationError):
            self.address.clean()
        self.address.verified_by_id = 1
        self.address.verified_at = datetime.now(datetime_timezone.utc)
        self.address.clean()

    def test_priority_and_balance_field_validation(self):
        for field, value in [("priority", 0), ("priority", 6), ("balance_snapshot", Decimal("-1")), ("balance_snapshot", Decimal("NaN"))]:
            with self.subTest(field=field, value=value), self.assertRaises(ValidationError):
                Account._meta.get_field(field).clean(value, self.account)


class AccountFoundationDatabaseTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="foundation-reviewer")
        self.client_record = Client.objects.create(code="TEST-CLIENT", name="Fictional Client")
        self.customer = Customer.objects.create(client=self.client_record, source_system="TEST", display_name="Fictional Customer")
        self.rule = CommissionRule.objects.create(client=self.client_record, code="TEST", version=1, name="Provisional test terms", kind="percentage", percentage=Decimal("10"), earning_basis="verified_payment")
        self.address = CustomerAddress.objects.create(customer=self.customer, kind="residential", address_line="Fictional address", position=Point(120.9842, 14.5995, srid=4326), verification_status="verified", verified_by=self.user, verified_at=timezone.now())
        self.values = dict(client=self.client_record, customer=self.customer, source_system="TEST", external_id="TEST-001", reference="TEST-001", balance_snapshot=Decimal("1000"), balance_as_of=timezone.now(), collection_address=self.address, commission_rule=self.rule)

    def test_verified_account_is_ready_for_offer_validation(self):
        account = Account.objects.create(**self.values)
        self.assertEqual(validate_account_for_offers(account), account)
        account.refresh_from_db()
        self.assertAlmostEqual(account.collection_address.position.x, 120.9842)

    def test_unverified_or_missing_location_is_not_offer_ready(self):
        account = Account.objects.create(**self.values)
        self.address.verification_status = "unverified"
        self.address.save(update_fields=["verification_status"])
        with self.assertRaises(ValidationError):
            validate_account_for_offers(account)
        self.address.verification_status = "verified"
        self.address.position = None
        self.address.save(update_fields=["verification_status", "position"])
        with self.assertRaises(ValidationError):
            validate_account_for_offers(account)

    def test_duplicate_source_reference_is_blocked_by_database(self):
        Account.objects.create(**self.values)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Account.objects.create(**self.values)

    def test_closed_case_is_not_offer_ready(self):
        account = Account.objects.create(**self.values, status="closed")
        with self.assertRaises(ValidationError):
            validate_account_for_offers(account)
