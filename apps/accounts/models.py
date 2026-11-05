import math
import uuid
from decimal import Decimal

from django.conf import settings
from django.contrib.gis.db import models
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator


class Client(models.Model):
    id = models.BigAutoField(primary_key=True)
    code = models.CharField(max_length=40, unique=True)
    name = models.CharField(max_length=200)
    is_active = models.BooleanField(default=True)
    # Provisional defaults from the architecture proposal.
    offer_timeout_minutes = models.PositiveSmallIntegerField(
        default=10, validators=[MinValueValidator(1)]
    )
    first_action_hours = models.PositiveSmallIntegerField(
        default=24, validators=[MinValueValidator(1)]
    )
    policy_version = models.PositiveIntegerField(default=1, validators=[MinValueValidator(1)])
    gps_max_age_seconds = models.PositiveIntegerField(default=300, validators=[MinValueValidator(1)])
    gps_max_accuracy_m = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("50.00"), validators=[MinValueValidator(Decimal("0.01"))])
    gps_max_clock_skew_seconds = models.PositiveIntegerField(default=120, validators=[MinValueValidator(1)])
    matching_strategy = models.CharField(max_length=20, choices=[("nearest", "Nearest"), ("fan_out", "Fan out"), ("manual", "Manual")], default="nearest")
    max_offer_recipients = models.PositiveSmallIntegerField(default=1, validators=[MinValueValidator(1), MaxValueValidator(20)])
    require_service_area_membership = models.BooleanField(default=False)
    allow_collector_release = models.BooleanField(default=False)
    auto_reassignment_enabled = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(policy_version__gt=0, gps_max_age_seconds__gt=0, gps_max_accuracy_m__gt=0, gps_max_clock_skew_seconds__gt=0), name="client_valid_gps_policy"),
            models.CheckConstraint(condition=models.Q(matching_strategy__in=["nearest", "fan_out", "manual"]), name="client_valid_matching_strategy"),
            models.CheckConstraint(condition=models.Q(max_offer_recipients__gte=1, max_offer_recipients__lte=20), name="client_valid_offer_recipient_count"),
            models.CheckConstraint(
                condition=models.Q(offer_timeout_minutes__gt=0),
                name="client_positive_offer_timeout",
            ),
            models.CheckConstraint(
                condition=models.Q(first_action_hours__gt=0),
                name="client_positive_first_action",
            ),
        ]

    def __str__(self):
        return self.name


class Customer(models.Model):
    class CustomerType(models.TextChoices):
        PERSON = "person", "Person"
        BUSINESS = "business", "Business"

    id = models.BigAutoField(primary_key=True)
    client = models.ForeignKey(
        Client,
        on_delete=models.PROTECT,
        related_name="customers",
        help_text="Client that owns this customer's data.",
    )
    source_system = models.CharField(
        max_length=40,
        default="WIRED",
        help_text="Source namespace; use TEST for fictional development records.",
    )
    external_id = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        help_text="Stable customer identifier from the source; use NULL if unavailable.",
    )
    display_name = models.CharField(max_length=200)
    customer_type = models.CharField(
        max_length=20,
        choices=CustomerType.choices,
        default=CustomerType.PERSON,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["client", "source_system", "external_id"],
                condition=models.Q(external_id__isnull=False),
                name="customer_unique_source_identity",
            ),
            models.CheckConstraint(
                condition=models.Q(customer_type__in=["person", "business"]),
                name="customer_valid_type",
            ),
            models.CheckConstraint(
                condition=~models.Q(source_system=""),
                name="customer_source_not_empty",
            ),
            models.CheckConstraint(
                condition=~models.Q(display_name=""),
                name="customer_name_not_empty",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(external_id__isnull=True) | ~models.Q(external_id="")
                ),
                name="customer_external_id_null_or_value",
            ),
        ]

    def __str__(self):
        return self.display_name


