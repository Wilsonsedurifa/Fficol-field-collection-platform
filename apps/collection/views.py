import json
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.models import Client
from apps.activities.views import _backoffice_context
from apps.user.models import ClientAccess, CollectorProfile, has_backoffice_access

from .models import Commission, InformationCategory, InformationEarningRule, Payout, PayoutItem, PointRule, PointTransaction
from .services import (
    CollectionWorkflowError,
    can_verify_payments,
    review_payment,
    reviewable_payments,
    report_payment,
    stream_document_response,
    can_manage_earning_rules,
    can_review_commissions,
    can_manage_payouts,
    can_manage_point_rules,
    publish_earning_rule,
    publish_point_rule,
    review_commission,
    create_payout,
    approve_payout,
    mark_payout_paid,
    reviewable_earning_rules,
    reviewable_point_rules,
    reviewable_commissions,
    reviewable_payouts,
    collector_earnings_summary,
    collector_commissions_list,
    collector_payouts_list,
    collector_points_summary,
    collector_point_transactions_list,
)


@never_cache
@csrf_protect
@login_required
@require_POST
def report_payment_api(request, assignment_id):
    if not request.user.is_authenticated:
        return JsonResponse({"error": {"code": "authentication_required", "message": "Please sign in."}}, status=401)
    if has_backoffice_access(request.user):
        return JsonResponse({"error": {"code": "collector_portal_forbidden", "message": "Use the Back Office portal with this account."}}, status=403)

    content_type = request.content_type or ""
    if not content_type.startswith("multipart/form-data"):
        return JsonResponse({"error": {"code": "invalid_content_type", "message": "Use multipart/form-data for payment and proof submission."}}, status=415)

    payload = {
        "event_id": request.POST.get("event_id"),
        "amount": request.POST.get("amount"),
        "paid_at": request.POST.get("paid_at"),
        "method": request.POST.get("method"),
        "reference": request.POST.get("reference", ""),
        "notes": request.POST.get("notes", ""),
    }
    if request.POST.get("promise_id"):
        payload["promise_id"] = request.POST.get("promise_id")
    if request.POST.get("activity_id"):
        payload["activity_id"] = request.POST.get("activity_id")

    proof_file = request.FILES.get("proof_file")

    try:
        result = report_payment(request.user.pk, assignment_id, payload, proof_file)
        status_code = 201 if result.get("created") else 200
        return JsonResponse(result, status=status_code)
    except CollectionWorkflowError as exc:
        return JsonResponse({"error": {"code": exc.code, "message": exc.message}}, status=exc.status)


def _payment_context(request):
    context = _backoffice_context(request, "payments", "Payment Verifications")
    if not can_verify_payments(request.user):
        raise PermissionDenied("Payment verification permission is required.")

    payments = reviewable_payments(request.user)
    state = request.GET.get("state", "pending")
    if state not in {"pending", "verified", "rejected", "all"}:
        state = "pending"

    from .models import Payment
    from apps.user.models import ClientAccess

    clients = ClientAccess.objects.filter(
        user_id=request.user.pk, is_active=True, client__is_active=True
    ).values_list("client_id", flat=True)

    base = Payment.objects.filter(account__client_id__in=clients)
    counts = {
        "pending": base.filter(status="reported").count(),
        "verified": base.filter(status="verified").count(),
        "rejected": base.filter(status="rejected").count(),
    }

    if state == "pending":
        payments = payments.filter(status="reported")
    elif state == "verified":
        payments = base.filter(status="verified").select_related(
            "account__customer", "account__client", "reported_by", "verified_by"
        ).prefetch_related("documents").order_by("-verified_at", "-pk")
    elif state == "rejected":
        payments = base.filter(status="rejected").select_related(
            "account__customer", "account__client", "reported_by", "verified_by"
        ).prefetch_related("documents").order_by("-verified_at", "-pk")

    query = request.GET.get("q", "").strip()[:100]
    if query:
        payments = payments.filter(
            Q(account__reference__icontains=query)
            | Q(account__customer__display_name__icontains=query)
            | Q(reported_by__username__icontains=query)
            | Q(reference__icontains=query)
        )

    page = Paginator(payments, 10).get_page(request.GET.get("page"))
    return {
        **context,
        "payments": page,
        "page_obj": page,
        "payment_counts": counts,
        "payment_state": state,
        "search_query": query,
    }


