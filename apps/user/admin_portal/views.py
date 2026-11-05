from .users.services import get_users_context
from .dashboard.services import get_dashboard_context, get_dashboard_records
from apps.user.backoffice_modules import PERMISSION_GROUPS, selected_modules
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import models
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.accounts.models import Client
from apps.audit.models import AuditEvent
from apps.audit.admin_log import audit_context, csv_export
from apps.user.invitations import (
    get_collector_account_setup_status,
    get_collector_approval_status_info,
    get_collector_standing_info,
    get_collector_badge_statuses,
)
from apps.user.models import BACK_OFFICE_PERMISSIONS, CollectorProfile
from apps.user.roles import (
    get_backoffice_groups_queryset,
    get_backoffice_role_group_names,
    get_user_role_info,
)

from .forms import (
    BackOfficeUserCreateForm,
    BackOfficeUserEditForm,
    ClientForm,
    CollectorConversionForm,
    CollectorCreateForm,
    CollectorEditForm,
    CollectorOffboardForm,
    CollectorReinstateForm,
    CollectorSuspendForm,
    CollectorWarningForm,
    UserCreateForm,
    UserEditForm,
    get_available_backoffice_permissions,
)
from .permissions import system_admin_required
from .services import (
    approve_collector_profile,
    convert_collector_to_backoffice,
    create_backoffice_user_account,
    create_client_record,
    create_collector_account,
    create_user_account,
    issue_collector_warning,
    offboard_collector_profile,
    reinstate_collector_profile,
    reject_collector_profile,
    resend_collector_invitation,
    revoke_collector_invitation,
    send_collector_password_reset_email,
    suspend_collector_profile,
    update_backoffice_user_account,
    update_client_record,
    update_collector_account,
    update_user_account,
    set_collector_account_active,
)

User = get_user_model()


@never_cache
@system_admin_required
@require_GET
def system_admin_dashboard(request):
    return render(request, "system_admin/dashboard.html", get_dashboard_context(request.GET.get("range", "7")))


@never_cache
@system_admin_required
@require_GET
def system_admin_dashboard_records(request):
    title, records = get_dashboard_records(request.GET)
    return render(request, "system_admin/dashboard_records.html", {
        "admin_section": "dashboard", "page_title": title, "records": records,
    })


@never_cache
@system_admin_required
@require_GET
def system_admin_users(request):
    return render(request, "system_admin/users.html", get_users_context(request.GET))


@never_cache
@system_admin_required
@require_http_methods(["GET", "POST"])
@csrf_protect
def system_admin_collector_create(request):
    """Explicit account creation flow: Add Collector."""
    if request.method == "POST":
        form = CollectorCreateForm(request.POST)
        if form.is_valid():
            user = create_collector_account(
                actor=request.user,
                email=form.cleaned_data["email"],
                first_name=form.cleaned_data.get("first_name", ""),
                last_name=form.cleaned_data.get("last_name", ""),
                request=request,
            )
            messages.success(request, f"Collector '{user.username}' created successfully. An invitation email has been sent.")
            return redirect("system_admin_users")
    else:
        form = CollectorCreateForm()

    context = {
        "admin_section": "users",
        "form": form,
        "is_create": True,
        "account_type": "collector",
    }
    return render(request, "system_admin/collector_form.html", context)


@never_cache
@system_admin_required
@require_http_methods(["GET", "POST"])
@csrf_protect
def system_admin_backoffice_user_create(request):
    """Explicit account creation flow: Add Back Office User."""
    if request.method == "POST":
        form = BackOfficeUserCreateForm(request.POST)
        if form.is_valid():
            user = create_backoffice_user_account(
                actor=request.user,
                username=form.cleaned_data["username"],
                email=form.cleaned_data["email"],
                first_name=form.cleaned_data["first_name"],
                last_name=form.cleaned_data["last_name"],
                role_group_ids=form.cleaned_data.get("role_groups", []),
                selected_client_ids=form.cleaned_data.get("client_accesses", []),
                module_keys=form.cleaned_data["modules"],
                request=request,
            )
            messages.success(request, f"Back Office user '{user.username}' created successfully.")
            return redirect("system_admin_users")
    else:
        form = BackOfficeUserCreateForm()

    context = {
        "admin_section": "users",
        "form": form,
        "is_create": True,
        "account_type": "backoffice",
    }
    context["permission_groups"] = PERMISSION_GROUPS
    return render(request, "system_admin/backoffice_user_form.html", context)


