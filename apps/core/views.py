from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from apps.assignments.models import AccountAssignment
from apps.assignments.services import client_ids
from apps.core.models import Notification
from apps.core.notification_services import (
    get_qualifying_offers_summary,
    get_qualifying_unread_offers_queryset,
    resolve_notification_destination,
)
from apps.user.models import CollectorProfile, has_backoffice_access


@require_GET
@never_cache
def landing_page(request):
    """Public FiCol introduction; access to application workflows remains protected."""
    return render(request, "landing.html")


def _resolve_portal(user):
    """Determine portal context for user or raise PermissionDenied."""
    if has_backoffice_access(user):
        return "backoffice"
    if user.is_active and CollectorProfile.objects.filter(user=user, approval_status="approved").exists():
        return "collector"
    raise PermissionDenied("Your account does not have portal access.")


@never_cache
@login_required
def inbox(request):
    portal = _resolve_portal(request.user)

    raw_filter = request.GET.get("filter")
    if raw_filter and raw_filter.strip().lower() == "all":
        filter_type = "all"
    else:
        filter_type = "unread"

    now = timezone.now()
    unread_count = Notification.objects.filter(recipient=request.user, read_at__isnull=True).count()
    all_count = Notification.objects.filter(recipient=request.user).count()

    grouped_offer_notification_ids = set()
    offer_summary = None
    collector_profile = None
    active_client_ids = set()

    if portal == "collector":
        collector_profile = CollectorProfile.objects.filter(
            user=request.user, approval_status="approved"
        ).first()
        if collector_profile:
            active_client_ids = set(client_ids(collector_profile))
            summary = get_qualifying_offers_summary(
                request.user,
                now=now,
                collector_profile=collector_profile,
                active_client_ids=active_client_ids,
            )
            if summary:
                grouped_offer_notification_ids = summary["notification_ids"]
                offer_summary = {
                    "count": summary["count"],
                }

    queryset = Notification.objects.filter(recipient=request.user).select_related("account", "offer__account")
    if filter_type == "unread":
        queryset = queryset.filter(read_at__isnull=True)

    if grouped_offer_notification_ids:
        queryset = queryset.exclude(id__in=grouped_offer_notification_ids)

    queryset = queryset.order_by("-created_at", "-id")

    paginator = Paginator(queryset, 25)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    # Pre-resolve active, client-scoped assignments for account notifications on this page
    active_assignments = {}
    if portal == "collector" and collector_profile and active_client_ids:
        page_account_ids = [n.account_id for n in page_obj if n.account_id]
        if page_account_ids:
            assignments = AccountAssignment.objects.filter(
                collector=collector_profile,
                account_id__in=page_account_ids,
                status="active",
                account__client_id__in=active_client_ids,
            )
            for assignment in assignments:
                active_assignments[assignment.account_id] = assignment.pk

    notification_items = []
    for notification in page_obj:
        action_url, action_label = resolve_notification_destination(
            user=request.user,
            portal=portal,
            notification=notification,
            collector_profile=collector_profile,
            active_assignments=active_assignments,
            active_client_ids=active_client_ids,
            now=now,
        )
        notification_items.append({
            "notification": notification,
            "action_url": action_url,
            "action_label": action_label,
        })

    context = {
        "collector_profile": collector_profile if portal == "collector" else None,
        "page_obj": page_obj,
        "notification_items": notification_items,
        "offer_summary": offer_summary if page_obj.number == 1 else None,
        "filter_type": filter_type,
        "unread_count": unread_count,
        "all_count": all_count,
        "portal": portal,
        "page_title": "Notifications",
        "navbar_title": "Notifications",
    }

    if portal == "backoffice":
        template_name = "notifications/backoffice_inbox.html"
    else:
        template_name = "notifications/collector_inbox.html"

    return render(request, template_name, context)


_resolve_notification_destination = resolve_notification_destination


@never_cache
@login_required
@require_POST
def open_offers(request):
    portal = _resolve_portal(request.user)
    now = timezone.now()

    if portal == "collector":
        qualifying_qs = get_qualifying_unread_offers_queryset(request.user, now=now)
        qualifying_qs.update(read_at=now, updated_at=now)

    return redirect("assignments:account_offers")


@never_cache
@login_required
@require_POST
def open_notification(request, notification_id):
    portal = _resolve_portal(request.user)
    notification = get_object_or_404(
        Notification.objects.select_related("account", "offer__account"),
        id=notification_id,
        recipient=request.user,
    )

    destination_url, _ = resolve_notification_destination(request.user, portal, notification)

    if notification.read_at is None:
        now = timezone.now()
        notification.read_at = now
        notification.save(update_fields=["read_at", "updated_at"])

    if destination_url:
        return redirect(destination_url)
    return redirect("notifications:inbox")


@never_cache
@login_required
@require_POST
def mark_read(request, notification_id):
    _resolve_portal(request.user)
    notification = get_object_or_404(Notification, id=notification_id, recipient=request.user)
    if notification.read_at is None:
        notification.read_at = timezone.now()
        notification.save(update_fields=["read_at", "updated_at"])

    next_url = request.POST.get("next")
    if next_url and next_url.startswith("/notifications/"):
        return redirect(next_url)
    return redirect("notifications:inbox")


@never_cache
@login_required
@require_POST
def mark_unread(request, notification_id):
    _resolve_portal(request.user)
    notification = get_object_or_404(Notification, id=notification_id, recipient=request.user)
    if notification.read_at is not None:
        notification.read_at = None
        notification.save(update_fields=["read_at", "updated_at"])

    next_url = request.POST.get("next")
    if next_url and next_url.startswith("/notifications/"):
        return redirect(next_url)
    return redirect("notifications:inbox")


@never_cache
@login_required
@require_POST
def mark_all_read(request):
    _resolve_portal(request.user)
    now = timezone.now()
    Notification.objects.filter(recipient=request.user, read_at__isnull=True).update(
        read_at=now, updated_at=now
    )

    next_url = request.POST.get("next")
    if next_url and next_url.startswith("/notifications/"):
        return redirect(next_url)
    return redirect("notifications:inbox")


@require_GET
@never_cache
def pwa_manifest(request):
    response = render(request, "pwa/manifest.webmanifest", content_type="application/manifest+json")
    response["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


@require_GET
@never_cache
def pwa_service_worker(request):
    response = render(request, "pwa/service-worker.js", content_type="application/javascript")
    response["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response["Service-Worker-Allowed"] = "/"
    return response


@require_GET
def pwa_offline(request):
    return render(request, "pwa/offline.html")