@never_cache
@login_required
@require_GET
def backoffice_payments(request):
    context = _payment_context(request)
    return render(request, "backoffice/payments.html", context)


@never_cache
@csrf_protect
@login_required
@require_POST
def review_payment_form(request, payment_id):
    context = _payment_context(request)
    try:
        if (
            request.content_type != "application/x-www-form-urlencoded"
            or len(request.body) > 65536
            or request.FILES
            or set(request.POST) != {"csrfmiddlewaretoken", "decision", "reason"}
            or any(len(request.POST.getlist(key)) != 1 for key in request.POST)
        ):
            raise CollectionWorkflowError("invalid_review", "Use the payment verification form.")
        result = review_payment(
            request.user.pk, payment_id, request.POST.get("decision"), request.POST.get("reason")
        )
    except CollectionWorkflowError as exc:
        context.update(
            review_error=exc.message,
            review_error_id=payment_id,
            entered_reason=request.POST.get("reason", "")[:5000],
        )
        return render(request, "backoffice/payments.html", context, status=exc.status)

    messages.success(
        request,
        "Payment verified." if result["status"] == "verified" else "Payment rejected. The reason has been recorded."
    )
    return redirect("backoffice_payments")


@never_cache
@login_required
@require_GET
def download_document(request, document_id):
    return stream_document_response(request.user, document_id)


# ---------------------------------------------------------------------------
# Earning Rules Views
# ---------------------------------------------------------------------------

def _earning_rules_context(request):
    context = _backoffice_context(request, "earning_rules", "Information Earning Rates")
    if not can_manage_earning_rules(request.user):
        raise PermissionDenied("Earning rule management permission is required.")

    rules = reviewable_earning_rules(request.user)
    if request.user.is_superuser:
        clients = Client.objects.filter(is_active=True).order_by("name")
    else:
        client_ids = ClientAccess.objects.filter(
            user=request.user, is_active=True, client__is_active=True
        ).values_list("client_id", flat=True)
        clients = Client.objects.filter(pk__in=client_ids).order_by("name")

    selected_client = request.GET.get("client", "")
    if selected_client.isdigit():
        rules = rules.filter(client_id=int(selected_client))

    categories = InformationCategory.choices
    page = Paginator(rules, 15).get_page(request.GET.get("page"))
    return {
        **context,
        "rules": page,
        "page_obj": page,
        "clients": clients,
        "selected_client": selected_client,
        "categories": categories,
        "can_manage_point_rules": can_manage_point_rules(request.user),
    }


@never_cache
@login_required
def backoffice_earning_rules(request):
    if request.method == "POST":
        context = _earning_rules_context(request)
        try:
            client_id = request.POST.get("client_id")
            category = request.POST.get("category")
            amount = request.POST.get("amount")
            publish_earning_rule(request.user, client_id=client_id, category=category, amount=amount)
            messages.success(request, "Information earning rate published successfully.")
            return redirect("backoffice_earning_rules")
        except CollectionWorkflowError as exc:
            context.update(form_error=exc.message)
            return render(request, "backoffice/earning_rules.html", context, status=exc.status)

    context = _earning_rules_context(request)
    return render(request, "backoffice/earning_rules.html", context)


# ---------------------------------------------------------------------------
# Point Rules Views
# ---------------------------------------------------------------------------

def _point_rules_context(request):
    if not can_manage_point_rules(request.user):
        raise PermissionDenied("Point rule management permission is required.")

    try:
        base_context = _backoffice_context(request, "point_rules", "Point Rules")
    except PermissionDenied:
        base_context = {
            "backoffice_section": "point_rules",
            "page_title": "Point Rules",
        }

    rules = reviewable_point_rules(request.user)
    categories = InformationCategory.choices
    page = Paginator(rules, 15).get_page(request.GET.get("page"))

    return {
        **base_context,
        "rules": page,
        "page_obj": page,
        "categories": categories,
        "can_manage_point_rules": True,
    }