@never_cache
@system_admin_required
@require_http_methods(["GET", "POST"])
@csrf_protect
def system_admin_user_create(request):
    """Legacy endpoint for user creation. Delegates to collector create on GET, supports legacy form on POST."""
    if request.method == "POST":
        form = UserCreateForm(request.POST)
        if form.is_valid():
            user = create_user_account(
                actor=request.user,
                username=form.cleaned_data["username"],
                password=form.cleaned_data["password"],
                email=form.cleaned_data["email"],
                first_name=form.cleaned_data["first_name"],
                last_name=form.cleaned_data["last_name"],
                create_collector_profile=form.cleaned_data["create_collector_profile"],
            )
            messages.success(request, f"User '{user.username}' created successfully.")
            return redirect("system_admin_users")
    else:
        form = UserCreateForm()

    context = {
        "admin_section": "users",
        "form": form,
        "is_create": True,
    }
    return render(request, "system_admin/user_form.html", context)


@never_cache
@system_admin_required
@require_http_methods(["GET", "POST"])
@csrf_protect
def system_admin_collector_edit(request, user_id):
    """Refactored Collector detail/edit view with 6 distinct concerns:
    1. Profile & Basic Information (NO password fields)
    2. Collector Standing (Active / Suspended / Offboarded / Pending with explicit actions)
    3. Client Access Scope
    4. Security & Invitation Status
    5. Warnings & Disciplinary History
    6. Audit Timeline
    """
    target_user = get_object_or_404(
        User.objects.select_related("collector_profile").prefetch_related("groups"), pk=user_id
    )
    if target_user.is_superuser:
        raise PermissionDenied("Superusers are read-only system accounts and cannot be modified.")

    profile = getattr(target_user, "collector_profile", None)
    if not profile:
        return redirect("system_admin_backoffice_user_edit", user_id=target_user.pk)

    standing_info = get_collector_standing_info(profile)
    is_offboarded = (standing_info["standing"] == "offboarded")

    if request.method == "POST":
        if is_offboarded:
            messages.error(request, "Offboarded collectors are read-only and cannot be modified.")
            return redirect("system_admin_collector_edit", user_id=target_user.pk)

        old_email = target_user.email
        form = CollectorEditForm(request.POST, target_user=target_user)
        if form.is_valid():
            new_email = form.cleaned_data.get("email")
            update_collector_account(
                actor=request.user,
                user_id=target_user.pk,
                first_name=form.cleaned_data.get("first_name", ""),
                last_name=form.cleaned_data.get("last_name", ""),
                email=new_email,
                selected_client_ids=list(target_user.client_accesses.filter(is_active=True).values_list("client_id", flat=True)),
                request=request,
            )
            if new_email and new_email.strip().lower() != (old_email or "").strip().lower():
                messages.success(
                    request,
                    f"Collector '{target_user.username}' email updated to {new_email}. "
                    f"Prior invitations revoked, email verification reset, and a new verification link was sent."
                )
            else:
                messages.success(request, f"Collector '{target_user.username}' updated successfully.")
            return redirect("system_admin_collector_edit", user_id=target_user.pk)
    else:
        current_active_cids = list(
            target_user.client_accesses.filter(is_active=True).values_list("client_id", flat=True)
        )
        initial = {
            "first_name": target_user.first_name,
            "last_name": target_user.last_name,
            "email": target_user.email,
            "client_accesses": [str(cid) for cid in current_active_cids],
        }
        form = CollectorEditForm(initial=initial, target_user=target_user)

    conversion_form = CollectorConversionForm(target_user=target_user)
    setup_status = get_collector_account_setup_status(target_user)
    approval_info = get_collector_approval_status_info(profile)
    latest_invitation = target_user.collector_invitations.order_by("-created_at").first()
    has_active_invitation = target_user.collector_invitations.filter(status="sent").exists()

    # Disciplinary history records
    disciplinary_records = profile.disciplinary_records.select_related("issued_by").order_by("-created_at")

    # Audit timeline
    invitation_ids = [str(pk) for pk in target_user.collector_invitations.values_list("pk", flat=True)]
    audit_events = (
        AuditEvent.objects.filter(
            models.Q(entity_type="User", entity_id=str(target_user.pk))
            | models.Q(entity_type="CollectorProfile", entity_id=str(profile.pk))
            | models.Q(
                entity_type="CollectorInvitation",
                entity_id__in=invitation_ids,
            )
        )
        .select_related("actor")
        .order_by("-created_at")[:25]
    )

    # Active client accesses (for display/read-only)
    active_client_accesses = target_user.client_accesses.filter(is_active=True).select_related("client")

    context = {
        "admin_section": "users",
        "target_user": target_user,
        "collector_profile": profile,
        "form": form,
        "conversion_form": conversion_form,
        "is_create": False,
        "is_offboarded": is_offboarded,
        "is_active_collector": (standing_info["standing"] == "active"),
        "is_suspended_collector": (standing_info["standing"] == "suspended"),
        "is_pending_collector": (standing_info["standing"] == "pending"),
        "standing_info": standing_info,
        "role_info": get_user_role_info(target_user),
        "setup_status": setup_status,
        "collector_badges": get_collector_badge_statuses(target_user),
        "approval_info": approval_info,
        "latest_invitation": latest_invitation,
        "has_active_invitation": has_active_invitation,
        "can_resend_invitation": target_user.is_collector_setup_incomplete and not is_offboarded,
        "can_reset_password": target_user.is_collector_setup_complete and not is_offboarded,
        "disciplinary_records": disciplinary_records,
        "audit_events": audit_events,
        "active_client_accesses": active_client_accesses,
        "suspend_form": CollectorSuspendForm(),
        "reinstate_form": CollectorReinstateForm(),
        "offboard_form": CollectorOffboardForm(),
        "warning_form": CollectorWarningForm(),
    }
    return render(request, "system_admin/collector_form.html", context)


