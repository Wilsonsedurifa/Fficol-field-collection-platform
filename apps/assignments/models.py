from django.contrib.gis.db import models
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.conf import settings


class CollectorLocation(models.Model):
    id = models.BigAutoField(primary_key=True)
    collector = models.OneToOneField(
        "user.CollectorProfile",
        on_delete=models.CASCADE,
        related_name="location",
        help_text="Collector whose latest accepted GPS observation is stored here.",
    )
    position = models.PointField(
        geography=True,
        srid=4326,
        spatial_index=True,
        help_text="WGS84 position: longitude first, latitude second.",
    )
    accuracy_m = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        validators=[MinValueValidator(0)],
        help_text="Reported GPS uncertainty in metres; zero or greater.",
    )
    captured_at = models.DateTimeField(
        help_text="Device observation time; receiving an old fix does not make it fresh.",
    )
    received_at = models.DateTimeField(
        auto_now=True,
        help_text="Server receipt time; include this field when saving accepted updates.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(accuracy_m__gte=0),
                name="collector_location_accuracy_nonnegative",
            ),
        ]


class Territory(models.Model):
    """Optional address classification, not a replacement for GPS matching."""

    id = models.BigAutoField(primary_key=True)
    code = models.CharField(max_length=40, unique=True)
    name = models.CharField(max_length=150)
    city = models.CharField(max_length=100)
    barangay = models.CharField(max_length=100, blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name


class AccountOffer(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ACCEPTED = "accepted", "Accepted"
        DECLINED = "declined", "Declined"
        EXPIRED = "expired", "Expired"
        WITHDRAWN = "withdrawn", "Withdrawn"

    id = models.BigAutoField(primary_key=True)
    account = models.ForeignKey("accounts.Account", on_delete=models.PROTECT, related_name="offers")
    collector = models.ForeignKey("user.CollectorProfile", on_delete=models.PROTECT, related_name="account_offers")
    target_address = models.ForeignKey("accounts.CustomerAddress", on_delete=models.PROTECT, related_name="offers")
    target_position = models.PointField(geography=True, srid=4326)
    commission_rule = models.ForeignKey("collection.CommissionRule", on_delete=models.PROTECT, related_name="offers")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    expires_at = models.DateTimeField()
    responded_at = models.DateTimeField(null=True, blank=True)
    collector_position = models.PointField(geography=True, srid=4326)
    location_captured_at = models.DateTimeField()
    distance_m = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(0)])
    matching_snapshot = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["account", "collector"], condition=models.Q(status="pending"), name="offer_unique_pending_recipient"),
            models.CheckConstraint(condition=models.Q(status__in=["pending", "accepted", "declined", "expired", "withdrawn"]), name="offer_valid_status"),
            models.CheckConstraint(condition=models.Q(distance_m__gte=0), name="offer_nonnegative_distance"),
            models.CheckConstraint(condition=~models.Q(status__in=["accepted", "declined"]) | models.Q(responded_at__isnull=False), name="offer_response_has_timestamp"),
            models.CheckConstraint(condition=models.Q(expires_at__gt=models.F("created_at")), name="offer_expiry_after_creation"),
        ]
        indexes = [models.Index(fields=["collector", "status", "expires_at"], name="offer_collector_status_expiry")]


