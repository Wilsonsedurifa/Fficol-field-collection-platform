import uuid
from decimal import Decimal

from django.conf import settings
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DecimalRangeField, RangeOperators
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models


class CommissionRule(models.Model):
    class Kind(models.TextChoices):
        PERCENTAGE = "percentage", "Percentage"
        FIXED = "fixed", "Fixed"
        TIERED = "tiered", "Tiered"

    class EarningBasis(models.TextChoices):
        VERIFIED_PAYMENT = "verified_payment", "Verified payment"
        ACCOUNT_RESOLUTION = "account_resolution", "Account resolution"

    class FixedFrequency(models.TextChoices):
        ONCE_PER_ACCOUNT = "once_per_account", "Once per account"
        PER_PAYMENT = "per_payment", "Per payment"

    class TierMode(models.TextChoices):
        WHOLE_AMOUNT = "whole_amount", "Whole amount"
        MARGINAL = "marginal", "Marginal"

    id = models.BigAutoField(primary_key=True)
    client = models.ForeignKey("accounts.Client", on_delete=models.PROTECT, related_name="commission_rules")
    code = models.CharField(max_length=60)
    version = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    name = models.CharField(max_length=150)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    percentage = models.DecimalField(max_digits=7, decimal_places=4, null=True, blank=True, validators=[MinValueValidator(0), MaxValueValidator(100)])
    fixed_amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(0)])
    currency = models.CharField(max_length=3, default="PHP", validators=[RegexValidator(r"^[A-Z]{3}$", "Use a three-letter uppercase currency code.")])
    earning_basis = models.CharField(max_length=30, choices=EarningBasis.choices)
    fixed_frequency = models.CharField(max_length=20, choices=FixedFrequency.choices, blank=True, default="")
    tier_mode = models.CharField(max_length=20, choices=TierMode.choices, blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["client", "code", "version"], name="commission_rule_unique_version"),
            models.CheckConstraint(condition=models.Q(version__gt=0), name="commission_rule_positive_version"),
            models.CheckConstraint(condition=models.Q(percentage__isnull=True) | models.Q(percentage__gte=0, percentage__lte=100), name="commission_rule_valid_percentage"),
            models.CheckConstraint(condition=models.Q(fixed_amount__isnull=True) | models.Q(fixed_amount__gte=0), name="commission_rule_nonnegative_fixed"),
            models.CheckConstraint(condition=models.Q(earning_basis__in=["verified_payment", "account_resolution"]), name="commission_rule_valid_basis"),
            models.CheckConstraint(condition=(
                models.Q(kind="percentage", percentage__isnull=False, fixed_amount__isnull=True, fixed_frequency="", tier_mode="")
                | models.Q(kind="fixed", percentage__isnull=True, fixed_amount__isnull=False, fixed_frequency__in=["once_per_account", "per_payment"], tier_mode="")
                | models.Q(kind="tiered", percentage__isnull=True, fixed_amount__isnull=True, fixed_frequency="", tier_mode__in=["whole_amount", "marginal"])
            ), name="commission_rule_valid_configuration"),
        ]

    def clean(self):
        super().clean()
        valid = (
            self.kind == self.Kind.PERCENTAGE and self.percentage is not None
            and self.fixed_amount is None and not self.fixed_frequency and not self.tier_mode
        ) or (
            self.kind == self.Kind.FIXED and self.fixed_amount is not None
            and self.percentage is None and self.fixed_frequency in self.FixedFrequency.values
            and not self.tier_mode
        ) or (
            self.kind == self.Kind.TIERED and self.percentage is None and self.fixed_amount is None
            and not self.fixed_frequency and self.tier_mode in self.TierMode.values
        )
        if not valid:
            raise ValidationError("Supply only the fields required by the selected commission kind.")

    def __str__(self):
        return f"{self.name} (v{self.version})"