class CustomerAddress(models.Model):
    class Kind(models.TextChoices):
        RESIDENTIAL = "residential", "Residential"
        BUSINESS = "business", "Business"
        OTHER = "other", "Other"

    class VerificationStatus(models.TextChoices):
        UNVERIFIED = "unverified", "Unverified"
        VERIFIED = "verified", "Verified"
        REJECTED = "rejected", "Rejected"

    id = models.BigAutoField(primary_key=True)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="addresses")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    address_line = models.CharField(max_length=300)
    territory = models.ForeignKey("assignments.Territory", on_delete=models.PROTECT, null=True, blank=True, related_name="customer_addresses")
    position = models.PointField(geography=True, srid=4326, null=True, blank=True, spatial_index=True, help_text="Verified collection coordinates, longitude first; leave unknown locations empty.")
    verification_status = models.CharField(max_length=20, choices=VerificationStatus.choices, default=VerificationStatus.UNVERIFIED)
    added_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="added_customer_addresses")
    verified_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="verified_customer_addresses")
    verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(kind__in=["residential", "business", "other"]), name="address_valid_kind"),
            models.CheckConstraint(condition=models.Q(verification_status__in=["unverified", "verified", "rejected"]), name="address_valid_verification_status"),
            models.CheckConstraint(condition=(~models.Q(verification_status="verified") | models.Q(verified_by__isnull=False, verified_at__isnull=False)), name="address_verified_has_reviewer"),
            models.CheckConstraint(condition=(models.Q(verified_by__isnull=True, verified_at__isnull=True) | models.Q(verified_by__isnull=False, verified_at__isnull=False)), name="address_review_evidence_pair"),
        ]

    def clean(self):
        super().clean()
        if self.position is not None:
            if self.position.srid != 4326 or self.position.empty or self.position.hasz:
                raise ValidationError({"position": "Use a two-dimensional WGS84 point."})
            if not (math.isfinite(self.position.x) and math.isfinite(self.position.y)
                    and -180 <= self.position.x <= 180 and -90 <= self.position.y <= 90):
                raise ValidationError({"position": "Coordinates must be finite and within valid bounds."})
        if self.verification_status == self.VerificationStatus.VERIFIED and (not self.verified_by_id or self.verified_at is None):
            raise ValidationError("Verified addresses require a reviewer and verification time.")


class Account(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "Open"
        ON_HOLD = "on_hold", "On hold"
        DISPUTED = "disputed", "Disputed"
        LEGAL = "legal", "Legal"
        CLOSED = "closed", "Closed"
        INVALID = "invalid", "Invalid"
        RESOLVED = "resolved", "Resolved"

    id = models.BigAutoField(primary_key=True)
    client = models.ForeignKey(Client, on_delete=models.PROTECT, related_name="accounts")
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="accounts")
    source_system = models.CharField(max_length=40, default="WIRED")
    external_id = models.CharField(max_length=100)
    reference = models.CharField(max_length=100)
    balance_snapshot = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(0)])
    balance_as_of = models.DateTimeField()
    currency = models.CharField(max_length=3, default="PHP", validators=[RegexValidator(r"^[A-Z]{3}$", "Use a three-letter uppercase currency code.")])
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    priority = models.PositiveSmallIntegerField(default=3, validators=[MinValueValidator(1), MaxValueValidator(5)])
    collection_address = models.ForeignKey(CustomerAddress, on_delete=models.PROTECT, null=True, blank=True, related_name="collection_accounts")
    commission_rule = models.ForeignKey("collection.CommissionRule", on_delete=models.PROTECT, null=True, blank=True, related_name="accounts")
    source_version = models.CharField(max_length=100, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        permissions = [
            ("change_account_status", "Can change account status"),
        ]
        constraints = [
            models.UniqueConstraint(fields=["client", "source_system", "external_id"], name="account_unique_source_identity"),
            models.UniqueConstraint(fields=["client", "reference"], name="account_unique_client_reference"),
            models.CheckConstraint(condition=models.Q(balance_snapshot__gte=0), name="account_nonnegative_balance"),
            models.CheckConstraint(condition=models.Q(priority__gte=1, priority__lte=5), name="account_valid_priority"),
            models.CheckConstraint(condition=models.Q(status__in=["open", "on_hold", "disputed", "legal", "closed", "invalid", "resolved"]), name="account_valid_status"),
        ]
        indexes = [models.Index(fields=["client", "status", "priority"], name="account_client_status_priority")]

    def clean(self):
        super().clean()
        errors = {}
        if self.customer_id and self.client_id:
            try:
                if self.customer.client_id != self.client_id:
                    errors["customer"] = "Customer must belong to the account's client."
            except ObjectDoesNotExist:
                errors["customer"] = "Choose an existing customer."
        if self.collection_address_id:
            try:
                if self.collection_address.customer_id != self.customer_id:
                    errors["collection_address"] = "Collection address must belong to the account's customer."
            except ObjectDoesNotExist:
                errors["collection_address"] = "Choose an existing collection address."
        if self.commission_rule_id:
            try:
                rule = self.commission_rule
                if rule.client_id != self.client_id:
                    errors["commission_rule"] = "Commission rule must belong to the account's client."
                elif rule.currency != self.currency:
                    errors["currency"] = "Account and earning terms must use the same currency."
            except ObjectDoesNotExist:
                errors["commission_rule"] = "Choose existing earning terms."
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return self.reference


class AccountStatusChange(models.Model):
    id = models.BigAutoField(primary_key=True)
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="status_changes")
    from_status = models.CharField(max_length=20)
    to_status = models.CharField(max_length=20)
    reason = models.TextField(max_length=5000)
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="account_status_changes")
    event_id = models.UUIDField(unique=True, editable=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        indexes = [
            models.Index(fields=["account", "created_at"], name="acc_status_change_idx"),
        ]

    def __str__(self):
        return f"{self.account.reference}: {self.from_status} -> {self.to_status}"


from .information.models import AccountInformationProposal, CustomerContact