@never_cache
@system_admin_required
@require_POST
@csrf_protect
def system_admin_collector_account_access(request, user_id):
    get_object_or_404(User, pk=user_id)
    action = request.POST.get("action")
    if action not in {"activate", "deactivate"}:
        raise PermissionDenied("Invalid account action.")
    try:
        set_collector_account_active(actor=request.user, user_id=user_id, active=action == "activate")
        messages.success(request, "Account enabled. Incomplete accounts must finish email and password setup before login." if action == "activate" else "Account deactivated.")
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    return redirect("system_admin_users")


@never_cache
@system_admin_required
@require_http_methods(["GET", "POST"])
@csrf_protect
def system_admin_backoffice_user_edit(request, user_id):
    """Explicit edit flow for Back Office accounts: role groups, direct permissions, client scope, active status, password reset."""
    target_user = get_object_or_404(
        User.objects.select_related("collector_profile").prefetch_related("groups", "user_permissions"), pk=user_id
    )
    if target_user.is_superuser:
        raise PermissionDenied("Superusers are read-only system accounts and cannot be modified.")

    all_bo_groups = get_backoffice_groups_queryset()
    bo_group_ids = [g.pk for g in all_bo_groups]
    available_bo_perms = get_available_backoffice_permissions()
    bo_perm_ids = [p.pk for p in available_bo_perms]

    if request.method == "POST":
        form = BackOfficeUserEditForm(request.POST, target_user=target_user)
        if form.is_valid():
            update_backoffice_user_account(
                actor=request.user,
                user_id=target_user.pk,
                is_active=form.cleaned_data["is_active"],
                role_group_ids=form.cleaned_data.get("role_groups", []),
                selected_permission_ids=form.cleaned_data.get("backoffice_permissions", []),
                selected_client_ids=form.cleaned_data.get("client_accesses", []),
                new_password=None,
                module_keys=form.cleaned_data["modules"],
            )
            messages.success(request, f"Back Office user '{target_user.username}' updated successfully.")
            return redirect("system_admin_users")
    else:
        current_active_cids = list(
            target_user.client_accesses.filter(is_active=True).values_list("client_id", flat=True)
        )
        current_role_gids = list(
            target_user.groups.filter(pk__in=bo_group_ids).values_list("pk", flat=True)
        )
        current_bo_perm_ids = list(
            target_user.user_permissions.filter(pk__in=bo_perm_ids).values_list("pk", flat=True)
        )
        initial = {
            "is_active": target_user.is_active,
            "modules": selected_modules(target_user),
            "role_groups": [str(gid) for gid in current_role_gids],
            "backoffice_permissions": [str(pid) for pid in current_bo_perm_ids],
            "client_accesses": [str(cid) for cid in current_active_cids],
        }
        form = BackOfficeUserEditForm(initial=initial, target_user=target_user)

    context = {
        "admin_section": "users",
        "target_user": target_user,
        "form": form,
        "is_create": False,
        "role_info": get_user_role_info(target_user),
    }
    context["permission_groups"] = PERMISSION_GROUPS
    return render(request, "system_admin/backoffice_user_form.html", context)