class CommissionTier(models.Model):
    id = models.BigAutoField(primary_key=True)
    rule = models.ForeignKey(CommissionRule, on_delete=models.PROTECT, related_name="tiers")
    lower_amount = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(0)])
    upper_amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    percentage = models.DecimalField(max_digits=7, decimal_places=4, validators=[MinValueValidator(0), MaxValueValidator(100)])
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["rule", "lower_amount"], name="commission_tier_unique_lower"),
            models.CheckConstraint(condition=models.Q(lower_amount__gte=0), name="commission_tier_nonnegative_lower"),
            models.CheckConstraint(condition=models.Q(upper_amount__isnull=True) | models.Q(upper_amount__gt=models.F("lower_amount")), name="commission_tier_valid_bounds"),
            models.CheckConstraint(condition=models.Q(percentage__gte=0, percentage__lte=100), name="commission_tier_valid_percentage"),
            ExclusionConstraint(name="commission_tier_no_overlap", expressions=[
                ("rule", RangeOperators.EQUAL),
                (models.Func("lower_amount", "upper_amount", models.Value("[)"), function="NUMRANGE", output_field=DecimalRangeField()), RangeOperators.OVERLAPS),
            ]),
        ]

    def clean(self):
        super().clean()
        if self.rule_id and self.rule.kind != CommissionRule.Kind.TIERED:
            raise ValidationError({"rule": "Bands require a tiered commission rule."})
        if self.lower_amount is not None and self.upper_amount is not None and self.upper_amount <= self.lower_amount:
            raise ValidationError({"upper_amount": "Upper amount must exceed the lower amount."})


class PromiseToPay(models.Model):
    id = models.BigAutoField(primary_key=True)
    activity = models.ForeignKey("activities.FieldActivity", on_delete=models.PROTECT, related_name="promises")
    amount = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    due_date = models.DateField()
    status = models.CharField(max_length=20, default="open", choices=[("open", "Open"), ("fulfilled", "Fulfilled"), ("broken", "Broken"), ("cancelled", "Cancelled")])
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="promise_positive_amount"),
            models.CheckConstraint(condition=models.Q(status="open", resolved_at__isnull=True) | models.Q(status__in=["fulfilled", "broken", "cancelled"], resolved_at__isnull=False), name="promise_resolution_evidence"),
        ]

    def clean(self):
        super().clean()
        if self.activity_id and (self.activity.status != "submitted" or self.activity.outcome != "promise_to_pay"):
            raise ValidationError({"activity": "A promise must belong to a submitted promise-to-pay activity."})


