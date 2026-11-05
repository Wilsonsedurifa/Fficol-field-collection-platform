from django.utils import timezone

from apps.assignments.models import AccountAssignment
from apps.assignments.services import client_ids
from apps.core.models import Notification
from apps.core.notification_services import (
    get_qualifying_offers_summary,
    resolve_notification_destination,
)
from apps.user.models import CollectorProfile, has_backoffice_access


def unread_notifications(request):
    """Provide unread notifications count and dropdown data for the authenticated recipient."""
    user = getattr(request, "user", None)
    if not user or not getattr(user, "is_authenticated", False):
        return {
            "unread_notifications_count": 0,
            "notification_dropdown_items": [],
            "notification_dropdown_offer_summary": None,
        }

    unread_count = Notification.objects.filter(recipient=user, read_at__isnull=True).count()
    if unread_count == 0:
        return {
            "unread_notifications_count": 0,
            "notification_dropdown_items": [],
            "notification_dropdown_offer_summary": None,
        }

    # Determine user role
    is_bo = has_backoffice_access(user)

    dropdown_offer_summary = None
    dropdown_items = []
    now = timezone.now()

    if not is_bo:
        collector_profile = CollectorProfile.objects.filter(
            user=user, approval_status="approved"
        ).first()
        if collector_profile:
            active_client_ids = set(client_ids(collector_profile))
            offer_summary = get_qualifying_offers_summary(
                user, now=now, collector_profile=collector_profile, active_client_ids=active_client_ids
            )
            if offer_summary:
                dropdown_offer_summary = {"count": offer_summary["count"]}
                grouped_ids = offer_summary["notification_ids"]
                remaining_slots = 3 - 1
                other_qs = (
                    Notification.objects.filter(recipient=user, read_at__isnull=True)
                    .exclude(id__in=grouped_ids)
                    .select_related("account", "offer__account")
                    .order_by("-created_at", "-id")[:remaining_slots]
                )
            else:
                remaining_slots = 3
                other_qs = (
                    Notification.objects.filter(recipient=user, read_at__isnull=True)
                    .select_related("account", "offer__account")
                    .order_by("-created_at", "-id")[:remaining_slots]
                )

            other_notifs = list(other_qs)
            account_ids = [n.account_id for n in other_notifs if n.account_id]
            active_assignments = {}
            if account_ids and active_client_ids:
                assignments = AccountAssignment.objects.filter(
                    collector=collector_profile,
                    account_id__in=account_ids,
                    status="active",
                    account__client_id__in=active_client_ids,
                )
                for a in assignments:
                    active_assignments[a.account_id] = a.pk

            for n in other_notifs:
                url, label = resolve_notification_destination(
                    user=user,
                    portal="collector",
                    notification=n,
                    collector_profile=collector_profile,
                    active_assignments=active_assignments,
                    active_client_ids=active_client_ids,
                    now=now,
                )
                dropdown_items.append({
                    "notification": n,
                    "action_url": url,
                    "action_label": label,
                })

            return {
                "unread_notifications_count": unread_count,
                "notification_dropdown_items": dropdown_items,
                "notification_dropdown_offer_summary": dropdown_offer_summary,
            }

    # For Back Office or general users: show up to 3 newest unread notifications without collector offer grouping
    newest = list(
        Notification.objects.filter(recipient=user, read_at__isnull=True)
        .select_related("account", "offer__account")
        .order_by("-created_at", "-id")[:3]
    )
    dropdown_items = [
        {"notification": n, "action_url": None, "action_label": None}
        for n in newest
    ]

    return {
        "unread_notifications_count": unread_count,
        "notification_dropdown_items": dropdown_items,
        "notification_dropdown_offer_summary": None,
    }


unread_notifications_count = unread_notifications