@never_cache
@system_admin_required
@require_http_methods(["POST"])
@csrf_protect
def system_admin_collector_convert(request, user_id):
    """Explicit conversion workflow: Collector to Back Office."""
    target_user = get_object_or_404(
        User.objects.select_related("collector_profile"), pk=user_id
    )
    if target_user.is_superuser:
        raise PermissionDenied("Superusers are read-only system accounts and cannot be modified.")

    form = CollectorConversionForm(request.POST, target_user=target_user)
    if form.is_valid():
        convert_collector_to_backoffice(
            actor=request.user,
            user_id=target_user.pk,
            role_group_ids=form.cleaned_data["role_groups"],
            selected_client_ids=form.cleaned_data.get("client_accesses", []),
        )
        messages.success(
            request,
            f"Collector '{target_user.username}' successfully converted to Back Office. Collector profile suspended and historical records preserved.",
        )
        return redirect("system_admin_backoffice_user_edit", user_id=target_user.pk)
    else:
        current_active_cids = list(
            target_user.client_accesses.filter(is_active=True).values_list("client_id", flat=True)
        )
        profile = getattr(target_user, "collector_profile", None)
        edit_form = CollectorEditForm(
            initial={
                "is_active": target_user.is_active,
                "collector_status": profile.approval_status if profile else "",
                "client_accesses": [str(cid) for cid in current_active_cids],
            },
            target_user=target_user,
        )
        context = {
            "admin_section": "users",
            "target_user": target_user,
            "collector_profile": profile,
            "form": edit_form,
            "conversion_form": form,
            "is_create": False,
            "role_info": get_user_role_info(target_user),
        }
        return render(request, "system_admin/collector_form.html", context)


@never_cache
@system_admin_required
@require_http_methods(["GET", "POST"])
@csrf_protect
def system_admin_user_edit(request, user_id):
    """Route user edit to appropriate flow while maintaining backwards compatibility."""
    target_user = get_object_or_404(
        User.objects.select_related("collector_profile"), pk=user_id
    )

    if target_user.is_superuser:
        if request.method == "POST":
            raise PermissionDenied("Superusers are read-only system accounts and cannot be modified.")
        context = {
            "admin_section": "users",
            "target_user": target_user,
            "is_superuser_account": True,
        }
        return render(request, "system_admin/user_form.html", context)

    available_bo_perms = get_available_backoffice_permissions()
    bo_perm_ids = [p.pk for p in available_bo_perms]

    if request.method == "POST":
        form = UserEditForm(request.POST, target_user=target_user)
        if form.is_valid():
            update_user_account(
                actor=request.user,
                user_id=target_user.pk,
                is_active=form.cleaned_data["is_active"],
                selected_client_ids=form.cleaned_data.get("client_accesses", []),
                selected_permission_ids=form.cleaned_data.get("backoffice_permissions", []),
                collector_status=form.cleaned_data.get("collector_status"),
                create_collector_profile=form.cleaned_data.get("create_collector_profile", False),
                new_password=form.cleaned_data.get("new_password"),
            )
            messages.success(request, f"User '{target_user.username}' updated successfully.")
            return redirect("system_admin_users")
    else:
        current_active_cids = list(
            target_user.client_accesses.filter(is_active=True).values_list("client_id", flat=True)
        )
        current_bo_perm_ids = list(
            target_user.user_permissions.filter(pk__in=bo_perm_ids).values_list("pk", flat=True)
        )

        initial = {
            "is_active": target_user.is_active,
            "client_accesses": [str(cid) for cid in current_active_cids],
            "backoffice_permissions": [str(pid) for pid in current_bo_perm_ids],
        }

        profile = getattr(target_user, "collector_profile", None)
        if profile:
            initial["collector_status"] = profile.approval_status

        form = UserEditForm(initial=initial, target_user=target_user)

    context = {
        "admin_section": "users",
        "target_user": target_user,
        "form": form,
        "is_create": False,
        "has_collector_profile": hasattr(target_user, "collector_profile") and target_user.collector_profile is not None,
    }
    return render(request, "system_admin/user_form.html", context)


@never_cache
@system_admin_required
@require_GET
def system_admin_clients(request):
    q = request.GET.get("q", "").strip()
    clients_qs = Client.objects.order_by("name")

    if q:
        clients_qs = clients_qs.filter(
            models.Q(code__icontains=q) | models.Q(name__icontains=q)
        )

    paginator = Paginator(clients_qs, 20)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    context = {
        "admin_section": "clients",
        "page_obj": page_obj,
        "search_query": q,
    }
    return render(request, "system_admin/clients.html", context)


