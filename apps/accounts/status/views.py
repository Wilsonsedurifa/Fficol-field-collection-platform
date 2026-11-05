"""Views for Back Office account status transitions."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_POST

from apps.accounts.models import Account
from apps.user.models import has_backoffice_access
from apps.user.models import ClientAccess

from .exceptions import AccountStatusWorkflowError
from .forms import ChangeAccountStatusForm
from .services import change_account_status


@never_cache
@login_required
@csrf_protect
@require_POST
def backoffice_change_account_status(request, account_id):
    """Handle status change submission from the Back Office account assignment detail page."""
    if not has_backoffice_access(request.user):
        raise PermissionDenied("Use the Back Office portal with this account.")
    if not request.user.has_perm("accounts.change_account_status"):
        raise PermissionDenied("Account status change permission is required.")

    clients = ClientAccess.objects.filter(user=request.user, is_active=True, client__is_active=True).values("client_id")
    account = get_object_or_404(Account, pk=account_id, client_id__in=clients)
    # Validate allowed transitions in the locked service, preserving exact POST retries.
    form = ChangeAccountStatusForm(request.POST)

    if not form.is_valid():
        for field, errors in form.errors.items():
            for error in errors:
                messages.error(request, error)
        return redirect("backoffice_account_assignment_detail", account_id=account_id)

    try:
        change_account_status(
            account_id=account.pk,
            to_status=form.cleaned_data["to_status"],
            reason=form.cleaned_data["reason"],
            actor=request.user,
            event_id=form.cleaned_data["event_id"],
        )
        messages.success(request, f"Account status updated to '{form.cleaned_data['to_status']}'.")
    except AccountStatusWorkflowError as exc:
        messages.error(request, exc.message)

    return redirect("backoffice_account_assignment_detail", account_id=account_id)