class AccountAssignment(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        COMPLETED = "completed", "Completed"
        RELEASED = "released", "Released"
        REASSIGNED = "reassigned", "Reassigned"
        EXPIRED = "expired", "Expired"

    id = models.BigAutoField(primary_key=True)
    account = models.ForeignKey("accounts.Account", on_delete=models.PROTECT, related_name="assignments")
    collector = models.ForeignKey("user.CollectorProfile", on_delete=models.PROTECT, related_name="account_assignments")
    offer = models.OneToOneField(AccountOffer, on_delete=models.PROTECT, null=True, blank=True, related_name="assignment")
    target_address = models.ForeignKey("accounts.CustomerAddress", on_delete=models.PROTECT, related_name="assignments")
    commission_rule = models.ForeignKey("collection.CommissionRule", on_delete=models.PROTECT, related_name="assignments")
    assigned_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="created_account_assignments")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    first_action_due_at = models.DateTimeField()
    target_position = models.PointField(geography=True, srid=4326)
    policy_snapshot = models.JSONField()
    next_action_due_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    end_reason = models.TextField(blank=True, default="")
    event_id = models.UUIDField(null=True, blank=True, unique=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        permissions = [
            ("manage_account_assignments", "Can manage account assignments"),
        ]
        constraints = [
            models.UniqueConstraint(fields=["account"], condition=models.Q(status="active"), name="assignment_one_active_owner"),
            models.CheckConstraint(condition=models.Q(status__in=["active", "completed", "released", "reassigned", "expired"]), name="assignment_valid_status"),
            models.CheckConstraint(condition=models.Q(status="active", ended_at__isnull=True) | (~models.Q(status="active") & models.Q(ended_at__isnull=False)), name="assignment_end_matches_status"),
            models.CheckConstraint(condition=models.Q(first_action_due_at__gt=models.F("created_at")), name="assignment_first_action_future"),
        ]
        indexes = [models.Index(fields=["collector", "status", "first_action_due_at"], name="assignment_collector_work")]


class AssignmentReleaseRequest(models.Model):
    class Status(models.TextChoices):
        REQUESTED = "requested", "Requested"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        SUPERSEDED = "superseded", "Superseded"

    id = models.BigAutoField(primary_key=True)
    assignment = models.ForeignKey(
        AccountAssignment,
        on_delete=models.PROTECT,
        related_name="release_requests",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="requested_assignment_releases",
    )
    event_id = models.UUIDField(unique=True, editable=False)
    reason = models.TextField(max_length=5000)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.REQUESTED,
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reviewed_assignment_releases",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_reason = models.TextField(blank=True, default="", max_length=5000)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "assignments_assignmentreleaserequest"
        permissions = [
            ("review_release_requests", "Can review release requests"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["assignment"],
                condition=models.Q(status="requested"),
                name="unique_pending_release_request_per_assignment",
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=["requested", "approved", "rejected", "superseded"]),
                name="release_request_valid_status",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(status="requested", reviewed_by__isnull=True, reviewed_at__isnull=True)
                    | models.Q(status__in=["approved", "rejected", "superseded"], reviewed_by__isnull=False, reviewed_at__isnull=False)
                ),
                name="release_request_review_evidence",
            ),
            models.CheckConstraint(
                condition=~models.Q(status="rejected") | ~models.Q(review_reason=""),
                name="release_request_rejected_reason",
            ),
            models.CheckConstraint(
                condition=~models.Q(status="superseded") | ~models.Q(review_reason=""),
                name="release_request_superseded_reason",
            ),
            models.CheckConstraint(
                condition=~models.Q(reason=""),
                name="release_request_reason_not_empty",
            ),
        ]
        indexes = [
            models.Index(fields=["assignment", "status"], name="rel_req_assignment_status"),
            models.Index(fields=["status", "created_at"], name="rel_req_status_created"),
        ]

    def clean(self):
        super().clean()
        if self.reason is not None:
            self.reason = self.reason.strip()
        if not self.reason:
            raise ValidationError({"reason": "A reason is required to request release."})
        if len(self.reason) > 5000:
            raise ValidationError({"reason": "Reason cannot exceed 5000 characters."})
        if self.status == "rejected" and not (self.review_reason and self.review_reason.strip()):
            raise ValidationError({"review_reason": "A non-blank reason is required when rejecting a release request."})
        if self.status == "superseded" and not (self.review_reason and self.review_reason.strip()):
            raise ValidationError({"review_reason": "A non-blank reason is required when superseding a release request."})
        if self.status == "requested":
            if self.reviewed_by_id or self.reviewed_at:
                raise ValidationError({"status": "Pending requests cannot have review details."})
        elif self.status in ["approved", "rejected", "superseded"]:
            if not self.reviewed_by_id or not self.reviewed_at:
                raise ValidationError({"status": "Finalized requests require reviewed_by and reviewed_at."})

    @property
    def has_ongoing_visit(self):
        if hasattr(self.assignment, "_prefetched_objects_cache") and "activities" in getattr(self.assignment, "_prefetched_objects_cache", {}):
            return any(getattr(a, "status", None) == "in_progress" for a in self.assignment._prefetched_objects_cache["activities"])
        activities = getattr(self.assignment, "activities", None)
        if activities is not None:
            if hasattr(activities, "filter"):
                return activities.filter(status="in_progress").exists()
            if hasattr(activities, "all"):
                return any(getattr(a, "status", None) == "in_progress" for a in activities.all())
        return False

    def __str__(self):
        return f"ReleaseRequest #{self.pk or 'new'} ({self.status}) for Assignment #{self.assignment_id}"

