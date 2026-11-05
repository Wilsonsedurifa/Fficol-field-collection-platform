from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.core.validators import MinValueValidator
from django.db import models


BACK_OFFICE_PERMISSIONS = (
    "user.view_collector_monitoring",
    "accounts.validate_account_information",
    "activities.change_fieldactivity",
    "activities.change_followuptask",
    "collection.change_payment",
    "assignments.review_release_requests",
    "assignments.manage_account_assignments",
    "collection.manage_earning_rules",
    "collection.review_commissions",
    "collection.manage_payouts",
    "collection.manage_point_rules",
    "accounts.change_account_status",
)


def has_backoffice_role(user):
    """Check if the user is assigned to one or more Back Office role groups."""
    groups = getattr(user, "groups", None)
    if groups is None:
        return False

    from .roles import get_backoffice_role_group_names
    bo_group_names = get_backoffice_role_group_names()

    # QuerySet / RelatedManager
    if callable(getattr(groups, "filter", None)):
        if groups.filter(name__in=bo_group_names).exists():
            return True
        bo_perm_q = models.Q()
        for perm_str in BACK_OFFICE_PERMISSIONS:
            app_label, codename = perm_str.split(".", 1)
            bo_perm_q |= models.Q(
                permissions__content_type__app_label=app_label,
                permissions__codename=codename,
            )
        return groups.filter(bo_perm_q).exists()

    # Iterable / mock groups in tests
    try:
        group_items = list(groups.all()) if callable(getattr(groups, "all", None)) else list(groups)
        for g in group_items:
            g_name = getattr(g, "name", str(g))
            if g_name in bo_group_names or g_name.startswith("Back Office"):
                return True
            g_perms = getattr(g, "permissions", None)
            if g_perms is not None:
                if callable(getattr(g_perms, "filter", None)):
                    bo_perm_q = models.Q()
                    for perm_str in BACK_OFFICE_PERMISSIONS:
                        app_label, codename = perm_str.split(".", 1)
                        bo_perm_q |= models.Q(content_type__app_label=app_label, codename=codename)
                    if g_perms.filter(bo_perm_q).exists():
                        return True
                else:
                    g_perm_list = list(g_perms.all()) if callable(getattr(g_perms, "all", None)) else list(g_perms)
                    for p in g_perm_list:
                        p_code = getattr(p, "codename", str(p))
                        if any(p_code == bp.split(".", 1)[1] for bp in BACK_OFFICE_PERMISSIONS):
                            return True
    except Exception:
        pass

    return False


def has_backoffice_access(user):
    """Back Office access requires superuser status or assignment to a Back Office role group.
    
    Direct permissions alone do not grant Back Office portal access, preventing collectors
    with exceptional direct permissions from being silently rerouted away from the Collector portal.
    """
    if not getattr(user, "is_authenticated", False) or not getattr(user, "is_active", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    return has_backoffice_role(user)


class User(AbstractUser):
    id = models.BigAutoField(primary_key=True)
    email_verified_at = models.DateTimeField(null=True, blank=True)
    account_activated_at = models.DateTimeField(null=True, blank=True)
    collector_account_disabled = models.BooleanField(default=False)

    class Meta:
        permissions = [("view_collector_monitoring", "Can view collector monitoring")]

    def __str__(self):
        return self.username

    @property
    def is_collector_setup_complete(self):
        return bool(self.account_activated_at or (self.is_active and self.has_usable_password()))

    @property
    def is_collector_setup_incomplete(self):
        return not self.is_collector_setup_complete


class CollectorProfile(models.Model):
    class ApprovalStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        SUSPENDED = "suspended", "Suspended"
        OFFBOARDED = "offboarded", "Offboarded"

    class Availability(models.TextChoices):
        ONLINE = "online", "Online"
        BUSY = "busy", "Busy"
        OFFLINE = "offline", "Offline"

    id = models.BigAutoField(primary_key=True)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="collector_profile",
    )
    approval_status = models.CharField(
        max_length=20,
        choices=ApprovalStatus.choices,
        default=ApprovalStatus.APPROVED,
    )
    availability = models.CharField(
        max_length=20,
        choices=Availability.choices,
        default=Availability.OFFLINE,
    )
    # Legacy assignment capacity retained for history; acceptance now uses the
    # global ten-acceptances-per-rolling-24-hours rule, not this stored value.
    capacity_limit = models.PositiveSmallIntegerField(
        default=5, validators=[MinValueValidator(1)]
    )
    matching_radius_m = models.PositiveIntegerField(
        default=5000, validators=[MinValueValidator(1)]
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="approved_collector_profiles",
        null=True,
        blank=True,
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    first_portal_visit_at = models.DateTimeField(null=True, blank=True)
    last_portal_activity_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        permissions = [("approve_collector", "Can approve collectors")]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(
                    approval_status__in=["pending", "approved", "rejected", "suspended", "offboarded"]
                ),
                name="collector_valid_approval_status",
            ),
            models.CheckConstraint(
                condition=models.Q(availability__in=["online", "busy", "offline"]),
                name="collector_valid_availability",
            ),
            models.CheckConstraint(
                condition=models.Q(capacity_limit__gt=0),
                name="collector_positive_capacity",
            ),
            models.CheckConstraint(
                condition=models.Q(matching_radius_m__gt=0),
                name="collector_positive_radius",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(approved_by__isnull=True, approved_at__isnull=True)
                    | models.Q(approved_by__isnull=False, approved_at__isnull=False)
                ),
                name="collector_approval_evidence_pair",
            ),
        ]

    def __str__(self):
        return f"{self.user.username} ({self.get_approval_status_display()})"


