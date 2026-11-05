"""Information proposals and approved customer contacts."""

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class AccountInformationProposal(models.Model):
    class Category(models.TextChoices):
        ADDRESS = "address", "Address"
        EMAIL = "email", "Email"
        PHONE = "phone", "Phone number"
        CONTACT_PERSON = "contact_person", "Contact person"
        PAYMENT_ARRANGEMENT = "payment_arrangement", "Payment arrangement"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    id = models.BigAutoField(primary_key=True)
    account = models.ForeignKey("accounts.Account", on_delete=models.PROTECT, related_name="information_proposals")
    customer = models.ForeignKey("accounts.Customer", on_delete=models.PROTECT, related_name="information_proposals")
    assignment = models.ForeignKey("assignments.AccountAssignment", on_delete=models.PROTECT, related_name="information_proposals")
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="information_proposals")
    category = models.CharField(max_length=30, choices=Category.choices)
    current_snapshot = models.JSONField(default=dict, blank=True)
    proposed_payload = models.JSONField()
    explanation = models.TextField(max_length=5000)
    evidence = models.ForeignKey("activities.Document", on_delete=models.PROTECT, null=True, blank=True, related_name="information_proposals")
    evidence_reference = models.CharField(max_length=500, blank=True, default="")
    event_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    payload_fingerprint = models.CharField(max_length=64, editable=False)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="reviewed_information_proposals")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_reason = models.TextField(max_length=5000, blank=True, default="")
    approved_address = models.OneToOneField("accounts.CustomerAddress", on_delete=models.PROTECT, null=True, blank=True, related_name="validated_from_proposal")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        permissions = [("validate_account_information", "Can validate account information proposals")]
        constraints = [
            models.CheckConstraint(condition=models.Q(category__in=["address", "email", "phone", "contact_person", "payment_arrangement"]), name="information_valid_category"),
            models.CheckConstraint(condition=models.Q(status__in=["pending", "approved", "rejected"]), name="information_valid_status"),
            models.CheckConstraint(condition=models.Q(status="pending", reviewed_by__isnull=True, reviewed_at__isnull=True, review_reason="") | models.Q(status__in=["approved", "rejected"], reviewed_by__isnull=False, reviewed_at__isnull=False), name="information_review_evidence"),
            models.CheckConstraint(condition=~models.Q(status="rejected") | ~models.Q(review_reason__regex=r"^\s*$"), name="information_rejection_reason"),
            models.CheckConstraint(condition=models.Q(reviewed_by__isnull=True) | ~models.Q(reviewed_by=models.F("submitted_by")), name="information_independent_review"),
            models.CheckConstraint(condition=~models.Q(payload_fingerprint=""), name="information_fingerprint_set"),
            models.CheckConstraint(condition=models.Q(approved_address__isnull=True) | models.Q(category="address", status="approved"), name="information_address_approved"),
        ]
        indexes = [models.Index(fields=["status", "category", "created_at"], name="information_review_queue"), models.Index(fields=["assignment", "created_at"], name="information_assignment_history")]

    def clean(self):
        super().clean()
        errors = {}
        if self.account_id and self.customer_id and self.account.customer_id != self.customer_id:
            errors["customer"] = "Customer must belong to the proposal's account."
        if self.assignment_id:
            if self.assignment.account_id != self.account_id:
                errors["assignment"] = "Assignment must belong to the proposal's account."
            if self.assignment.collector.user_id != self.submitted_by_id:
                errors["submitted_by"] = "Submitter must be the assignment's collector."
        if self.evidence_id and self.evidence.account_id != self.account_id:
            errors["evidence"] = "Evidence must belong to the proposal's account."
        if self.approved_address_id and self.approved_address.customer_id != self.customer_id:
            errors["approved_address"] = "Approved address must belong to the proposal's customer."
        if not isinstance(self.current_snapshot, dict):
            errors["current_snapshot"] = "Use a structured current value."
        if not isinstance(self.proposed_payload, dict) or not self.proposed_payload:
            errors["proposed_payload"] = "Provide structured proposed information."
        if not self.explanation.strip():
            errors["explanation"] = "Explain the proposed update."
        if self.status == self.Status.REJECTED and not self.review_reason.strip():
            errors["review_reason"] = "A rejection reason is required."
        if errors:
            raise ValidationError(errors)


class CustomerContact(models.Model):
    id = models.BigAutoField(primary_key=True)
    customer = models.ForeignKey("accounts.Customer", on_delete=models.PROTECT, related_name="contacts")
    kind = models.CharField(max_length=30, choices=[("email", "Email"), ("phone", "Phone number"), ("contact_person", "Contact person")])
    value = models.CharField(max_length=500)
    normalized_value = models.CharField(max_length=500)
    is_active = models.BooleanField(default=True)
    validated_from_proposal = models.OneToOneField(AccountInformationProposal, on_delete=models.PROTECT, null=True, blank=True, related_name="validated_contact")
    validated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="validated_customer_contacts")
    validated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # TODO(decision): Do not impose value uniqueness until duplicate policy is approved.
        constraints = [
            models.CheckConstraint(condition=models.Q(kind__in=["email", "phone", "contact_person"]), name="contact_valid_kind"),
            models.CheckConstraint(condition=~models.Q(value__regex=r"^\s*$") & ~models.Q(normalized_value__regex=r"^\s*$"), name="contact_values_not_blank"),
        ]
        indexes = [models.Index(fields=["customer", "kind", "is_active"], name="contact_customer_kind_active")]

    def clean(self):
        super().clean()
        if self.validated_from_proposal_id:
            proposal = self.validated_from_proposal
            if proposal.customer_id != self.customer_id or proposal.category != self.kind or proposal.status != "approved":
                raise ValidationError({"validated_from_proposal": "Use an approved proposal for this customer and contact category."})
            if proposal.reviewed_by_id != self.validated_by_id or proposal.reviewed_at != self.validated_at:
                raise ValidationError({"validated_from_proposal": "Validation attribution must match the proposal decision."})