class Payment(models.Model):
    class Status(models.TextChoices):
        REPORTED = "reported", "Reported"
        VERIFIED = "verified", "Verified"
        REJECTED = "rejected", "Rejected"
        REVERSED = "reversed", "Reversed"

    class Method(models.TextChoices):
        BANK_TRANSFER = "bank_transfer", "Bank transfer"
        E_WALLET = "e_wallet", "E-wallet (GCash / Maya)"
        OVER_THE_COUNTER = "over_the_counter", "Over the counter"
        REMITTANCE = "remittance", "Remittance center"
        OTHER = "other", "Other authorized channel"

    id = models.BigAutoField(primary_key=True)
    account = models.ForeignKey("accounts.Account", on_delete=models.PROTECT, related_name="payments")
    assignment = models.ForeignKey("assignments.AccountAssignment", on_delete=models.PROTECT, null=True, blank=True, related_name="payments")
    activity = models.ForeignKey("activities.FieldActivity", on_delete=models.PROTECT, null=True, blank=True, related_name="payments")
    promise = models.ForeignKey(PromiseToPay, on_delete=models.PROTECT, null=True, blank=True, related_name="payments")
    reported_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="reported_payments")
    event_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    source_system = models.CharField(max_length=40, default="FICOL")
    external_id = models.CharField(max_length=100, null=True, blank=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    paid_at = models.DateTimeField(help_text="Customer reported payment timestamp.")
    method = models.CharField(max_length=30, choices=Method.choices)
    reference = models.CharField(max_length=100, blank=True, default="", help_text="Transaction reference / deposit number.")
    notes = models.TextField(max_length=5000, blank=True, default="")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.REPORTED)
    submission_fingerprint = models.CharField(max_length=64, blank=True, default="", editable=False)
    verified_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="verified_payments")
    verified_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.TextField(max_length=5000, blank=True, default="")
    reversed_at = models.DateTimeField(null=True, blank=True)
    reversal_reason = models.TextField(max_length=5000, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="payment_positive_amount"),
            models.CheckConstraint(condition=~models.Q(source_system=""), name="payment_source_system_not_empty"),
            models.CheckConstraint(condition=models.Q(status__in=["reported", "verified", "rejected", "reversed"]), name="payment_valid_status"),
            models.CheckConstraint(condition=(
                models.Q(status="reported", verified_by__isnull=True, verified_at__isnull=True, rejection_reason="")
                | models.Q(status__in=["verified", "reversed"], verified_by__isnull=False, verified_at__isnull=False, rejection_reason="")
                | models.Q(status="rejected", verified_by__isnull=False, verified_at__isnull=False, rejection_reason__gt="")
            ), name="payment_verification_evidence"),
            models.CheckConstraint(condition=(
                models.Q(status__in=["reported", "verified", "rejected"], reversed_at__isnull=True, reversal_reason="")
                | models.Q(status="reversed", reversed_at__isnull=False, reversal_reason__gt="")
            ), name="payment_reversal_evidence"),
            models.UniqueConstraint(fields=["account", "source_system", "external_id"], condition=models.Q(external_id__isnull=False), name="payment_unique_external_source"),
        ]
        indexes = [
            models.Index(fields=["account", "status", "paid_at"], name="payment_account_status_paid"),
            models.Index(fields=["reported_by", "status", "paid_at"], name="payment_reporter_status_paid"),
        ]

    def clean(self):
        super().clean()
        if self.assignment_id and self.account_id != self.assignment.account_id:
            raise ValidationError({"assignment": "Payment and assignment must belong to the same account."})
        if self.activity_id and self.account_id != self.activity.account_id:
            raise ValidationError({"activity": "Payment and activity must belong to the same account."})
        if self.promise_id and self.account_id != self.promise.activity.account_id:
            raise ValidationError({"promise": "Payment and promise must belong to the same account."})
        if self.status == self.Status.REJECTED and not self.rejection_reason:
            raise ValidationError({"rejection_reason": "Provide a reason when rejecting a payment."})
        if self.status == self.Status.REVERSED and not self.reversal_reason:
            raise ValidationError({"reversal_reason": "Provide a reason when reversing a payment."})

    def __str__(self):
        return f"{self.account.reference}: {self.amount} ({self.get_status_display()})"


class InformationCategory(models.TextChoices):
    ADDRESS = "address", "Address"
    EMAIL = "email", "Email"
    PHONE = "phone", "Phone number"
    CONTACT_PERSON = "contact_person", "Contact person"
    PAYMENT_ARRANGEMENT = "payment_arrangement", "Payment arrangement"