@never_cache
@login_required
def backoffice_point_rules(request):
    if request.method == "POST":
        context = _point_rules_context(request)
        try:
            category = request.POST.get("category")
            publish_point_rule(request.user, category=category, points=1)
            messages.success(request, "Point rule version published successfully.")
            return redirect("backoffice_point_rules")
        except CollectionWorkflowError as exc:
            context.update(form_error=exc.message)
            return render(request, "backoffice/point_rules.html", context, status=exc.status)

    context = _point_rules_context(request)
    return render(request, "backoffice/point_rules.html", context)


# ---------------------------------------------------------------------------
# Commissions Views
# ---------------------------------------------------------------------------

def _commissions_context(request):
    context = _backoffice_context(request, "commissions", "Commissions Review")
    if not can_review_commissions(request.user):
        raise PermissionDenied("Commission review permission is required.")

    base = reviewable_commissions(request.user)
    counts = {
        "pending": base.filter(status=Commission.Status.PENDING).count(),
        "approved": base.filter(status=Commission.Status.APPROVED).count(),
        "rejected": base.filter(status=Commission.Status.REJECTED).count(),
        "paid": base.filter(status=Commission.Status.PAID).count(),
    }

    state = request.GET.get("state", "pending")
    if state not in {"pending", "approved", "rejected", "paid", "all"}:
        state = "pending"

    commissions = base
    if state != "all":
        commissions = commissions.filter(status=state)

    query = request.GET.get("q", "").strip()[:100]
    if query:
        commissions = commissions.filter(
            Q(proposal__account__reference__icontains=query)
            | Q(collector__user__username__icontains=query)
            | Q(client__code__icontains=query)
            | Q(category__icontains=query)
        )

    page = Paginator(commissions, 10).get_page(request.GET.get("page"))
    return {
        **context,
        "commissions": page,
        "page_obj": page,
        "commission_counts": counts,
        "commission_state": state,
        "search_query": query,
        "can_manage_point_rules": can_manage_point_rules(request.user),
    }


@never_cache
@login_required
@require_GET
def backoffice_commissions(request):
    context = _commissions_context(request)
    return render(request, "backoffice/commissions.html", context)


@never_cache
@csrf_protect
@login_required
@require_POST
def review_commission_form(request, commission_id):
    decision = request.POST.get("decision")
    reason = request.POST.get("reason", "")
    try:
        review_commission(request.user, commission_id, decision=decision, reason=reason)
        messages.success(
            request,
            "Commission approved." if decision == "approve" else "Commission rejected. Reason recorded."
        )
    except CollectionWorkflowError as exc:
        messages.error(request, exc.message)
    return redirect("backoffice_commissions")


# ---------------------------------------------------------------------------
# Payouts Views
# ---------------------------------------------------------------------------

def _payouts_context(request):
    from django.db.models import Count, Sum
    context = _backoffice_context(request, "payouts", "Disbursement Payouts")
    if not can_manage_payouts(request.user):
        raise PermissionDenied("Payout management permission is required.")

    base = reviewable_payouts(request.user)
    counts = {
        "draft": base.filter(status=Payout.Status.DRAFT).count(),
        "approved": base.filter(status=Payout.Status.APPROVED).count(),
        "paid": base.filter(status=Payout.Status.PAID).count(),
    }

    state = request.GET.get("state", "draft")
    if state not in {"draft", "approved", "paid", "all"}:
        state = "draft"

    payouts = base
    if state != "all":
        payouts = payouts.filter(status=state)

    eligible_commissions = Commission.objects.filter(
        status=Commission.Status.APPROVED,
        payout_item__isnull=True,
    )
    if not request.user.is_superuser:
        user_client_ids = ClientAccess.objects.filter(
            user=request.user, is_active=True, client__is_active=True
        ).values_list("client_id", flat=True)
        eligible_commissions = eligible_commissions.filter(client_id__in=user_client_ids)

    eligible_pairs = (
        eligible_commissions.values("client_id", "client__name", "client__code", "collector_id", "collector__user__username")
        .annotate(total_amount=Sum("amount"), comm_count=Count("id"))
        .order_by("client__name", "collector__user__username")
    )

    page = Paginator(payouts, 10).get_page(request.GET.get("page"))
    return {
        **context,
        "payouts": page,
        "page_obj": page,
        "payout_counts": counts,
        "payout_state": state,
        "eligible_pairs": eligible_pairs,
        "can_manage_point_rules": can_manage_point_rules(request.user),
    }