@never_cache
@system_admin_required
@require_http_methods(["GET", "POST"])
@csrf_protect
def system_admin_client_create(request):
    if request.method == "POST":
        form = ClientForm(request.POST)
        if form.is_valid():
            client = create_client_record(
                actor=request.user,
                code=form.cleaned_data["code"],
                name=form.cleaned_data["name"],
                is_active=form.cleaned_data["is_active"],
            )
            messages.success(request, f"Client '{client.code}' created successfully.")
            return redirect("system_admin_clients")
    else:
        form = ClientForm()

    context = {
        "admin_section": "clients",
        "form": form,
        "is_create": True,
    }
    return render(request, "system_admin/client_form.html", context)


@never_cache
@system_admin_required
@require_http_methods(["GET", "POST"])
@csrf_protect
def system_admin_client_edit(request, client_id):
    client = get_object_or_404(Client, pk=client_id)

    if request.method == "POST":
        form = ClientForm(request.POST, instance=client)
        if form.is_valid():
            update_client_record(
                actor=request.user,
                client_id=client.pk,
                code=form.cleaned_data["code"],
                name=form.cleaned_data["name"],
                is_active=form.cleaned_data["is_active"],
            )
            messages.success(request, f"Client '{client.code}' updated successfully.")
            return redirect("system_admin_clients")
    else:
        form = ClientForm(instance=client)

    context = {
        "admin_section": "clients",
        "client": client,
        "form": form,
        "is_create": False,
    }
    return render(request, "system_admin/client_form.html", context)


@never_cache
@system_admin_required
@require_GET
def system_admin_audit(request):
    return render(request, "system_admin/audit_log.html", audit_context(request.GET))


@never_cache
@system_admin_required
@require_GET
def system_admin_audit_export(request):
    return csv_export(request.GET)


@never_cache
@system_admin_required
@require_POST
@csrf_protect
def system_admin_collector_resend_invitation(request, user_id):
    """Resend a collector invitation email and revoke previous active invitations."""
    target_user = get_object_or_404(User, pk=user_id)
    try:
        new_invitation, _ = resend_collector_invitation(actor=request.user, user_id=target_user.pk, request=request)
        messages.success(request, f"A new invitation email has been sent to {target_user.email}. Prior active invitations were revoked.")
    except Exception as e:
        messages.error(request, f"Failed to resend invitation: {e}")

    next_url = request.POST.get("next") or request.META.get("HTTP_REFERER")
    if not next_url or not next_url.startswith("/"):
        next_url = reverse("system_admin_collector_edit", kwargs={"user_id": target_user.pk})
    return redirect(next_url)


@never_cache
@system_admin_required
@require_POST
@csrf_protect
def system_admin_collector_revoke_invitation(request, user_id):
    """Immediately revoke active collector invitations for a user."""
    target_user = get_object_or_404(User, pk=user_id)
    count = revoke_collector_invitation(actor=request.user, user_id=target_user.pk)
    if count:
        messages.success(request, f"Revoked active invitation(s) for {target_user.username}.")
    else:
        messages.info(request, f"No active invitations to revoke for {target_user.username}.")

    next_url = request.POST.get("next") or request.META.get("HTTP_REFERER")
    if not next_url or not next_url.startswith("/"):
        next_url = reverse("system_admin_collector_edit", kwargs={"user_id": target_user.pk})
    return redirect(next_url)


@never_cache
@system_admin_required
@require_POST
@csrf_protect
def system_admin_collector_send_password_reset(request, user_id):
    """Send password reset email to an activated collector account."""
    target_user = get_object_or_404(User, pk=user_id)
    try:
        send_collector_password_reset_email(actor=request.user, user_id=target_user.pk, request=request)
        messages.success(request, f"A password reset link has been emailed to {target_user.email}.")
    except Exception as e:
        messages.error(request, f"Could not send password reset email: {e}")

    next_url = request.POST.get("next") or request.META.get("HTTP_REFERER")
    if not next_url or not next_url.startswith("/"):
        next_url = reverse("system_admin_collector_edit", kwargs={"user_id": target_user.pk})
    return redirect(next_url)