class InformationEarningRule(models.Model):
    id = models.BigAutoField(primary_key=True)
    client = models.ForeignKey(
        "accounts.Client",
        on_delete=models.PROTECT,
        related_name="information_earning_rules",
        help_text="Client to which this information earning rate applies.",
    )
    category = models.CharField(
        max_length=30,
        choices=InformationCategory.choices,
        help_text="Approved information category eligible for remuneration.",
    )
    amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Fixed positive remuneration amount in PHP.",
    )
    currency = models.CharField(
        max_length=3,
        default="PHP",
        validators=[RegexValidator(r"^[A-Z]{3}$", "Use a three-letter uppercase currency code.")],
        help_text="Currency code (PHP).",
    )
    version = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
        help_text="Version number of this earning rate for this client and category.",
    )
    is_active = models.BooleanField(
        default=True,
        help_text="Designates whether this rate is currently active.",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="created_earning_rules",
        help_text="Back Office user who published this earning rate.",
    )
    retired_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp when this rate version was superseded.",
    )
    retired_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="retired_earning_rules",
        help_text="Back Office user who retired this rate version.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        permissions = [("manage_earning_rules", "Can manage client information earning rules")]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "category"],
                condition=models.Q(is_active=True),
                name="unique_active_client_category_earning_rule",
            ),
            models.UniqueConstraint(
                fields=["client", "category", "version"],
                name="unique_client_category_version_earning_rule",
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gt=0),
                name="earning_rule_positive_amount",
            ),
            models.CheckConstraint(
                condition=models.Q(version__gt=0),
                name="earning_rule_positive_version",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    category__in=[
                        "address",
                        "email",
                        "phone",
                        "contact_person",
                        "payment_arrangement",
                    ]
                ),
                name="earning_rule_valid_category",
            ),
            models.CheckConstraint(
                condition=models.Q(currency="PHP"),
                name="earning_rule_php_currency",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(is_active=True, retired_at__isnull=True, retired_by__isnull=True)
                    | models.Q(is_active=False, retired_at__isnull=False)
                ),
                name="earning_rule_active_retired_consistency",
            ),
        ]
        indexes = [
            models.Index(fields=["client", "category", "is_active"], name="earning_rule_client_cat_active"),
        ]

    def clean(self):
        super().clean()
        if self.amount is not None and self.amount <= Decimal("0.00"):
            raise ValidationError({"amount": "Remuneration amount must be positive."})
        if self.currency != "PHP":
            raise ValidationError({"currency": "Information earning rules must use PHP currency."})
        if self.version is not None and self.version < 1:
            raise ValidationError({"version": "Version must be at least 1."})
        if self.is_active and (self.retired_at is not None or self.retired_by is not None):
            raise ValidationError("An active earning rule cannot have retirement metadata.")
        if not self.is_active and self.retired_at is None:
            raise ValidationError({"retired_at": "Retired earning rules must record retirement time."})

    def __str__(self):
        status = "Active" if self.is_active else f"Retired (v{self.version})"
        return f"{self.client.code} - {self.get_category_display()}: {self.currency} {self.amount} ({status})"


