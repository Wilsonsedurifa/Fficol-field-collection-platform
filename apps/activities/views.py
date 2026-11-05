import json
from decimal import Decimal
from functools import wraps

from django.http import JsonResponse
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Q, F
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from apps.assignments.services import OfferWorkflowError
from apps.assignments.views import _json_object, _invalid_constant
from .services import start_visit, submit_visit, cancel_visit, schedule_revisit, pending_followups, followup_day_end, followup_counts, followup_period, reviewable_visits, review_visit, can_review_visits, can_schedule_followups, schedulable_assignments, decide_followup, record_contact_attempt as record_contact_attempt_service
from apps.assignments.services import collector_profile
from apps.user.models import has_backoffice_access


def _backoffice_context(request, section, title):
    from apps.accounts.information.services import can_validate_information
    from apps.collection.services import (
        can_verify_payments,
        can_manage_earning_rules,
        can_manage_point_rules,
        can_review_commissions,
        can_manage_payouts,
    )
    from apps.assignments.services import can_review_releases, can_manage_assignments
    review = can_review_visits(request.user)
    schedule = can_schedule_followups(request.user)
    verify_payments = can_verify_payments(request.user)
    review_releases = can_review_releases(request.user)
    manage_assignments = can_manage_assignments(request.user)
    validate_information = can_validate_information(request.user)
    manage_earning_rules = can_manage_earning_rules(request.user)
    manage_point_rules = can_manage_point_rules(request.user)
    review_commissions = can_review_commissions(request.user)
    manage_payouts = can_manage_payouts(request.user)
    monitor_collectors = request.user.has_perm("user.view_collector_monitoring")
    if not (
        review
        or schedule
        or verify_payments
        or review_releases
        or manage_assignments
        or validate_information
        or manage_earning_rules
        or manage_point_rules
        or review_commissions
        or manage_payouts
        or monitor_collectors
    ):
        raise PermissionDenied("Back Office permission is required.")
    return {
        "can_review": review,
        "can_schedule": schedule,
        "can_verify_payments": verify_payments,
        "can_review_releases": review_releases,
        "can_manage_assignments": manage_assignments,
        "can_validate_information": validate_information,
        "can_manage_earning_rules": manage_earning_rules,
        "can_manage_point_rules": manage_point_rules,
        "can_review_commissions": review_commissions,
        "can_manage_payouts": manage_payouts,
        "can_monitor_collectors": monitor_collectors,
        "backoffice_section": section,
        "page_title": title,
    }


def _schedule_queries(user):
    from .models import FollowUpTask
    assignments=schedulable_assignments(user)
    tasks=FollowUpTask.objects.filter(assignment__in=assignments,kind="revisit",
        account_id=F("assignment__account_id"),assignee_id=F("assignment__collector__user_id"))
    requests=tasks.filter(status="requested").select_related("account__customer","assignee").order_by("created_at","pk")
    overdue=tasks.filter(status="pending",available_from__isnull=False,due_at__lt=timezone.now()).select_related("account","assignee").order_by("due_at","pk")
    return assignments,requests,overdue


def _dashboard_context(request):
    context=_backoffice_context(request,"dashboard","Dashboard")
    if context["can_review"]:
        context["pending_reports"]=reviewable_visits(request.user).filter(review_status="pending").count()
    if context["can_schedule"]:
        assignments,requests,overdue=_schedule_queries(request.user)
        context.update(followup_request_count=requests.count(),overdue_schedule_count=overdue.count())
    if context.get("can_verify_payments"):
        from apps.collection.services import reviewable_payments
        context["pending_payments"] = reviewable_payments(request.user).filter(status="reported").count()
    if context.get("can_review_releases"):
        from apps.assignments.services import reviewable_release_requests
        context["pending_releases"] = reviewable_release_requests(request.user).filter(status="requested").count()
    if context.get("can_review_commissions"):
        from apps.collection.services import reviewable_commissions
        context["pending_commissions"] = reviewable_commissions(request.user).filter(status="pending").count()
    if context.get("can_manage_payouts"):
        from apps.collection.services import reviewable_payouts
        context["draft_payouts"] = reviewable_payouts(request.user).filter(status="draft").count()
    return context


