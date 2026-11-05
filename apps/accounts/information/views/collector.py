"""Collector proposal presentation; authorization stays in account services."""

import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods

from apps.accounts.information.forms import InformationProposalForm
from apps.accounts.information.models import AccountInformationProposal
from apps.accounts.information.services import InformationWorkflowError, collector_information_context, submit_proposal


@never_cache
@login_required
@require_http_methods(["GET", "POST"])
@csrf_protect
def propose_information(request, assignment_id):
    try:
        context = collector_information_context(request.user, assignment_id)
    except InformationWorkflowError as exc:
        return HttpResponse(exc.message, status=exc.status, content_type="text/plain")
    categories = AccountInformationProposal.Category
    category = request.POST.get("category") if request.method == "POST" else request.GET.get("category", "email")
    if category not in categories.values:
        category = "email"
    form = InformationProposalForm(request.POST if request.method == "POST" else None,
        category=category, documents=context["documents"], initial={"event_id": uuid.uuid4()})
    status = 200
    if request.method == "POST":
        if form.is_valid():
            try:
                result = submit_proposal(request.user, assignment_id, **form.submission_data())
            except InformationWorkflowError as exc:
                if exc.status in {403, 404}:
                    return HttpResponse(exc.message, status=exc.status, content_type="text/plain")
                form.add_error(None, exc.message)
                status = exc.status
            else:
                messages.success(request, "Information proposal submitted for Back Office review." if result["created"] else "This proposal was already submitted.")
                return redirect("assignments:propose_information", assignment_id=assignment_id)
        else:
            status = 400
    page = Paginator(context.pop("proposals"), 10).get_page(request.GET.get("page"))
    context.update(form=form, category=category, categories=categories.choices,
        category_label=categories(category).label, proposals=page, page_obj=page)
    return render(request, "information/collector_form.html", context, status=status)