class Commission(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        PAID = "paid", "Paid"

    id = models.BigAutoField(primary_key=True)
    proposal = models.OneToOneField(
        "accounts.AccountInformationProposal",
        on_delete=models.PROTECT,
        related_name="commission",
        help_text="Source validated information proposal.",
    )
    client = models.ForeignKey(
        "accounts.Client",
        on_delete=models.PROTECT,
        related_name="commissions",
        help_text="Creditor client responsible for remuneration terms.",
    )
    collector = models.ForeignKey(
        "user.CollectorProfile",
        on_delete=models.PROTECT,
        related_name="commissions",
        help_text="Field collector earning the commission.",
    )
    earning_rule = models.ForeignKey(
        InformationEarningRule,
        on_delete=models.PROTECT,
        related_name="commissions",
        help_text="Information earning rule applied at creation.",
    )
    category = models.CharField(
        max_length=30,
        choices=InformationCategory.choices,
        help_text="Snapshot of the information category.",
    )
    amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Snapshot of the earning amount in PHP.",
    )
    currency = models.CharField(
        max_length=3,
        default="PHP",
        validators=[RegexValidator(r"^[A-Z]{3}$", "Use a three-letter uppercase currency code.")],
        help_text="Snapshot of the currency code.",
    )
    rule_version = models.PositiveIntegerField(
        validators=[MinValueValidator(1)],
        help_text="Snapshot of the applied earning rule version.",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        help_text="Approval and settlement lifecycle state.",
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reviewed_commissions",
        help_text="Back Office user who approved or rejected this earning.",
    )
    reviewed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp of approval or rejection.",
    )
    rejection_reason = models.TextField(
        max_length=5000,
        blank=True,
        default="",
        help_text="Optional reason explaining rejection.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        permissions = [("review_commissions", "Can review collector commissions")]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gt=0),
                name="commission_positive_amount",
            ),
            models.CheckConstraint(
                condition=models.Q(currency="PHP"),
                name="commission_php_currency",
            ),
            models.CheckConstraint(
                condition=models.Q(rule_version__gt=0),
                name="commission_positive_rule_version",
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=["pending", "approved", "rejected", "paid"]),
                name="commission_valid_status",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    category__in=[
                        "address",
                        "email",
                        "phone",
                        "contact_person",
                        "payment_arrangement",
                    ]
                ),
                name="commission_valid_category",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(status="pending", reviewed_by__isnull=True, reviewed_at__isnull=True)
                    | models.Q(
                        status__in=["approved", "rejected", "paid"],
                        reviewed_by__isnull=False,
                        reviewed_at__isnull=False,
                    )
                ),
                name="commission_review_evidence",
            ),
            models.CheckConstraint(
                condition=models.Q(status="rejected") | models.Q(rejection_reason=""),
                name="commission_rejection_reason_clean",
            ),
        ]
        indexes = [
            models.Index(fields=["collector", "status", "created_at"], name="comm_coll_status_created"),
            models.Index(fields=["client", "status", "created_at"], name="comm_client_status_created"),
        ]

    def clean(self):
        super().clean()
        if self.amount is not None and self.amount <= Decimal("0.00"):
            raise ValidationError({"amount": "Commission amount must be positive."})
        if self.currency != "PHP":
            raise ValidationError({"currency": "Commissions must use PHP currency."})
        if self.status == self.Status.PENDING and (self.reviewed_by is not None or self.reviewed_at is not None):
            raise ValidationError("Pending commissions cannot have review attribution.")
        if self.status in {self.Status.APPROVED, self.Status.REJECTED, self.Status.PAID}:
            if self.reviewed_by is None or self.reviewed_at is None:
                raise ValidationError("Reviewed commissions must record reviewer and review timestamp.")
        if self.status != self.Status.REJECTED and self.rejection_reason:
            raise ValidationError({"rejection_reason": "Rejection reason applies only to rejected commissions."})

    def __str__(self):
        return f"{self.collector.user.username} - {self.get_category_display()}: {self.currency} {self.amount} ({self.get_status_display()})"


