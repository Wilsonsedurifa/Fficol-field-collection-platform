"""
Core notification services for FiCol.
Provides shared qualification logic for collector offers and destination resolution.
"""
from django.urls import reverse
from django.contrib.auth import get_user_model
from django.db.models import Q
from django.utils import timezone

from apps.assignments.models import AccountAssignment
from apps.assignments.services import client_ids
from apps.core.models import Notification
from apps.user.models import CollectorProfile
from apps.user.models import ClientAccess


def notify_client_reviewers(account, permission, event_key, message, *, exclude_user_id=None):
    """Notify active, client-scoped users who can perform one review action."""
    app_label, codename = permission.split(".", 1)
    reviewers = get_user_model().objects.filter(
        is_active=True,
        client_accesses__client_id=account.client_id,
        client_accesses__is_active=True,
    ).filter(
        Q(is_superuser=True)
        | Q(user_permissions__content_type__app_label=app_label, user_permissions__codename=codename)
        | Q(groups__permissions__content_type__app_label=app_label, groups__permissions__codename=codename)
    )
    if exclude_user_id:
        reviewers = reviewers.exclude(pk=exclude_user_id)
    for reviewer_id in reviewers.distinct().values_list("pk", flat=True):
        Notification.objects.get_or_create(
            recipient_id=reviewer_id,
            account=account,
            dedup_key=f"{event_key}:user:{reviewer_id}",
            defaults={"kind": "review", "message": message},
        )


def notify_user(user_id, account, event_key, message, *, kind="review"):
    if user_id:
        Notification.objects.get_or_create(
            recipient_id=user_id,
            account=account,
            dedup_key=f"{event_key}:user:{user_id}",
            defaults={"kind": kind, "message": message},
        )


def get_qualifying_unread_offers_queryset(user, now=None, collector_profile=None, active_client_ids=None):
    """
    Return a QuerySet of unread offer notifications that qualify for current active offers.

    Rules enforced:
    - Recipient is the authenticated, active user.
    - Notification kind is 'offer'.
    - Notification is unread (read_at is NULL).
    - Notification is linked to an AccountOffer.
    - Offer belongs to the user's approved CollectorProfile.
    - Offer status is 'pending'.
    - Offer has not expired (expires_at > now).
    - Offer account client is within the collector's active client scope.
    """
    if not user or not getattr(user, "is_authenticated", False) or not getattr(user, "is_active", False):
        return Notification.objects.none()

    if collector_profile is None:
        collector_profile = CollectorProfile.objects.filter(
            user=user, approval_status="approved"
        ).first()
    if not collector_profile:
        return Notification.objects.none()

    if active_client_ids is None:
        active_client_ids = set(client_ids(collector_profile))
    if not active_client_ids:
        return Notification.objects.none()

    if now is None:
        now = timezone.now()

    return Notification.objects.filter(
        recipient=user,
        kind="offer",
        read_at__isnull=True,
        offer__isnull=False,
        offer__collector=collector_profile,
        offer__status="pending",
        offer__expires_at__gt=now,
        offer__account__client_id__in=active_client_ids,
    )


def get_qualifying_offers_summary(user, now=None, collector_profile=None, active_client_ids=None):
    """
    Return a summary dict for qualifying unread offers or None if no qualifying offers exist.

    Returns:
        dict with:
            - 'count': number of distinct qualifying pending offers
            - 'notification_ids': set of Notification PKs corresponding to qualifying offers
        or None.
    """
    qualifying_qs = get_qualifying_unread_offers_queryset(
        user, now=now, collector_profile=collector_profile, active_client_ids=active_client_ids
    )
    qualifying_notifs = list(qualifying_qs.select_related("offer__account"))
    if not qualifying_notifs:
        return None

    distinct_active_offers_count = len({n.offer_id for n in qualifying_notifs})
    grouped_ids = {n.pk for n in qualifying_notifs}
    return {
        "count": distinct_active_offers_count,
        "notification_ids": grouped_ids,
    }


def resolve_notification_destination(
    user,
    portal,
    notification,
    collector_profile=None,
    active_assignments=None,
    active_client_ids=None,
    now=None,
):
    """
    Resolve safe destination URL and label for a notification based on portal rules.

    Returns (destination_url, action_label) or (None, None).
    """
    if portal == "backoffice":
        destinations = (
            ("payment-review-needed-", "backoffice_payments", "Review payment"),
            ("visit-review-needed-", "backoffice_visits", "Review visit"),
            ("followup-request-", "backoffice_followups", "Schedule follow-up"),
            ("release-request-", "backoffice_release_requests", "Review release request"),
            ("information-proposal-", "backoffice_information_proposals", "Validate information"),
        )
        for prefix, route, label in destinations:
            if notification.dedup_key.startswith(prefix):
                return reverse(route), label
        return None, None

    if portal == "collector":
        if now is None:
            now = timezone.now()

        if active_client_ids is None:
            if collector_profile is None:
                collector_profile = CollectorProfile.objects.filter(
                    user=user, approval_status="approved"
                ).first()
            if collector_profile:
                active_client_ids = set(client_ids(collector_profile))
            else:
                active_client_ids = set()

        if notification.kind == "offer":
            if notification.offer_id:
                return reverse("assignments:account_offers"), "View offers"
            return None, None

        if notification.account_id:
            if active_assignments is not None:
                assignment_pk = active_assignments.get(notification.account_id)
                if assignment_pk:
                    return reverse("assignments:account_detail", args=[assignment_pk]), "Open account"
                return None, None

            if collector_profile is None:
                collector_profile = CollectorProfile.objects.filter(
                    user=user, approval_status="approved"
                ).first()
            if collector_profile and active_client_ids:
                assignment = AccountAssignment.objects.filter(
                    collector=collector_profile,
                    account_id=notification.account_id,
                    status="active",
                    account__client_id__in=active_client_ids,
                ).first()
                if assignment:
                    return reverse("assignments:account_detail", args=[assignment.pk]), "Open account"

    return None, None