@never_cache
@system_admin_required
@require_POST
@csrf_protect
def system_admin_collector_approve(request, user_id):
    """Explicitly approve a collector profile."""
    target_user = get_object_or_404(User, pk=user_id)
    try:
        approve_collector_profile(actor=request.user, user_id=target_user.pk)
        messages.success(request, f"Collector '{target_user.username}' has been approved.")
    except Exception as e:
        messages.error(request, f"Could not approve collector: {e}")

    next_url = request.POST.get("next") or request.META.get("HTTP_REFERER")
    if not next_url or not next_url.startswith("/"):
        next_url = reverse("system_admin_collector_edit", kwargs={"user_id": target_user.pk})
    return redirect(next_url)


@never_cache
@system_admin_required
@require_POST
@csrf_protect
def system_admin_collector_reject(request, user_id):
    """Explicitly reject a collector profile."""
    target_user = get_object_or_404(User, pk=user_id)
    try:
        reject_collector_profile(actor=request.user, user_id=target_user.pk)
        messages.success(request, f"Collector '{target_user.username}' has been rejected.")
    except Exception as e:
        messages.error(request, f"Could not reject collector: {e}")

    next_url = request.POST.get("next") or request.META.get("HTTP_REFERER")
    if not next_url or not next_url.startswith("/"):
        next_url = reverse("system_admin_collector_edit", kwargs={"user_id": target_user.pk})
    return redirect(next_url)


@never_cache
@system_admin_required
@require_POST
@csrf_protect
def system_admin_collector_suspend(request, user_id):
    """Explicitly suspend a collector profile. Requires a reason."""
    target_user = get_object_or_404(User, pk=user_id)
    reason = request.POST.get("reason", "").strip()
    try:
        suspend_collector_profile(actor=request.user, user_id=target_user.pk, reason=reason)
        messages.success(request, f"Collector '{target_user.username}' has been suspended.")
    except Exception as e:
        messages.error(request, f"Could not suspend collector: {e}")

    next_url = request.POST.get("next") or request.META.get("HTTP_REFERER")
    if not next_url or not next_url.startswith("/"):
        next_url = reverse("system_admin_collector_edit", kwargs={"user_id": target_user.pk})
    return redirect(next_url)


@never_cache
@system_admin_required
@require_POST
@csrf_protect
def system_admin_collector_reinstate(request, user_id):
    """Reinstate a suspended collector profile. Optional note."""
    target_user = get_object_or_404(User, pk=user_id)
    note = request.POST.get("note", "").strip()
    try:
        reinstate_collector_profile(actor=request.user, user_id=target_user.pk, note=note)
        messages.success(request, f"Collector '{target_user.username}' has been reinstated to active standing.")
    except Exception as e:
        messages.error(request, f"Could not reinstate collector: {e}")

    next_url = request.POST.get("next") or request.META.get("HTTP_REFERER")
    if not next_url or not next_url.startswith("/"):
        next_url = reverse("system_admin_collector_edit", kwargs={"user_id": target_user.pk})
    return redirect(next_url)


@never_cache
@system_admin_required
@require_POST
@csrf_protect
def system_admin_collector_offboard(request, user_id):
    """Offboard a collector profile. Requires a reason."""
    target_user = get_object_or_404(User, pk=user_id)
    reason = request.POST.get("reason", "").strip()
    try:
        offboard_collector_profile(actor=request.user, user_id=target_user.pk, reason=reason)
        messages.success(request, f"Collector '{target_user.username}' has been offboarded.")
    except Exception as e:
        messages.error(request, f"Could not offboard collector: {e}")

    next_url = request.POST.get("next") or request.META.get("HTTP_REFERER")
    if not next_url or not next_url.startswith("/"):
        next_url = reverse("system_admin_collector_edit", kwargs={"user_id": target_user.pk})
    return redirect(next_url)


@never_cache
@system_admin_required
@require_POST
@csrf_protect
def system_admin_collector_issue_warning(request, user_id):
    """Issue a formal warning or final warning to a collector. Requires a reason."""
    target_user = get_object_or_404(User, pk=user_id)
    level = request.POST.get("level", "warning")
    reason = request.POST.get("reason", "").strip()
    try:
        issue_collector_warning(actor=request.user, user_id=target_user.pk, level=level, reason=reason)
        level_label = "Final Warning" if level == "final_warning" else "Warning"
        messages.success(request, f"{level_label} issued to '{target_user.username}'.")
    except Exception as e:
        messages.error(request, f"Could not issue warning: {e}")

    next_url = request.POST.get("next") or request.META.get("HTTP_REFERER")
    if not next_url or not next_url.startswith("/"):
        next_url = reverse("system_admin_collector_edit", kwargs={"user_id": target_user.pk})
    return redirect(next_url)


