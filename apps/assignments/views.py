import json
from decimal import Decimal

from django.http import JsonResponse
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect

from .services import LocationSubmissionError, submit_collector_location
from apps.user.models import has_backoffice_access


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field.")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("Non-finite JSON number.")


@never_cache
@csrf_protect
def submit_location(request):
    """POST JSON with longitude, latitude, accuracy_m and ISO captured_at."""
    if request.method != "POST":
        response = JsonResponse({"error": {"code": "method_not_allowed", "message": "Use POST to submit a location."}}, status=405)
        response["Allow"] = "POST"
        return response
    if not request.user.is_authenticated:
        return JsonResponse({"error": {"code": "authentication_required", "message": "Please sign in."}}, status=401)
    if not request.user.is_active:
        return JsonResponse({"error": {"code": "collector_required", "message": "An active collector account is required."}}, status=403)
    if has_backoffice_access(request.user):
        return JsonResponse({"error":{"code":"collector_portal_forbidden","message":"Use the Back Office portal with this account."}},status=403)
    if request.content_type != "application/json":
        return JsonResponse({"error": {"code": "unsupported_media_type", "message": "Submit the location as JSON."}}, status=415)
    if len(request.body) > 4096:
        return JsonResponse({"error": {"code": "payload_too_large", "message": "Location submission is too large."}}, status=413)
    try:
        payload = json.loads(
            request.body.decode("utf-8"), parse_float=Decimal,
            parse_constant=_invalid_constant, object_pairs_hook=_json_object,
        )
    except (ValueError, UnicodeDecodeError, RecursionError):
        return JsonResponse({"error": {"code": "invalid_json", "message": "Submit a valid JSON location."}}, status=400)
    try:
        result = submit_collector_location(request.user.pk, payload)
    except LocationSubmissionError as exc:
        return JsonResponse({"error": {"code": exc.code, "message": exc.message}}, status=exc.status)
    return JsonResponse(result)


from functools import wraps
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST
import uuid
from .models import AccountAssignment, AssignmentReleaseRequest
from .services import (OfferWorkflowError, assignment_payload, change_availability,
                       client_ids, collector_profile, offers_state, refresh_offers, respond_to_offer, acceptance_allowance,
                       request_assignment_release, decide_release_request, reviewable_release_requests, can_review_releases,
                       can_manage_assignments, compute_ownership_token, assignable_accounts,
                       eligible_collectors_for_account, assign_or_reassign_account)


def _page_context(request, section):
    if has_backoffice_access(request.user):
        raise PermissionDenied("Use the Back Office portal with this account.")
    try:
        profile = collector_profile(request.user.pk)
    except OfferWorkflowError as exc:
        raise PermissionDenied(exc.message)
    return {"collector_profile": profile, "collector_section": section,"daily_acceptance":acceptance_allowance(profile)}


def _map_options():
    return {"tileUrl": settings.MAP_TILE_URL, "attribution": settings.MAP_ATTRIBUTION}


@never_cache
@login_required
@require_GET
def account_offers_page(request):
    context = _page_context(request, "offers")
    context["map_config"] = {
        **_map_options(), "listUrl": reverse("assignments:offers_list"),
        "refreshUrl": reverse("assignments:offers_refresh"),
        "availabilityUrl": reverse("assignments:availability"),
        "locationUrl": reverse("assignments:submit_location"),
    }
    return render(request, "collector/account_offers.html", context)


@never_cache
@login_required
@require_GET
def my_accounts_page(request):
    context = _page_context(request, "accounts")
    work = AccountAssignment.objects.filter(
        collector=context["collector_profile"], status="active", account__client_id__in=client_ids(context["collector_profile"]),
    ).select_related("account__customer", "account__client", "target_address").order_by("first_action_due_at", "pk")
    context["assigned_count"] = work.count()
    query = request.GET.get("q", "").strip()[:100]
    if query:
        work = work.filter(
            Q(account__reference__icontains=query)
            | Q(account__customer__display_name__icontains=query)
            | Q(target_address__address_line__icontains=query)
        )
    page = Paginator(work, 12).get_page(request.GET.get("page"))
    context.update(work=page, page_obj=page, search_query=query)
    return render(request, "collector/my_accounts.html", context)


