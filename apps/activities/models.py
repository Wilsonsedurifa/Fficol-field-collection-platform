import uuid
import math

from django.conf import settings
from django.contrib.gis.db import models
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator


class FieldActivity(models.Model):
    class Status(models.TextChoices):
        IN_PROGRESS = "in_progress", "In progress"
        SUBMITTED = "submitted", "Submitted"
        CANCELLED = "cancelled", "Cancelled"

    class Outcome(models.TextChoices):
        CONTACTED = "contacted", "Customer contacted"
        UNAVAILABLE = "unavailable", "Customer unavailable"
        PROMISE = "promise_to_pay", "Promise to pay"
        WRONG_ADDRESS = "wrong_address", "Address incorrect"
        DISPUTED = "disputed", "Customer disputed the account"
        OTHER = "other", "Other"

    id = models.BigAutoField(primary_key=True)
    account = models.ForeignKey("accounts.Account", on_delete=models.PROTECT, related_name="field_activities")
    assignment = models.ForeignKey("assignments.AccountAssignment", on_delete=models.PROTECT, null=True, blank=True, related_name="activities")
    scheduled_followup = models.ForeignKey("activities.FollowUpTask", on_delete=models.PROTECT, null=True, blank=True, related_name="resulting_visits", help_text="Scheduled revisit consumed by this attempt; cancelled attempts do not consume it.")
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="field_activities")
    event_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    kind = models.CharField(max_length=20, default="visit", choices=[("visit", "Visit"), ("phone", "Phone"), ("sms", "SMS"), ("email", "Email"), ("skip_trace", "Skip trace")])
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.IN_PROGRESS)
    outcome = models.CharField(max_length=30, choices=Outcome.choices, blank=True, default="")
    occurred_at = models.DateTimeField(help_text="Server-recorded visit start time.")
    ended_at = models.DateTimeField(null=True, blank=True)
    position = models.PointField(geography=True, srid=4326, null=True, blank=True)
    accuracy_m = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(0)])
    location_captured_at = models.DateTimeField(null=True, blank=True)
    distance_m = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(0)])
    location_issue_reason = models.CharField(max_length=500, blank=True, default="")
    notes = models.TextField(max_length=5000, blank=True, default="")
    cancel_reason = models.CharField(max_length=500, blank=True, default="")
    submission_fingerprint = models.CharField(max_length=64, blank=True, default="", editable=False, help_text="Digest of normalized completion data for safe exact retries; not visit evidence.")
    review_status = models.CharField(max_length=20, default="not_required", choices=[("not_required", "Review not required"), ("pending", "Pending"), ("approved", "Approved"), ("rejected", "Rejected")])
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="reviewed_field_activities")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_reason = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["assignment"], condition=models.Q(status="in_progress", kind="visit"), name="activity_one_ongoing_visit"),
            models.UniqueConstraint(fields=["recorded_by"], condition=models.Q(status="in_progress", kind="visit"), name="activity_one_collector_visit"),
            models.UniqueConstraint(fields=["scheduled_followup"], condition=models.Q(status__in=["in_progress", "submitted"]), name="activity_one_visit_per_followup"),
            models.CheckConstraint(condition=models.Q(kind__in=["visit", "phone", "sms", "email", "skip_trace"]), name="activity_valid_kind"),
            models.CheckConstraint(condition=(models.Q(status="in_progress", ended_at__isnull=True, outcome="", cancel_reason="", submission_fingerprint="", review_status__in=["pending", "not_required"]) | (models.Q(status="submitted", ended_at__isnull=False, outcome__in=["contacted", "unavailable", "promise_to_pay", "wrong_address", "disputed", "other"], cancel_reason="") & ~models.Q(submission_fingerprint="")) | (models.Q(status="cancelled", ended_at__isnull=False, outcome="", submission_fingerprint="", review_status__in=["pending", "not_required"]) & ~models.Q(cancel_reason=""))), name="activity_valid_lifecycle"),
            models.CheckConstraint(condition=models.Q(ended_at__isnull=True) | models.Q(ended_at__gte=models.F("occurred_at")), name="activity_end_after_start"),
            models.CheckConstraint(condition=models.Q(accuracy_m__isnull=True) | models.Q(accuracy_m__gte=0), name="activity_nonnegative_accuracy"),
            models.CheckConstraint(condition=models.Q(distance_m__isnull=True) | models.Q(distance_m__gte=0), name="activity_nonnegative_distance"),
            models.CheckConstraint(condition=(models.Q(position__isnull=True, accuracy_m__isnull=True, location_captured_at__isnull=True, distance_m__isnull=True) | models.Q(position__isnull=False, accuracy_m__isnull=False, location_captured_at__isnull=False, distance_m__isnull=False)), name="activity_location_evidence_set"),
            models.CheckConstraint(condition=~models.Q(kind="visit") | models.Q(position__isnull=False) | ~models.Q(location_issue_reason=""), name="activity_missing_gps_reason"),
            models.CheckConstraint(condition=(models.Q(review_status__in=["pending", "not_required"], reviewed_by__isnull=True, reviewed_at__isnull=True) | models.Q(review_status__in=["approved", "rejected"], reviewed_by__isnull=False, reviewed_at__isnull=False)), name="activity_review_evidence"),
            models.CheckConstraint(condition=~models.Q(review_status="rejected") | ~models.Q(review_reason=""), name="activity_rejected_reason"),
        ]
        indexes = [models.Index(fields=["assignment", "occurred_at"], name="activity_assignment_history")]

    def clean(self):
        super().clean()
        if self.assignment_id and self.account_id != self.assignment.account_id:
            raise ValidationError({"assignment": "Activity and assignment must belong to the same account."})
        if self.kind == "visit" and not self.assignment_id:
            raise ValidationError({"assignment": "Collector visits require an assignment."})
        if self.scheduled_followup_id and (self.kind != "visit" or self.scheduled_followup.kind != "revisit" or self.scheduled_followup.account_id != self.account_id or self.scheduled_followup.assignment_id != self.assignment_id or self.scheduled_followup.assignee_id != self.recorded_by_id):
            raise ValidationError({"scheduled_followup": "A visit must use its collector's revisit task on the same account and assignment."})
        if self.position is not None and (self.position.empty or self.position.hasz or self.position.srid != 4326 or not math.isfinite(self.position.x) or not math.isfinite(self.position.y) or not -180 <= self.position.x <= 180 or not -90 <= self.position.y <= 90):
            raise ValidationError({"position": "Use a finite two-dimensional WGS84 point."})