class ClientAccess(models.Model):
    id = models.BigAutoField(primary_key=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="client_accesses",
    )
    client = models.ForeignKey(
        "accounts.Client",
        on_delete=models.PROTECT,
        related_name="user_accesses",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "client accesses"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "client"], name="unique_user_client_access"
            )
        ]

    def __str__(self):
        return f"{self.user.username} / {self.client.code}"


class CollectorInvitation(models.Model):
    class Status(models.TextChoices):
        SENT = "sent", "Sent"
        ACCEPTED = "accepted", "Accepted"
        EXPIRED = "expired", "Expired"
        REVOKED = "revoked", "Revoked"
        FAILED = "failed", "Failed"

    id = models.BigAutoField(primary_key=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="collector_invitations",
    )
    email = models.EmailField(max_length=254)
    token_hash = models.CharField(max_length=64, db_index=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.SENT,
        db_index=True,
    )
    expires_at = models.DateTimeField()
    sent_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sent_collector_invitations",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "status"], name="user_inv_status_idx"),
            models.Index(fields=["token_hash"], name="inv_token_hash_idx"),
        ]

    def __str__(self):
        return f"Invitation for {self.user.username} ({self.get_status_display()})"

    @property
    def is_expired(self):
        from django.utils import timezone
        return self.status == self.Status.EXPIRED or (
            self.status == self.Status.SENT and timezone.now() >= self.expires_at
        )

    def is_valid_for_setup(self):
        from django.utils import timezone
        return self.status == self.Status.SENT and timezone.now() < self.expires_at


class BackOfficeInvitation(models.Model):
    id = models.AutoField(primary_key=True, serialize=False, verbose_name="ID")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="backoffice_invitations")
    email = models.EmailField()
    token_hash = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=20, choices=CollectorInvitation.Status.choices, default="sent")
    expires_at = models.DateTimeField()
    sent_at = models.DateTimeField(null=True)
    accepted_at = models.DateTimeField(null=True)
    revoked_at = models.DateTimeField(null=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="sent_backoffice_invitations")
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def is_expired(self):
        from django.utils import timezone
        return timezone.now() >= self.expires_at


class CollectorDisciplinaryRecord(models.Model):
    class ActionType(models.TextChoices):
        WARNING = "warning", "Warning"
        FINAL_WARNING = "final_warning", "Final Warning"
        SUSPENSION = "suspension", "Suspension"
        REINSTATEMENT = "reinstatement", "Reinstatement"
        OFFBOARDING = "offboarding", "Offboarding"

    id = models.BigAutoField(primary_key=True)
    collector = models.ForeignKey(
        CollectorProfile,
        on_delete=models.CASCADE,
        related_name="disciplinary_records",
    )
    action_type = models.CharField(
        max_length=20,
        choices=ActionType.choices,
        db_index=True,
    )
    reason = models.TextField()
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="issued_collector_disciplinary_records",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["collector", "action_type"], name="collector_disc_idx"),
        ]

    def __str__(self):
        return f"{self.get_action_type_display()} for {self.collector.user.username} by {self.issued_by.username}"