@never_cache
@login_required
@require_GET
def offline_pack_api(request):
    if has_backoffice_access(request.user):
        return JsonResponse({"error": {"code": "collector_portal_forbidden", "message": "Use the Back Office portal with this account."}}, status=403)
    try:
        profile = collector_profile(request.user.pk)
    except OfferWorkflowError as exc:
        return JsonResponse({"error": {"code": exc.code, "message": exc.message}}, status=exc.status)

    from django.utils import timezone
    from apps.accounts.information.models import CustomerContact
    from apps.activities.models import FieldActivity
    from apps.activities.services import visit_gate

    now = timezone.now()
    assignments = (
        AccountAssignment.objects.filter(
            collector=profile,
            status="active",
            account__client_id__in=client_ids(profile),
            account__status="open",
        )
        .select_related("account__customer", "account__client", "target_address")
        .order_by("first_action_due_at", "pk")
    )

    customer_ids = [item.account.customer_id for item in assignments]
    contacts = CustomerContact.objects.filter(customer_id__in=customer_ids, is_active=True).values("customer_id", "kind", "value")
    contacts_by_customer = {}
    for c in contacts:
        contacts_by_customer.setdefault(c["customer_id"], []).append({"kind": c["kind"], "value": c["value"]})

    assignment_ids = [item.pk for item in assignments]
    latest_visits = (
        FieldActivity.objects.filter(assignment_id__in=assignment_ids, recorded_by=request.user, kind="visit")
        .exclude(status="in_progress")
        .order_by("assignment_id", "-occurred_at", "-pk")
    )
    latest_visit_by_assignment = {}
    for v in latest_visits:
        if v.assignment_id not in latest_visit_by_assignment:
            latest_visit_by_assignment[v.assignment_id] = {
                "outcome": v.outcome,
                "occurred_at": v.occurred_at.isoformat() if v.occurred_at else None,
                "notes": v.notes,
                "status": v.status,
            }

    accounts_data = []
    for item in assignments:
        acc = item.account
        cust = acc.customer
        gate = visit_gate(item, request.user.pk, now)
        lat = item.target_position.y if item.target_position else None
        lng = item.target_position.x if item.target_position else None
        latest_v = latest_visit_by_assignment.get(item.pk)

        accounts_data.append({
            "assignment_id": item.pk,
            "account_id": acc.pk,
            "reference": acc.reference,
            "customer_name": cust.display_name if cust else "Unknown",
            "client_name": acc.client.name,
            "balance": str(acc.balance_snapshot),
            "currency": acc.currency,
            "address": item.target_address.address_line if item.target_address else "No address specified",
            "latitude": lat,
            "longitude": lng,
            "contacts": contacts_by_customer.get(acc.customer_id, []),
            "first_action_due_at": item.first_action_due_at.isoformat() if item.first_action_due_at else None,
            "next_action_due_at": item.next_action_due_at.isoformat() if item.next_action_due_at else None,
            "visit_gate": gate,
            "latest_visit": latest_v,
            "local_status": latest_v["outcome"] if latest_v else "pending",
        })

    return JsonResponse({
        "status": "ok",
        "synced_at": now.isoformat(),
        "total_accounts": len(accounts_data),
        "accounts": accounts_data,
    })


