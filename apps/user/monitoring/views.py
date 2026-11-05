"""Read-only collector monitoring view."""
from django.contrib.auth.decorators import login_required, permission_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from .queries import attach_rows, ordered_collectors, scoped_collectors, workload
from .navigation import navigation


@never_cache
@login_required
@permission_required("user.view_collector_monitoring", raise_exception=True)
@require_GET
def collectors(request):
    """Show authorized-client collectors; filters also apply to summary cards."""
    from apps.activities.views import _backoffice_context
    profiles, clients = scoped_collectors(request.user)
    client = request.GET.get("client", "")
    if client.isdigit():
        clients = clients.filter(pk=int(client))
        profiles = profiles.filter(user__client_accesses__client__in=clients, user__client_accesses__is_active=True).distinct()
    query = request.GET.get("q", "").strip()[:100]
    if query:
        profiles = profiles.filter(Q(user__username__icontains=query) | Q(user__first_name__icontains=query) | Q(user__last_name__icontains=query))
    availability = request.GET.get("availability", "")
    if availability in CollectorAvailability:
        profiles = profiles.filter(availability=availability)
    approval = request.GET.get("approval", "")
    if approval in CollectorApproval:
        profiles = profiles.filter(approval_status=approval)
    assignments = workload(clients)
    overdue = assignments.filter(Q(first_overdue=True) | Q(followup_overdue=True))
    if request.GET.get("overdue") == "1":
        profiles = profiles.filter(pk__in=overdue.values("collector_id"))
    summary = {
        "approved": profiles.filter(approval_status="approved").count(),
        "online": profiles.filter(availability="online").count(),
        "busy": profiles.filter(availability="busy").count(),
        "offline": profiles.filter(availability="offline").count(),
        "working": profiles.filter(pk__in=assignments.values("collector_id")).count(),
        "overdue": overdue.filter(collector__in=profiles).count(),
    }
    sort, direction, card_links, sort_links, pagination_query = navigation(request.GET)
    profiles = ordered_collectors(profiles, assignments, clients, sort, direction)
    page = Paginator(profiles, 20).get_page(request.GET.get("page"))
    context = _backoffice_context(request, "collectors", "Collector Monitoring")
    context.update(rows=attach_rows(page.object_list, assignments, clients), page_obj=page, summary=summary,
                   clients=scoped_collectors(request.user)[1], filters=request.GET, query=query,
                   pagination_query=pagination_query, sort=sort, direction=direction,
                   card_links=card_links, sort_links=sort_links)
    return render(request, "backoffice/collectors.html", context)


CollectorAvailability = {"online", "busy", "offline"}
CollectorApproval = {"pending", "approved", "rejected", "suspended"}
