import json
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.models import Client
from apps.collection.models import CommissionRule
from apps.user.models import ClientAccess, has_backoffice_access
from apps.activities.views import _backoffice_context
from .importer import (
    parse_import_file,
    generate_sample_csv,
    execute_import_batch,
    get_trial_import_defaults,
)


def _get_accessible_clients(user):
    if getattr(user, "is_superuser", False):
        return Client.objects.filter(is_active=True).order_by("name")
    client_ids = ClientAccess.objects.filter(user=user, is_active=True).values_list("client_id", flat=True)
    return Client.objects.filter(pk__in=client_ids, is_active=True).order_by("name")


@never_cache
@login_required
@csrf_protect
def backoffice_account_import(request):
    """
    Renders upload form or preview of parsed & geocoded accounts before saving.
    """
    if not has_backoffice_access(request.user):
        raise PermissionDenied("Back Office access is required.")

    context = _backoffice_context(request, "import", "Bulk Account Importer")
    if request.method == "POST":
        uploaded_file = request.FILES.get("import_file")

        if not uploaded_file:
            messages.error(request, "Please upload a CSV or Excel (.xlsx) file.")
            return render(request, "backoffice/account_import.html", context)

        max_size = 10 * 1024 * 1024  # 10MB limit
        if uploaded_file.size > max_size:
            messages.error(request, "Uploaded file exceeds the maximum size limit of 10 MB.")
            return render(request, "backoffice/account_import.html", context)

        client, rule = get_trial_import_defaults()
        try:
            preview_result = parse_import_file(uploaded_file, client.pk)
        except Exception as exc:
            messages.error(request, f"Failed to parse file: {str(exc)}")
            return render(request, "backoffice/account_import.html", context)

        # Store in session for confirmation
        request.session["pending_account_import"] = {
            "client_id": client.pk,
            "rule_id": rule.pk,
            "filename": uploaded_file.name,
            "rows": preview_result["rows"],
            "stats": preview_result["stats"],
        }

        context["preview_mode"] = True
        context["selected_client"] = client
        context["selected_rule"] = rule
        context["preview_rows"] = preview_result["rows"]
        context["stats"] = preview_result["stats"]
        return render(request, "backoffice/account_import.html", context)

    # GET request - reset session if any
    request.session.pop("pending_account_import", None)
    context["preview_mode"] = False
    return render(request, "backoffice/account_import.html", context)


@never_cache
@login_required
@csrf_protect
@require_POST
def backoffice_account_import_confirm(request):
    """
    Executes database insertion for validated accounts from session.
    """
    if not has_backoffice_access(request.user):
        raise PermissionDenied("Back Office access is required.")

    pending = request.session.get("pending_account_import")
    if not pending or not pending.get("rows"):
        messages.warning(request, "No pending import session found. Please upload your file again.")
        return redirect("backoffice_account_import")

    client_id = pending["client_id"]
    rule_id = pending["rule_id"]
    rows = pending["rows"]

    try:
        created = execute_import_batch(client_id, rule_id, rows, request.user)
        request.session.pop("pending_account_import", None)
        messages.success(request, f"Successfully imported {len(created)} accounts with geocoded collection coordinates!")
        return redirect("backoffice_assignments")
    except Exception as exc:
        messages.error(request, f"Failed to import accounts: {str(exc)}")
        return redirect("backoffice_account_import")


@never_cache
@login_required
@require_GET
def download_import_template(request):
    """
    Streams downloadable CSV template.
    """
    if not has_backoffice_access(request.user):
        raise PermissionDenied("Back Office access is required.")

    csv_content = generate_sample_csv()
    response = HttpResponse(csv_content, content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="ficol_accounts_import_template.csv"'
    return response