@never_cache
@login_required
@require_GET
def account_detail_page(request, assignment_id):
    context = _page_context(request, "accounts")
    assignment = AccountAssignment.objects.filter(pk=assignment_id, collector=context["collector_profile"],
        account__client_id__in=client_ids(context["collector_profile"])).select_related("account__customer", "account__client", "target_address").first()
    if assignment is None:
        from django.http import Http404
        raise Http404("Assigned account not found.")
    context["assignment"] = assignment
    context["detail"] = assignment_payload(assignment)
    if assignment.account.status != assignment.account.Status.OPEN:
        return render(request, "collector/account_readonly.html", context)
    if assignment.status != assignment.Status.ACTIVE:
        from django.http import Http404
        raise Http404("Assigned account not found.")
    context["map_config"] = {**_map_options(), "latitude": assignment.target_position.y, "longitude": assignment.target_position.x}
    from apps.activities.models import FieldActivity
    from apps.activities.services import activity_payload, visit_gate
    from django.utils import timezone
    visits = FieldActivity.objects.filter(assignment=assignment, account_id=assignment.account_id, recorded_by=request.user, kind="visit").order_by("-occurred_at", "-pk")
    ongoing = visits.filter(status="in_progress").first()
    context["visit_history"] = visits.exclude(status="in_progress").prefetch_related("promises", "follow_up_tasks")[:20]
    context["pending_followups"] = assignment.follow_up_tasks.filter(account_id=assignment.account_id, assignee=request.user, status__in=["requested","pending","rejected"]).order_by("due_at", "pk")[:20]
    context["visit_outcomes"] = FieldActivity.Outcome.choices
    gate=visit_gate(assignment,request.user.pk,timezone.now())
    context["visit_gate"]=gate
    from django.utils.dateparse import parse_datetime
    context["revisit_due_at"]=parse_datetime(gate["next_due_at"]) if gate["next_due_at"] else None
    context["visit_config"] = {
        "assignment_id": assignment.pk,
        "startUrl": reverse("activities:start_visit", args=[assignment.pk]),
        "submitUrl": reverse("activities:submit_visit", args=[assignment.pk, 0]),
        "cancelUrl": reverse("activities:cancel_visit", args=[assignment.pk, 0]),
        "ongoing": activity_payload(ongoing) if ongoing else None,
        "gate":gate,
        "scheduleUrl":reverse("activities:schedule_revisit",args=[assignment.pk,0]),
    }
    from apps.collection.models import Payment
    context["payment_history"] = Payment.objects.filter(assignment=assignment).prefetch_related("documents").order_by("-paid_at", "-pk")[:20]
    context["has_pending_payment_report"] = Payment.objects.filter(
        assignment=assignment,
        reported_by=request.user,
        status=Payment.Status.REPORTED,
    ).exists()
    context["payment_methods"] = Payment.Method.choices
    context["payment_report_url"] = reverse("collection:report_payment", args=[assignment.pk])
    release_requests = assignment.release_requests.order_by("-created_at", "-pk")
    context["release_requests"] = release_requests
    context["pending_release_request"] = release_requests.filter(status="requested").first()
    context["allow_collector_release"] = assignment.account.client.allow_collector_release
    context["can_request_release"] = assignment.account.client.allow_collector_release and context["pending_release_request"] is None
    context["release_history"] = release_requests.exclude(status="requested")[:10]
    context["release_event_id"] = str(uuid.uuid4())
    context["release_request_url"] = reverse("assignments:request_release", args=[assignment.pk])
    context["contact_attempt_url"] = reverse("activities:record_contact_attempt", args=[assignment.pk])
    context["contact_attempt_methods"] = [
        ("phone", "Call"),
        ("sms", "SMS"),
        ("email", "Email"),
    ]
    context["contact_attempt_outcomes"] = [
        ("contacted", "Customer contacted"),
        ("unavailable", "Customer unavailable"),
        ("disputed", "Customer disputed the account"),
        ("other", "Other"),
    ]
    context["contact_attempt_history"] = FieldActivity.objects.filter(
        assignment=assignment,
        account_id=assignment.account_id,
        recorded_by=request.user,
        kind__in=["phone", "sms", "email"],
        status="submitted",
    ).order_by("-occurred_at", "-pk")[:20]
    return render(request, "collector/account_detail.html", context)


def _api(method):
    def decorate(view):
        @wraps(view)
        @never_cache
        @csrf_protect
        def wrapped(request, *args, **kwargs):
            if request.method != method:
                response = JsonResponse({"error": {"code": "method_not_allowed", "message": f"Use {method}."}}, status=405)
                response["Allow"] = method
                return response
            if not request.user.is_authenticated:
                return JsonResponse({"error": {"code": "authentication_required", "message": "Please sign in."}}, status=401)
            if has_backoffice_access(request.user):
                return JsonResponse({"error":{"code":"collector_portal_forbidden","message":"Use the Back Office portal with this account."}},status=403)
            try:
                return JsonResponse(view(request, *args, **kwargs))
            except OfferWorkflowError as exc:
                return JsonResponse({"error": {"code": exc.code, "message": exc.message}}, status=exc.status)
        return wrapped
    return decorate


