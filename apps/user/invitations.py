import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from apps.user.models import CollectorInvitation, CollectorProfile

INVITATION_EXPIRY_HOURS = getattr(settings, "INVITATION_EXPIRY_HOURS", 72)


def generate_invitation_token() -> str:
    """Generate a cryptographically secure URL-safe random token."""
    return secrets.token_urlsafe(32)


def hash_invitation_token(raw_token: str) -> str:
    """Compute SHA-256 hash of raw invitation token for secure database storage."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def send_invitation_email(invitation: CollectorInvitation, raw_token: str, request=None, *, account_type="Collector", setup_route="collector_invitation_setup"):
    """Send a secure one-time password setup link to the collector email.
    
    NEVER includes any password in the email.
    Uses existing Django email configuration (console in dev, locmem in tests).
    """
    if request is not None:
        setup_url = request.build_absolute_uri(
            reverse(setup_route, kwargs={"token": raw_token})
        )
    else:
        path = reverse(setup_route, kwargs={"token": raw_token})
        setup_url = f"{settings.SITE_URL.rstrip('/')}{path}"

    display_name = invitation.user.get_full_name() or invitation.user.username
    subject = f"Set your FiCol {account_type} password"
    context = {
        "full_name": display_name,
        "activation_url": setup_url,
        "expiry_hours": INVITATION_EXPIRY_HOURS,
        "account_type": account_type,
        "login_email": invitation.email,
        "login_username": invitation.email if account_type == "Collector" else invitation.user.username,
    }
    message = render_to_string("emails/collector_invitation.txt", context)
    html_message = render_to_string("emails/collector_invitation.html", context)

    from_email = getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@ficol.local")
    email = EmailMultiAlternatives(
        subject=subject,
        body=message,
        from_email=from_email,
        to=[invitation.email],
    )
    email.attach_alternative(html_message, "text/html")
    return email.send(fail_silently=False)


def get_collector_account_setup_status(user):
    """Compute the account setup lifecycle status for a collector user.
    
    Possible statuses:
    - "Invitation sent"
    - "Invitation accepted / password set"
    - "Account activated"
    - "Activated — Collector Portal not yet visited"
    - "Collector Portal visited"
    - "Invitation revoked"
    - "Invitation expired"
    """
    profile = CollectorProfile.objects.filter(user=user).first()
    latest_invitation = user.collector_invitations.order_by("-created_at").first()

    first_visit = profile.first_portal_visit_at if profile else None
    last_activity = profile.last_portal_activity_at if profile else None

    if first_visit is not None:
        label = "Collector Portal visited"
        badge = "success"
    elif user.is_active and user.account_activated_at:
        label = "Activated — Collector Portal not yet visited"
        badge = "info"
    elif user.is_active and not user.account_activated_at:
        label = "Account activated"
        badge = "info"
    elif latest_invitation:
        if latest_invitation.status == CollectorInvitation.Status.ACCEPTED:
            label = "Invitation accepted / password set"
            badge = "info"
        elif latest_invitation.status == CollectorInvitation.Status.REVOKED:
            label = "Invitation revoked"
            badge = "danger"
        elif latest_invitation.status == CollectorInvitation.Status.EXPIRED or (
            latest_invitation.status == CollectorInvitation.Status.SENT
            and latest_invitation.expires_at <= timezone.now()
        ):
            label = "Invitation expired"
            badge = "warning"
        elif latest_invitation.status == CollectorInvitation.Status.SENT:
            label = "Invitation sent"
            badge = "warning"
        else:
            label = f"Invitation {latest_invitation.status}"
            badge = "secondary"
    else:
        if user.is_active:
            label = "Account activated"
            badge = "info"
        else:
            label = "Not invited"
            badge = "secondary"

    return {
        "label": label,
        "badge": badge,
        "first_visit": first_visit,
        "last_activity": last_activity,
        "latest_invitation": latest_invitation,
    }


def get_collector_approval_status_info(profile):
    """Return display label and badge class for collector profile approval status."""
    if not profile:
        return {"label": "None", "badge": "secondary"}
    status = profile.approval_status
    if status == CollectorProfile.ApprovalStatus.APPROVED:
        return {"label": "Approved", "badge": "success"}
    elif status == CollectorProfile.ApprovalStatus.PENDING:
        return {"label": "Pending approval", "badge": "warning"}
    elif status == CollectorProfile.ApprovalStatus.REJECTED:
        return {"label": "Rejected", "badge": "danger"}
    elif status == CollectorProfile.ApprovalStatus.SUSPENDED:
        return {"label": "Suspended", "badge": "danger"}
    elif status == CollectorProfile.ApprovalStatus.OFFBOARDED:
        return {"label": "Offboarded", "badge": "secondary"}
    return {"label": status.capitalize(), "badge": "secondary"}


def get_collector_standing_info(profile):
    """Return standing key, display label and badge class for collector standing."""
    if not profile:
        return {"standing": "none", "label": "None", "badge": "secondary"}
    status = profile.approval_status
    if status == CollectorProfile.ApprovalStatus.APPROVED:
        return {"standing": "active", "label": "Active", "badge": "success"}
    elif status == CollectorProfile.ApprovalStatus.SUSPENDED:
        return {"standing": "suspended", "label": "Suspended", "badge": "danger"}
    elif status == CollectorProfile.ApprovalStatus.OFFBOARDED:
        return {"standing": "offboarded", "label": "Offboarded", "badge": "secondary"}
    elif status == CollectorProfile.ApprovalStatus.PENDING:
        return {"standing": "pending", "label": "Pending Approval", "badge": "warning"}
    elif status == CollectorProfile.ApprovalStatus.REJECTED:
        return {"standing": "rejected", "label": "Rejected", "badge": "danger"}
    return {"standing": status, "label": status.capitalize(), "badge": "secondary"}


def get_collector_badge_statuses(user):
    """Compute the 4 discrete badge states for a collector:
    1. Email:
       - Unverified
       - Verification sent
       - Verified
    2. Invitation:
       - Sent
       - Accepted
       - Expired
       - Revoked
       - Failed
    3. Account:
       - Awaiting activation
       - Active
       - Disabled
    4. Collector standing:
       - Active
       - Suspended
       - Offboarded
       (and Pending if pending approval)
    """
    profile = getattr(user, "collector_profile", None)
    latest_invitation = user.collector_invitations.order_by("-created_at").first()

    # 1. Email badge
    if user.email_verified_at is not None:
        email_status = "verified"
        email_label = "Verified"
        email_badge = "success"
    elif (
        latest_invitation
        and latest_invitation.status == CollectorInvitation.Status.SENT
        and not latest_invitation.is_expired
    ):
        email_status = "verification_sent"
        email_label = "Verification sent"
        email_badge = "info"
    else:
        email_status = "unverified"
        email_label = "Unverified"
        email_badge = "secondary"

    # 2. Invitation badge
    if not latest_invitation:
        inv_status = "none"
        inv_label = "None"
        inv_badge = "secondary"
    elif latest_invitation.status == CollectorInvitation.Status.ACCEPTED:
        inv_status = "accepted"
        inv_label = "Accepted"
        inv_badge = "success"
    elif latest_invitation.status == CollectorInvitation.Status.REVOKED:
        inv_status = "revoked"
        inv_label = "Revoked"
        inv_badge = "danger"
    elif latest_invitation.status == CollectorInvitation.Status.FAILED:
        inv_status = "failed"
        inv_label = "Failed"
        inv_badge = "danger"
    elif latest_invitation.is_expired or latest_invitation.status == CollectorInvitation.Status.EXPIRED:
        inv_status = "expired"
        inv_label = "Expired"
        inv_badge = "warning"
    elif latest_invitation.status == CollectorInvitation.Status.SENT:
        inv_status = "sent"
        inv_label = "Sent"
        inv_badge = "info"
    else:
        inv_status = latest_invitation.status
        inv_label = latest_invitation.get_status_display()
        inv_badge = "secondary"

    # 3. Account badge
    if user.is_active:
        account_status = "active"
        account_label = "Active"
        account_badge = "success"
    elif user.account_activated_at is not None:
        account_status = "disabled"
        account_label = "Disabled"
        account_badge = "danger"
    else:
        account_status = "awaiting_activation"
        account_label = "Awaiting activation"
        account_badge = "warning"

    # 4. Collector standing badge
    standing_status = "none"
    standing_label = "None"
    standing_badge = "secondary"
    if profile:
        st = profile.approval_status
        if st == CollectorProfile.ApprovalStatus.APPROVED:
            standing_status = "active"
            standing_label = "Active"
            standing_badge = "success"
        elif st == CollectorProfile.ApprovalStatus.SUSPENDED:
            standing_status = "suspended"
            standing_label = "Suspended"
            standing_badge = "warning"
        elif st == CollectorProfile.ApprovalStatus.OFFBOARDED:
            standing_status = "offboarded"
            standing_label = "Offboarded"
            standing_badge = "danger"
        elif st == CollectorProfile.ApprovalStatus.PENDING:
            standing_status = "pending"
            standing_label = "Pending"
            standing_badge = "secondary"
        elif st == CollectorProfile.ApprovalStatus.REJECTED:
            standing_status = "rejected"
            standing_label = "Rejected"
            standing_badge = "danger"

    return {
        "email": {"status": email_status, "label": email_label, "badge": email_badge},
        "invitation": {"status": inv_status, "label": inv_label, "badge": inv_badge},
        "account": {"status": account_status, "label": account_label, "badge": account_badge},
        "standing": {"status": standing_status, "label": standing_label, "badge": standing_badge},
    }