def _review_context(request):
    context=_backoffice_context(request,"visits","Visit Reviews")
    if not context["can_review"]:
        raise PermissionDenied("Visit review permission is required.")
    reports=reviewable_visits(request.user)
    state=request.GET.get("state","pending")
    if state not in {"pending","approved","rejected","legacy","all"}:state="pending"
    counts={label:reports.filter(review_status=value).count() for label,value in [("pending","pending"),("approved","approved"),("rejected","rejected"),("legacy","not_required")]}
    if state!="all":reports=reports.filter(review_status="not_required" if state=="legacy" else state)
    query=request.GET.get("q","").strip()[:100]
    if query:reports=reports.filter(Q(account__reference__icontains=query)|Q(account__customer__display_name__icontains=query)|Q(recorded_by__username__icontains=query))
    page=Paginator(reports,10).get_page(request.GET.get("page"))
    return {**context,"reports":page,"page_obj":page,"review_counts":counts,"review_state":state,"search_query":query}


def _scheduling_context(request):
    context=_backoffice_context(request,"followups","Follow-up Scheduling")
    if not context["can_schedule"]:
        raise PermissionDenied("Follow-up scheduling permission is required.")
    assignments,requests,overdue=_schedule_queries(request.user)
    context.update(schedule_assignments=Paginator(assignments,10).get_page(request.GET.get("schedule_page")),
        schedule_requests=Paginator(requests,10).get_page(request.GET.get("request_page")),
        overdue_schedules=Paginator(overdue,10).get_page(request.GET.get("overdue_page")))
    return context


@never_cache
@login_required
@require_GET
def review_dashboard(request):
    return render(request,"dashboard/supervisor.html",_dashboard_context(request))


@never_cache
@csrf_protect
@login_required
@require_POST
def review_visit_form(request,activity_id):
    context=_review_context(request)
    try:
        if request.content_type!="application/x-www-form-urlencoded" or len(request.body)>65536 or request.FILES or set(request.POST)!={"csrfmiddlewaretoken","decision","reason"} or any(len(request.POST.getlist(key))!=1 for key in request.POST):
            raise OfferWorkflowError("invalid_review","Use the report review form.")
        result=review_visit(request.user.pk,activity_id,request.POST.get("decision"),request.POST.get("reason"))
    except OfferWorkflowError as exc:
        context.update(review_error=exc.message,review_error_id=activity_id,entered_reason=request.POST.get("reason","")[:5000])
        return render(request,"backoffice/visits.html",context,status=exc.status)
    messages.success(request,"Report approved." if result["review_status"]=="approved" else "Report rejected. The reason is visible in visit history.")
    return redirect("backoffice_visits")


@never_cache
@login_required
@require_GET
def followups_page(request):
    if has_backoffice_access(request.user):
        raise PermissionDenied("Use the Back Office portal with this account.")
    try:
        profile=collector_profile(request.user.pk)
    except OfferWorkflowError as exc:
        raise PermissionDenied(exc.message)
    now=timezone.now()
    tasks=pending_followups(profile)
    from .models import FollowUpTask
    from apps.assignments.services import client_ids
    requests=FollowUpTask.objects.filter(assignee=request.user,assignment__collector=profile,assignment__status="active",account_id=F("assignment__account_id"),account__client_id__in=client_ids(profile),status__in=["requested","rejected"]).select_related("account").order_by("-created_at","-pk")[:20]
    counts=followup_counts(tasks,now)
    period=request.GET.get("period","all")
    if period not in {"all","overdue","today","upcoming"}:
        period="all"
    end=followup_day_end(now)
    if period=="overdue":tasks=tasks.filter(due_at__lt=now)
    elif period=="today":tasks=tasks.filter(due_at__gte=now,due_at__lt=end)
    elif period=="upcoming":tasks=tasks.filter(due_at__gte=end)
    query=request.GET.get("q","").strip()[:100]
    if query:
        tasks=tasks.filter(Q(account__reference__icontains=query)|Q(account__customer__display_name__icontains=query)|Q(assignment__target_address__address_line__icontains=query))
    page=Paginator(tasks,12).get_page(request.GET.get("page"))
    for task in page:task.schedule_period=followup_period(task,now)
    return render(request,"collector/followup.html",{
        "collector_profile":profile,"collector_section":"followups","tasks":page,"page_obj":page,
        "counts":counts,"follow_ups_connected":True,"follow_ups_total":counts["total"],
        "period":period,"search_query":query,"followup_requests":requests,
    })