@_api("GET")
def offers_list(request):
    return offers_state(request.user.pk)


@_api("POST")
def offers_refresh(request):
    return refresh_offers(request.user.pk)


@_api("POST")
def availability(request):
    if request.content_type != "application/json" or len(request.body) > 512:
        raise OfferWorkflowError("invalid_availability", "Submit an online or offline availability choice.")
    try:
        payload = json.loads(request.body.decode("utf-8"), object_pairs_hook=_json_object)
    except (ValueError, UnicodeDecodeError):
        raise OfferWorkflowError("invalid_availability", "Submit an online or offline availability choice.")
    if not isinstance(payload, dict) or set(payload) != {"availability"}:
        raise OfferWorkflowError("invalid_availability", "Provide availability only.")
    return change_availability(request.user.pk, payload["availability"])


@_api("POST")
def accept_offer(request, offer_id):
    return respond_to_offer(request.user.pk, offer_id, "accept")


@_api("POST")
def decline_offer(request, offer_id):
    return respond_to_offer(request.user.pk, offer_id, "decline")


@never_cache
@csrf_protect
@login_required
@require_POST
def request_release_form(request, assignment_id):
    if has_backoffice_access(request.user):
        raise PermissionDenied("Use the Back Office portal with this account.")
    try:
        if request.content_type != "application/x-www-form-urlencoded" or len(request.body) > 65536 or request.FILES:
            raise OfferWorkflowError("invalid_request", "Use the release request form.")
        payload = {
            "event_id": request.POST.get("event_id"),
            "reason": request.POST.get("reason"),
        }
        res = request_assignment_release(request.user.pk, assignment_id, payload)
        if res.get("created"):
            messages.success(request, "Release request submitted and awaiting Back Office review.")
        else:
            req_status = res["request"].status
            if req_status == "approved":
                messages.info(request, "This assignment has already been released.")
                return redirect("assignments:my_accounts")
            elif req_status == "rejected":
                messages.warning(request, "Release request was already reviewed and rejected.")
            elif req_status == "superseded":
                messages.info(request, "This release request was superseded by account reassignment.")
                return redirect("assignments:my_accounts")
            else:
                messages.info(request, "Release request already submitted and awaiting Back Office review.")
    except OfferWorkflowError as exc:
        messages.error(request, exc.message)
    return redirect("assignments:account_detail", assignment_id=assignment_id)


def _release_context(request):
    from apps.activities.views import _backoffice_context
    context = _backoffice_context(request, "releases", "Release Requests")
    if not context["can_review_releases"]:
        raise PermissionDenied("Release request review permission is required.")
    base = reviewable_release_requests(request.user)
    state = request.GET.get("state", "pending")
    if state not in {"pending", "approved", "rejected", "superseded", "all"}:
        state = "pending"

    counts = {
        "pending": base.filter(status="requested").count(),
        "approved": base.filter(status="approved").count(),
        "rejected": base.filter(status="rejected").count(),
        "superseded": base.filter(status="superseded").count(),
    }

    if state == "pending":
        reqs = base.filter(status="requested")
    elif state == "approved":
        reqs = base.filter(status="approved").order_by("-reviewed_at", "-pk")
    elif state == "rejected":
        reqs = base.filter(status="rejected").order_by("-reviewed_at", "-pk")
    elif state == "superseded":
        reqs = base.filter(status="superseded").order_by("-reviewed_at", "-pk")
    else:
        reqs = base.order_by("-created_at", "-pk")

    query = request.GET.get("q", "").strip()[:100]
    if query:
        reqs = reqs.filter(
            Q(assignment__account__reference__icontains=query)
            | Q(assignment__account__customer__display_name__icontains=query)
            | Q(requested_by__username__icontains=query)
            | Q(reason__icontains=query)
        )

    page = Paginator(reqs, 10).get_page(request.GET.get("page"))
    return {
        **context,
        "release_requests": page,
        "page_obj": page,
        "release_counts": counts,
        "release_state": state,
        "search_query": query,
    }