@never_cache
@login_required
@require_GET
def backoffice_payouts(request):
    context = _payouts_context(request)
    return render(request, "backoffice/payouts.html", context)


@never_cache
@csrf_protect
@login_required
@require_POST
def create_payout_form(request):
    client_id = request.POST.get("client_id")
    collector_id = request.POST.get("collector_id")
    try:
        payout = create_payout(request.user, client_id=client_id, collector_id=collector_id)
        messages.success(request, f"Draft payout #{payout.pk} created for {payout.currency} {payout.amount:,.2f}.")
    except CollectionWorkflowError as exc:
        messages.error(request, exc.message)
    return redirect("backoffice_payouts")


@never_cache
@csrf_protect
@login_required
@require_POST
def approve_payout_form(request, payout_id):
    try:
        payout = approve_payout(request.user, payout_id)
        messages.success(request, f"Payout #{payout.pk} approved.")
    except CollectionWorkflowError as exc:
        messages.error(request, exc.message)
    return redirect("backoffice_payouts")


@never_cache
@csrf_protect
@login_required
@require_POST
def mark_payout_paid_form(request, payout_id):
    reference = request.POST.get("reference", "")
    try:
        payout = mark_payout_paid(request.user, payout_id, reference=reference)
        messages.success(request, f"Payout #{payout.pk} marked as paid (Ref: {payout.reference}). Linked commissions settled.")
    except CollectionWorkflowError as exc:
        messages.error(request, exc.message)
    return redirect("backoffice_payouts")


# ---------------------------------------------------------------------------
# Collector Commissions View
# ---------------------------------------------------------------------------

@never_cache
@login_required
@require_GET
def collector_commissions(request):
    if not request.user.is_active or has_backoffice_access(request.user):
        raise PermissionDenied("Use the Back Office portal with this account.")
    collector_profile = CollectorProfile.objects.filter(
        user=request.user, approval_status="approved"
    ).first()
    if collector_profile is None:
        raise PermissionDenied("An approved collector account is required.")

    summary = collector_earnings_summary(collector_profile)
    commissions = collector_commissions_list(collector_profile)
    payouts = collector_payouts_list(collector_profile)

    context = {
        "collector_profile": collector_profile,
        "collector_section": "commissions",
        "navbar_title": "Commissions",
        "summary": summary,
        "commissions": commissions[:20],
        "payouts": payouts[:20],
    }
    return render(request, "collector/commissions.html", context)


# ---------------------------------------------------------------------------
# Collector Performance View
# ---------------------------------------------------------------------------

@never_cache
@login_required
@require_GET
def collector_performance(request):
    if not request.user.is_active or has_backoffice_access(request.user):
        raise PermissionDenied("Use the Back Office portal with this account.")
    collector_profile = CollectorProfile.objects.filter(
        user=request.user, approval_status="approved"
    ).first()
    if collector_profile is None:
        raise PermissionDenied("An approved collector account is required.")

    summary = collector_points_summary(collector_profile)
    txns = collector_point_transactions_list(collector_profile)

    context = {
        "collector_profile": collector_profile,
        "collector_section": "performance",
        "navbar_title": "Stars & Performance",
        "summary": summary,
        "transactions": txns[:20],
    }
    return render(request, "collector/performance.html", context)