class Payout(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        APPROVED = "approved", "Approved"
        PAID = "paid", "Paid"

    id = models.BigAutoField(primary_key=True)
    client = models.ForeignKey(
        "accounts.Client",
        on_delete=models.PROTECT,
        related_name="payouts",
        help_text="Creditor client funding this payout batch.",
    )
    collector = models.ForeignKey(
        "user.CollectorProfile",
        on_delete=models.PROTECT,
        related_name="payouts",
        help_text="Recipient field collector.",
    )
    currency = models.CharField(
        max_length=3,
        default="PHP",
        validators=[RegexValidator(r"^[A-Z]{3}$", "Use a three-letter uppercase currency code.")],
        help_text="Payout denomination (PHP).",
    )
    amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Total payout batch amount in PHP.",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.DRAFT,
        help_text="Lifecycle state of the payout record.",
    )
    reference = models.CharField(
        max_length=100,
        blank=True,
        default="",
        help_text="Internal company payment or disbursement reference.",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_payouts",
        help_text="Back Office user who created this payout draft.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="approved_payouts",
        help_text="Finance/Back Office user who approved this payout.",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    paid_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="paid_payouts",
        help_text="Back Office user who confirmed payment.",
    )
    paid_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        permissions = [("manage_payouts", "Can manage and mark payouts paid")]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gte=0),
                name="payout_nonnegative_amount",
            ),
            models.CheckConstraint(
                condition=models.Q(currency="PHP"),
                name="payout_php_currency",
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=["draft", "approved", "paid"]),
                name="payout_valid_status",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        status="draft",
                        approved_by__isnull=True,
                        approved_at__isnull=True,
                        paid_by__isnull=True,
                        paid_at__isnull=True,
                    )
                    | models.Q(
                        status="approved",
                        approved_by__isnull=False,
                        approved_at__isnull=False,
                        paid_by__isnull=True,
                        paid_at__isnull=True,
                    )
                    | models.Q(
                        status="paid",
                        approved_by__isnull=False,
                        approved_at__isnull=False,
                        paid_by__isnull=False,
                        paid_at__isnull=False,
                    )
                ),
                name="payout_lifecycle_evidence",
            ),
        ]
        indexes = [
            models.Index(fields=["collector", "status", "created_at"], name="payout_coll_status_created"),
            models.Index(fields=["client", "status", "created_at"], name="payout_client_status_created"),
        ]

    def clean(self):
        super().clean()
        if self.amount is not None and self.amount < Decimal("0.00"):
            raise ValidationError({"amount": "Payout amount cannot be negative."})
        if self.currency != "PHP":
            raise ValidationError({"currency": "Payouts must use PHP currency."})
        if self.status == self.Status.DRAFT:
            if self.approved_by or self.approved_at or self.paid_by or self.paid_at:
                raise ValidationError("Draft payout cannot have approval or payment metadata.")
        elif self.status == self.Status.APPROVED:
            if not self.approved_by or not self.approved_at:
                raise ValidationError("Approved payout must record approver and approval timestamp.")
            if self.paid_by or self.paid_at:
                raise ValidationError("Approved payout cannot have payment metadata before being marked paid.")
        elif self.status == self.Status.PAID:
            if not self.approved_by or not self.approved_at or not self.paid_by or not self.paid_at:
                raise ValidationError("Paid payout must record approval and payment metadata.")

    def __str__(self):
        return f"Payout #{self.pk} - {self.collector.user.username} ({self.client.code}): {self.currency} {self.amount} [{self.get_status_display()}]"


class PayoutItem(models.Model):
    id = models.BigAutoField(primary_key=True)
    payout = models.ForeignKey(
        Payout,
        on_delete=models.PROTECT,
        related_name="items",
        help_text="Payout batch grouping this commission.",
    )
    commission = models.OneToOneField(
        Commission,
        on_delete=models.PROTECT,
        related_name="payout_item",
        help_text="Commission settled by this payout.",
    )
    amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Allocated settlement amount in PHP.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gt=0),
                name="payout_item_positive_amount",
            ),
        ]

    def clean(self):
        super().clean()
        if self.amount is not None and self.amount <= Decimal("0.00"):
            raise ValidationError({"amount": "Payout item amount must be positive."})
        if self.payout_id and self.commission_id:
            if self.payout.collector_id != self.commission.collector_id:
                raise ValidationError("Payout and commission must belong to the same collector.")
            if self.payout.client_id != self.commission.client_id:
                raise ValidationError("Payout and commission must belong to the same client.")
            if self.payout.currency != self.commission.currency:
                raise ValidationError("Payout and commission must use the same currency.")
            if self.amount != self.commission.amount:
                raise ValidationError({"amount": "Item amount must match commission amount."})

    def __str__(self):
        return f"Payout #{self.payout_id} -> Commission #{self.commission_id} ({self.amount})"