@never_cache
@login_required
@require_GET
def backoffice_release_requests(request):
    context = _release_context(request)
    return render(request, "backoffice/releases.html", context)


@never_cache
@csrf_protect
@login_required
@require_POST
def decide_release_request_form(request, request_id):
    context = _release_context(request)
    try:
        if (
            request.content_type != "application/x-www-form-urlencoded"
            or len(request.body) > 65536
            or request.FILES
            or set(request.POST) != {"csrfmiddlewaretoken", "decision", "reason"}
            or any(len(request.POST.getlist(key)) != 1 for key in request.POST)
        ):
            raise OfferWorkflowError("invalid_decision", "Use the release review form.")
        decision = request.POST.get("decision")
        reason = request.POST.get("reason", "")
        result = decide_release_request(request.user.pk, request_id, decision, reason)
    except OfferWorkflowError as exc:
        context.update(
            review_error=exc.message,
            review_error_id=request_id,
            entered_reason=request.POST.get("reason", "")[:5000],
        )
        return render(request, "backoffice/releases.html", context, status=exc.status)

    messages.success(
        request,
        "Release request approved. Assignment released." if result["status"] == "approved" else "Release request rejected."
    )
    state = request.GET.get("state", "pending")
    q = request.GET.get("q", "")
    page = request.GET.get("page", "")
    params = []
    if state:
        params.append(f"state={state}")
    if q:
        params.append(f"q={q}")
    if page:
        params.append(f"page={page}")
    query_string = f"?{'&'.join(params)}" if params else ""
    return redirect(f"{reverse('backoffice_release_requests')}{query_string}")


def _assignment_context(request):
    from apps.activities.views import _backoffice_context
    context = _backoffice_context(request, "assignments", "Account Assignments")
    if not context.get("can_manage_assignments"):
        raise PermissionDenied("Account assignment management permission is required.")
    return context


@never_cache
@login_required
@require_GET
def backoffice_assignments(request):
    context = _assignment_context(request)
    state = request.GET.get("state", "unassigned")
    if state not in {"unassigned", "assigned", "all"}:
        state = "unassigned"

    query = request.GET.get("q", "").strip()[:100]
    accounts = assignable_accounts(request.user, query=query, state=state)

    all_accounts = assignable_accounts(request.user, query=None, state="all")
    counts = {
        "unassigned": all_accounts.exclude(assignments__status="active").count(),
        "assigned": all_accounts.filter(assignments__status="active").count(),
        "all": all_accounts.count(),
    }

    page = Paginator(accounts, 15).get_page(request.GET.get("page"))
    context.update(
        accounts=page,
        page_obj=page,
        assignment_state=state,
        assignment_counts=counts,
        search_query=query,
    )
    return render(request, "backoffice/assignments.html", context)