class FollowUpTask(models.Model):
    id = models.BigAutoField(primary_key=True)
    account = models.ForeignKey("accounts.Account", on_delete=models.PROTECT, related_name="follow_up_tasks")
    assignment = models.ForeignKey("assignments.AccountAssignment", on_delete=models.PROTECT, null=True, blank=True, related_name="follow_up_tasks")
    source_activity = models.ForeignKey(FieldActivity, on_delete=models.PROTECT, null=True, blank=True, related_name="follow_up_tasks")
    assignee = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="follow_up_tasks")
    kind = models.CharField(max_length=30, choices=[("revisit", "Revisit"), ("call", "Call"), ("promise_check", "Promise check"), ("review", "Review")])
    due_at = models.DateTimeField()
    status = models.CharField(max_length=20, default="requested", choices=[("requested", "Awaiting schedule"), ("pending", "Scheduled"), ("rejected", "Request rejected"), ("completed", "Completed"), ("cancelled", "Cancelled")])
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="requested_followups")
    available_from = models.DateTimeField(null=True, blank=True, help_text="Authorized earliest start; null on preserved historical schedules.")
    authorized_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="authorized_followups")
    authorized_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True, default="", max_length=5000)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(kind__in=["revisit", "call", "promise_check", "review"]), name="followup_valid_kind"),
            models.CheckConstraint(condition=models.Q(available_from__isnull=True, authorized_by__isnull=True, authorized_at__isnull=True) | models.Q(available_from__isnull=False, authorized_by__isnull=False, authorized_at__isnull=False, due_at__gt=models.F("available_from")), name="followup_authorization_window"),
            models.CheckConstraint(condition=~models.Q(status__in=["requested", "rejected"]) | models.Q(available_from__isnull=True, authorized_by__isnull=True, authorized_at__isnull=True), name="followup_request_not_authorized"),
            models.CheckConstraint(condition=models.Q(status="completed", completed_at__isnull=False) | models.Q(status__in=["requested", "pending", "rejected", "cancelled"], completed_at__isnull=True), name="followup_completion_evidence"),
        ]
        indexes = [models.Index(fields=["assignment", "status", "due_at"], name="followup_assignment_due")]

    def clean(self):
        super().clean()
        if self.assignment_id and self.assignment.account_id != self.account_id:
            raise ValidationError({"assignment": "Follow-up and assignment must share an account."})
        if self.source_activity_id and (self.source_activity.account_id != self.account_id or self.source_activity.assignment_id != self.assignment_id):
            raise ValidationError({"source_activity": "Follow-up must use the activity's account and assignment."})


class Document(models.Model):
    class Kind(models.TextChoices):
        PHOTO = "photo", "Photo"
        RECEIPT = "receipt", "Receipt"
        DEMAND_LETTER = "demand_letter", "Demand letter"
        OTHER = "other", "Other"

    id = models.BigAutoField(primary_key=True)
    account = models.ForeignKey("accounts.Account", on_delete=models.PROTECT, related_name="documents")
    activity = models.ForeignKey(FieldActivity, on_delete=models.PROTECT, null=True, blank=True, related_name="documents")
    payment = models.ForeignKey("collection.Payment", on_delete=models.PROTECT, null=True, blank=True, related_name="documents")
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="uploaded_documents")
    kind = models.CharField(max_length=30, choices=Kind.choices, default=Kind.RECEIPT)
    file = models.FileField(max_length=500, upload_to="evidence/%Y/%m/")
    original_name = models.CharField(max_length=255)
    mime_type = models.CharField(max_length=100)
    size_bytes = models.PositiveBigIntegerField()
    sha256 = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(kind__in=["photo", "receipt", "demand_letter", "other"]), name="document_valid_kind"),
            models.CheckConstraint(condition=models.Q(size_bytes__gt=0), name="document_positive_size"),
            models.CheckConstraint(condition=~models.Q(mime_type=""), name="document_mime_type_not_empty"),
            models.CheckConstraint(condition=~models.Q(original_name=""), name="document_original_name_not_empty"),
            models.CheckConstraint(condition=~models.Q(sha256=""), name="document_sha256_not_empty"),
        ]
        indexes = [
            models.Index(fields=["account", "kind", "created_at"], name="document_account_kind_created"),
            models.Index(fields=["sha256"], name="document_sha256_idx"),
        ]

    def clean(self):
        super().clean()
        if self.activity_id and self.account_id != self.activity.account_id:
            raise ValidationError({"activity": "Document and activity must share an account."})
        if self.payment_id and self.account_id != self.payment.account_id:
            raise ValidationError({"payment": "Document and payment must share an account."})

    def __str__(self):
        return f"{self.original_name} ({self.get_kind_display()})"

