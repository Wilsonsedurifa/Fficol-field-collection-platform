"""Back Office information queue and protected decisions."""

from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_GET, require_POST

from ..forms import InformationDecisionForm
from ..models import AccountInformationProposal as Proposal
from ..services import (InformationWorkflowError, information_backoffice_context, information_queue,
    information_proposal_for_review, information_review_row, review_proposal)


def _filters(params):
    status, category = params.get("status", "pending"), params.get("category", "all")
    return {"status": status if status in [*Proposal.Status.values, "all"] else "pending",
        "category": category if category in [*Proposal.Category.values, "all"] else "all",
        "query": params.get("q", "").strip()[:100]}


def _queue_response(request, params, *, decision_form=None, proposal_id=None, error="", response_status=200):
    filters = _filters(params)
    context = information_backoffice_context(request.user)
    page = Paginator(information_queue(request.user, **filters), 10).get_page(params.get("page"))
    rows = []
    for proposal in page:
        row = information_review_row(request.user, proposal)
        row["form"] = decision_form if proposal.pk == proposal_id else InformationDecisionForm(prefix=f"proposal-{proposal.pk}")
        rows.append(row)
    context.update(rows=rows, page_obj=page, filters=filters, review_error=error,
        statuses=[("all", "All statuses"), *Proposal.Status.choices], categories=[("all", "All categories"), *Proposal.Category.choices])
    return render(request, "information/backoffice_queue.html", context, status=response_status)


def _error_response(exc):
    return HttpResponse(exc.message, status=exc.status, content_type="text/plain")


@never_cache
@login_required
@require_GET
def information_proposals(request):
    try:
        return _queue_response(request, request.GET)
    except InformationWorkflowError as exc:
        return _error_response(exc)


@never_cache
@login_required
@require_POST
@csrf_protect
def information_decision(request, proposal_id):
    try:
        information_proposal_for_review(request.user, proposal_id)
        form = InformationDecisionForm(request.POST, prefix=f"proposal-{proposal_id}")
        if not form.is_valid():
            return _queue_response(request, request.POST, decision_form=form, proposal_id=proposal_id,
                error="Please correct the decision form. Rejections require a reason.", response_status=400)
        try:
            proposal = review_proposal(request.user, proposal_id, **form.cleaned_data)
        except InformationWorkflowError as exc:
            if exc.status in {403, 404}:
                return _error_response(exc)
            return _queue_response(request, request.POST, decision_form=form, proposal_id=proposal_id,
                error=exc.message, response_status=exc.status)
    except InformationWorkflowError as exc:
        return _error_response(exc)
    messages.success(request, f"Information proposal {proposal.get_status_display().lower()}.")
    filters = _filters(request.POST)
    query = urlencode({"status": filters["status"], "category": filters["category"], "q": filters["query"], "page": request.POST.get("page", "1")})
    return redirect(reverse("backoffice_information_proposals") + "?" + query)