@never_cache
@login_required
@require_GET
def backoffice_account_assignment_detail(request, account_id):
    context = _assignment_context(request)
    from apps.accounts.models import Account
    from apps.user.models import ClientAccess
    client_ids_list = list(ClientAccess.objects.filter(user_id=request.user.pk, is_active=True, client__is_active=True).values_list("client_id", flat=True))
    account = Account.objects.filter(pk=account_id, client_id__in=client_ids_list).select_related("client", "customer", "collection_address", "commission_rule").first()
    if account is None:
        from django.http import Http404
        raise Http404("Account not found.")

    from apps.activities.models import FieldActivity
    from apps.accounts.status.constants import ALLOWED_STATUS_TRANSITIONS
    from apps.collection.services import has_unverified_payments
    current_assignment = account.assignments.filter(status="active").select_related("collector__user", "target_address").first()
    assignment_history = account.assignments.exclude(status="active").select_related("collector__user").order_by("-created_at", "-pk")
    ownership_token = compute_ownership_token(account)
    event_id = str(uuid.uuid4())
    collectors = eligible_collectors_for_account(account, request.user)
    has_ongoing_visit = FieldActivity.objects.filter(account=account, status="in_progress", kind="visit").exists()
    status_choices_map = dict(Account.Status.choices)
    allowed_status_targets = [
        (s, status_choices_map.get(s, s))
        for s in ALLOWED_STATUS_TRANSITIONS.get(account.status, frozenset())
    ]
    status_history = account.status_changes.select_related("changed_by").order_by("-created_at", "-pk")
    status_event_id = str(uuid.uuid4())
    account_has_unverified_payments = has_unverified_payments(account)
    can_change_account_status = request.user.has_perm("accounts.change_account_status")

    context.update(
        account=account,
        current_assignment=current_assignment,
        assignment_history=assignment_history,
        ownership_token=ownership_token,
        event_id=event_id,
        collectors=collectors,
        has_ongoing_visit=has_ongoing_visit,
        allowed_status_targets=allowed_status_targets,
        status_history=status_history,
        status_event_id=status_event_id,
        account_has_unverified_payments=account_has_unverified_payments,
        can_change_account_status=can_change_account_status,
    )
    return render(request, "backoffice/assignment_detail.html", context)


@never_cache
@csrf_protect
@login_required
@require_POST
def assign_account_form(request, account_id):
    context = _assignment_context(request)
    from apps.accounts.models import Account
    from apps.user.models import ClientAccess
    client_ids_list = list(ClientAccess.objects.filter(user_id=request.user.pk, is_active=True, client__is_active=True).values_list("client_id", flat=True))
    account = Account.objects.filter(pk=account_id, client_id__in=client_ids_list).select_related("client", "customer", "collection_address", "commission_rule").first()
    if account is None:
        from django.http import Http404
        raise Http404("Account not found.")

    try:
        if (
            request.content_type != "application/x-www-form-urlencoded"
            or len(request.body) > 65536
            or request.FILES
            or set(request.POST) != {"csrfmiddlewaretoken", "collector_id", "reason", "event_id", "ownership_token"}
            or any(len(request.POST.getlist(key)) != 1 for key in request.POST)
        ):
            raise OfferWorkflowError("invalid_request", "Use the account assignment form.")

        collector_id = request.POST.get("collector_id")
        reason = request.POST.get("reason", "")
        event_id = request.POST.get("event_id")
        submitted_ownership_token = request.POST.get("ownership_token")

        result = assign_or_reassign_account(
            staff_user_id=request.user.pk,
            account_id=account_id,
            collector_id=collector_id,
            reason=reason,
            event_id=event_id,
            submitted_ownership_token=submitted_ownership_token,
        )

        if not result.get("created"):
            messages.info(request, "Account assignment was already completed with this submission.")
        elif result.get("reassigned"):
            messages.success(request, "Account successfully reassigned to new collector.")
        else:
            messages.success(request, "Account successfully assigned.")

        return redirect("backoffice_account_assignment_detail", account_id=account_id)

    except OfferWorkflowError as exc:
        from apps.activities.models import FieldActivity
        current_assignment = account.assignments.filter(status="active").select_related("collector__user", "target_address").first()
        assignment_history = account.assignments.exclude(status="active").select_related("collector__user").order_by("-created_at", "-pk")
        collectors = eligible_collectors_for_account(account, request.user)
        has_ongoing_visit = FieldActivity.objects.filter(account=account, status="in_progress", kind="visit").exists()
        context.update(
            account=account,
            current_assignment=current_assignment,
            assignment_history=assignment_history,
            collectors=collectors,
            ownership_token=request.POST.get("ownership_token", ""),
            event_id=request.POST.get("event_id", str(uuid.uuid4())),
            entered_collector_id=request.POST.get("collector_id", ""),
            entered_reason=request.POST.get("reason", "")[:5000],
            assignment_error=exc.message,
            has_ongoing_visit=has_ongoing_visit,
        )
        messages.error(request, exc.message)
        return render(request, "backoffice/assignment_detail.html", context, status=exc.status)