def visit_post(service):
    @wraps(service)
    @never_cache
    @csrf_protect
    def view(request, **kwargs):
        if request.method != "POST":
            response = JsonResponse({"error": {"code": "method_not_allowed", "message": "Use POST."}}, status=405)
            response["Allow"] = "POST"
            return response
        if not request.user.is_authenticated:
            return JsonResponse({"error": {"code": "authentication_required", "message": "Please sign in again."}}, status=401)
        if has_backoffice_access(request.user):
            return JsonResponse({"error":{"code":"collector_portal_forbidden","message":"Use the Back Office portal with this account."}},status=403)
        if request.content_type != "application/json" or len(request.body) > 32768:
            return JsonResponse({"error": {"code": "invalid_payload", "message": "Submit a valid visit report within the size limit."}}, status=400)
        try:
            payload = json.loads(request.body.decode("utf-8"), parse_float=Decimal, parse_constant=_invalid_constant, object_pairs_hook=_json_object)
            return JsonResponse(service(request.user.pk, payload=payload, **kwargs))
        except (ValueError, UnicodeDecodeError, RecursionError):
            return JsonResponse({"error": {"code": "invalid_payload", "message": "Submit a valid visit report."}}, status=400)
        except OfferWorkflowError as exc:
            return JsonResponse({"error": {"code": exc.code, "message": exc.message}}, status=exc.status)
    return view


start = visit_post(start_visit)
submit = visit_post(submit_visit)
cancel = visit_post(cancel_visit)
schedule = visit_post(schedule_revisit)


@never_cache
@csrf_protect
def record_contact_attempt(request, assignment_id):
    if request.method != "POST":
        response = JsonResponse({"error": {"code": "method_not_allowed", "message": "Use POST."}}, status=405)
        response["Allow"] = "POST"
        return response
    if not request.user.is_authenticated:
        return JsonResponse({"error": {"code": "authentication_required", "message": "Please sign in again."}}, status=401)
    if has_backoffice_access(request.user):
        return JsonResponse({"error": {"code": "collector_portal_forbidden", "message": "Use the Back Office portal with this account."}}, status=403)
    if request.content_type != "application/json" or len(request.body) > 32768:
        return JsonResponse({"error": {"code": "invalid_payload", "message": "Submit a valid contact attempt within the size limit."}}, status=400)
    try:
        payload = json.loads(request.body.decode("utf-8"), parse_float=Decimal, parse_constant=_invalid_constant, object_pairs_hook=_json_object)
        return JsonResponse(record_contact_attempt_service(request.user.pk, assignment_id, payload))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return JsonResponse({"error": {"code": "invalid_payload", "message": "Submit a valid contact attempt."}}, status=400)
    except OfferWorkflowError as exc:
        return JsonResponse({"error": {"code": exc.code, "message": exc.message}}, status=exc.status)