class PointRule(models.Model):
    id = models.BigAutoField(primary_key=True)
    category = models.CharField(
        max_length=30,
        choices=InformationCategory.choices,
        help_text="Information category for this point rule.",
    )
    version = models.PositiveIntegerField(
        default=1,
        help_text="Monotonically increasing version number for this category.",
    )
    points = models.PositiveIntegerField(
        default=1,
        help_text="Whole points awarded upon approval (1 pt in current version).",
    )
    is_active = models.BooleanField(
        default=True,
        help_text="Whether this rule version is actively awarded to new approvals.",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_point_rules",
        help_text="Back Office staff who published this point rule.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    retired_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp when this rule was superseded by a newer version.",
    )
    retired_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="retired_point_rules",
        help_text="Back Office staff who retired this rule.",
    )

    class Meta:
        permissions = [
            ("manage_point_rules", "Can manage point rules"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["category"],
                condition=models.Q(is_active=True),
                name="pointrule_unique_active_category",
            ),
            models.UniqueConstraint(
                fields=["category", "version"],
                name="pointrule_unique_category_version",
            ),
            models.CheckConstraint(
                condition=models.Q(points=1),
                name="pointrule_exactly_one_point",
            ),
            models.CheckConstraint(
                condition=models.Q(version__gt=0),
                name="pointrule_positive_version",
            ),
            models.CheckConstraint(
                condition=models.Q(category__in=InformationCategory.values),
                name="pointrule_valid_category",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(is_active=True, retired_at__isnull=True, retired_by__isnull=True)
                    | models.Q(is_active=False, retired_at__isnull=False, retired_by__isnull=False)
                ),
                name="pointrule_retirement_consistency",
            ),
        ]

    def clean(self):
        super().clean()
        if self.category not in InformationCategory.values:
            raise ValidationError({"category": "Select an approved information category."})
        if self.version is None or self.version < 1:
            raise ValidationError({"version": "Point rule version must be a positive integer."})
        if self.points is None or self.points != 1:
            raise ValidationError({"points": "Current point rule version allows exactly 1 whole point."})
        if self.is_active:
            if self.retired_at or self.retired_by:
                raise ValidationError("Active point rule cannot record retirement metadata.")
        else:
            if not self.retired_at or not self.retired_by:
                raise ValidationError("Retired point rule must record retirement timestamp and actor.")

    def __str__(self):
        status = "Active" if self.is_active else "Retired"
        return f"PointRule({self.get_category_display()} v{self.version}: {self.points} pt [{status}])"


class PointTransaction(models.Model):
    id = models.BigAutoField(primary_key=True)
    proposal = models.OneToOneField(
        "accounts.AccountInformationProposal",
        on_delete=models.PROTECT,
        related_name="point_transaction",
        help_text="Source validated information proposal.",
    )
    collector = models.ForeignKey(
        "user.CollectorProfile",
        on_delete=models.PROTECT,
        related_name="point_transactions",
        help_text="Collector who submitted the validated proposal.",
    )
    point_rule = models.ForeignKey(
        PointRule,
        on_delete=models.PROTECT,
        related_name="transactions",
        help_text="Rule version governing this award.",
    )
    category = models.CharField(
        max_length=30,
        choices=InformationCategory.choices,
        help_text="Information category snapshot.",
    )
    points = models.PositiveIntegerField(
        default=1,
        help_text="Frozen point amount awarded.",
    )
    rule_version = models.PositiveIntegerField(
        help_text="Version number of the point rule at the time of award.",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="approved_point_transactions",
        help_text="Back Office reviewer who approved the proposal.",
    )
    event_key = models.CharField(
        max_length=128,
        blank=True,
        default="",
        help_text="Safe non-sensitive identifier derived from proposal and category.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(points__gt=0),
                name="point_txn_positive_points",
            ),
            models.CheckConstraint(
                condition=models.Q(rule_version__gt=0),
                name="point_txn_positive_rule_version",
            ),
            models.CheckConstraint(
                condition=models.Q(category__in=InformationCategory.values),
                name="point_txn_valid_category",
            ),
        ]
        indexes = [
            models.Index(fields=["collector", "created_at"], name="point_txn_coll_created"),
            models.Index(fields=["category", "created_at"], name="point_txn_cat_created"),
        ]

    def clean(self):
        super().clean()
        if self.category not in InformationCategory.values:
            raise ValidationError({"category": "Select an approved information category."})
        if self.points is None or self.points <= 0:
            raise ValidationError({"points": "Point transaction points must be positive."})
        if self.rule_version is None or self.rule_version < 1:
            raise ValidationError({"rule_version": "Rule version must be positive."})

    def __str__(self):
        return f"PointTransaction #{self.pk} - {self.collector.user.username}: +{self.points} pt ({self.get_category_display()})"