@never_cache
@csrf_protect
@login_required
@require_POST
def schedule_followup_form(request, assignment_id):
    context=_scheduling_context(request)
    try:
        fields={"csrfmiddlewaretoken","decision","task_id","available_from","due_at","note"}
        if request.content_type!="application/x-www-form-urlencoded" or len(request.body)>65536 or request.FILES or set(request.POST)!=fields or any(len(request.POST.getlist(k))!=1 for k in request.POST):
            raise OfferWorkflowError("invalid_schedule","Use the follow-up scheduling form.")
        raw_id=request.POST.get("task_id","")
        if raw_id and (not raw_id.isascii() or not raw_id.isdigit() or len(raw_id)>18):
            raise OfferWorkflowError("invalid_schedule","Choose a valid request.")
        # datetime-local inputs represent Philippine time, regardless of device timezone.
        from .services import _datetime
        def local_time(value):
            return _datetime(value+"+08:00").isoformat()
        decision=request.POST.get("decision")
        start=local_time(request.POST.get("available_from","")) if decision=="authorize" else None
        end=local_time(request.POST.get("due_at","")) if decision=="authorize" else None
        result=decide_followup(request.user.pk,assignment_id,decision,start,end,request.POST.get("note",""),int(raw_id) if raw_id else None)
    except OfferWorkflowError as exc:
        context.update(review_error=exc.message)
        return render(request,"backoffice/followups.html",context,status=exc.status)
    messages.success(request,"Follow-up scheduled." if result["task"]["status"]=="pending" else "Follow-up request rejected. The note is visible to the collector.")
    return redirect("backoffice_followups")


@never_cache
@login_required
@require_GET
def backoffice_visits(request):
    return render(request,"backoffice/visits.html",_review_context(request))


@never_cache
@login_required
@require_GET
def backoffice_followups(request):
    return render(request,"backoffice/followups.html",_scheduling_context(request))


@never_cache
@csrf_protect
def offline_sync_visit(request):
    if request.method != "POST":
        response = JsonResponse({"error": {"code": "method_not_allowed", "message": "Use POST."}}, status=405)
        response["Allow"] = "POST"
        return response
    if not request.user.is_authenticated:
        return JsonResponse({"error": {"code": "authentication_required", "message": "Please sign in again."}}, status=401)
    if has_backoffice_access(request.user):
        return JsonResponse({"error": {"code": "collector_portal_forbidden", "message": "Use the Back Office portal with this account."}}, status=403)

    from .services import sync_offline_visit as sync_service

    content_type = request.content_type or ""
    photo_file = None
    if content_type.startswith("multipart/form-data"):
        photo_file = request.FILES.get("evidence_photo")
        promise_amount = request.POST.get("promise_amount")
        promise_date = request.POST.get("promise_date")
        promise = {"amount": promise_amount, "due_date": promise_date} if promise_amount and promise_date else None
        followup_date = request.POST.get("followup_date")
        follow_up = {"kind": "revisit", "due_at": followup_date} if followup_date else None

        payload = {
            "event_id": request.POST.get("event_id"),
            "assignment_id": request.POST.get("assignment_id"),
            "outcome": request.POST.get("outcome"),
            "notes": request.POST.get("notes", ""),
            "occurred_at": request.POST.get("occurred_at"),
            "ended_at": request.POST.get("ended_at"),
            "location_issue_reason": request.POST.get("location_issue_reason", ""),
            "latitude": request.POST.get("latitude") or None,
            "longitude": request.POST.get("longitude") or None,
            "accuracy_m": request.POST.get("accuracy_m") or None,
            "promise": promise,
            "follow_up": follow_up,
        }
        try:
            assignment_id = int(request.POST.get("assignment_id"))
        except (ValueError, TypeError):
            return JsonResponse({"error": {"code": "invalid_assignment", "message": "Valid assignment ID is required."}}, status=400)
    elif content_type == "application/json":
        try:
            body_data = json.loads(request.body.decode("utf-8"), parse_float=Decimal, parse_constant=_invalid_constant, object_pairs_hook=_json_object)
            payload = body_data
            assignment_id = int(payload.get("assignment_id"))
        except (ValueError, TypeError, UnicodeDecodeError):
            return JsonResponse({"error": {"code": "invalid_payload", "message": "Submit valid JSON."}}, status=400)
    else:
        return JsonResponse({"error": {"code": "invalid_content_type", "message": "Use multipart/form-data or application/json."}}, status=415)

    try:
        result = sync_service(request.user.pk, assignment_id, payload, photo_file=photo_file)
        return JsonResponse(result)
    except OfferWorkflowError as exc:
        return JsonResponse({"error": {"code": exc.code, "message": exc.message}}, status=exc.status)
