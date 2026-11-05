from zoneinfo import ZoneInfo

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods

from .admin_portal.forms import CollectorSetPasswordForm
from .admin_portal.services import accept_collector_invitation
from .admin_portal.services import accept_backoffice_invitation
from .models import BackOfficeInvitation
from .invitations import INVITATION_EXPIRY_HOURS, hash_invitation_token
from .models import ClientAccess, CollectorInvitation, CollectorProfile, has_backoffice_access



class FiColLoginView(LoginView):
    template_name = "registration/login.html"
    redirect_authenticated_user = False

    def get_success_url(self):
        return portal_url(self.request.user)


def portal_url(user):
    if getattr(user, "is_superuser", False) and getattr(user, "is_active", False):
        return reverse("system_admin_dashboard")
    if has_backoffice_access(user):
        return reverse("supervisor_dashboard")
    if user.is_active and CollectorProfile.objects.filter(user=user, approval_status="approved").exists():
        return reverse("collector_dashboard")
    if user.is_active and CollectorProfile.objects.filter(user=user).exists():
        return reverse("collector_pending_approval")
    raise PermissionDenied("Your account does not have portal access.")


@never_cache
@login_required
def collector_dashboard(request):
    if not request.user.is_active or has_backoffice_access(request.user):
        raise PermissionDenied("Use the Back Office portal with this account.")
    collector_profile = CollectorProfile.objects.filter(
        user=request.user, approval_status="approved"
    ).first()

    if collector_profile is None:
        if CollectorProfile.objects.filter(user=request.user).exists():
            return redirect("collector_pending_approval")
        raise PermissionDenied("An approved collector account is required.")

    # Record first portal visit if not yet recorded
    if getattr(collector_profile, "first_portal_visit_at", None) is None:
        if hasattr(collector_profile, "save"):
            collector_profile.first_portal_visit_at = timezone.now()
            collector_profile.save(update_fields=["first_portal_visit_at"])

    context = {
        "collector_profile": collector_profile,

        "offers_connected": False,
        "offers_loading": False,
        "offers_error": None,
        "nearby_offers": [],

        "assigned_accounts_connected": False,
        "assigned_accounts": [],

        "follow_ups_connected": False,
        "follow_ups": [],

        "earnings_connected": False,
        "earnings_period_label": None,
        "earnings": {
            "awaiting_approval": None,
            "approved_unpaid": None,
            "paid_out": None,
        },

        "performance_connected": False,
        "verified_points": None,
    }

    if request.user.is_active and collector_profile.approval_status == "approved":
        from apps.activities.services import pending_followups, followup_counts, followup_period
        from apps.assignments.services import acceptance_allowance, client_ids
        from apps.assignments.models import AccountAssignment
        now=timezone.now()
        context["daily_acceptance"]=acceptance_allowance(collector_profile,now)
        assignments = AccountAssignment.objects.filter(
            collector=collector_profile, status="active",
            account__client_id__in=client_ids(collector_profile),
        ).select_related("account__customer", "target_address").order_by("first_action_due_at", "pk")
        context["assigned_total"] = assignments.count()
        context["assigned_accounts_connected"] = True
        context["assigned_accounts"] = []
        for assignment in assignments[:5]:
            deadline = assignment.next_action_due_at or assignment.first_action_due_at
            context["assigned_accounts"].append({
                "reference_number": assignment.account.reference,
                "customer_name": assignment.account.customer.display_name,
                "area": assignment.target_address.address_line,
                "balance_display": f"{assignment.account.currency} {assignment.account.balance_snapshot:,.2f}",
                "next_deadline_display": timezone.localtime(deadline, timezone=ZoneInfo('Asia/Manila')).strftime('%d %b %Y, %I:%M %p'),
                "is_overdue": deadline < now,
                "url": reverse('assignments:account_detail', args=[assignment.pk]),
            })
        tasks=pending_followups(collector_profile)
        counts=followup_counts(tasks,now)
        context["follow_ups_connected"]=True
        context["follow_ups_total"]=counts["total"]
        context["follow_ups"]=[{
            "next_action":task.get_kind_display(),"account_reference":task.account.reference,
            "is_overdue":followup_period(task,now)=="overdue",
            "due_datetime_display":timezone.localtime(task.due_at,timezone=ZoneInfo('Asia/Manila')).strftime('%d %b %Y, %I:%M %p'),
            "account_url":reverse('assignments:account_detail',args=[task.assignment_id]),
        } for task in tasks[:5]]

        from apps.collection.services import collector_earnings_summary, collector_points_summary
        earnings_summary = collector_earnings_summary(collector_profile)
        context["earnings_connected"] = True
        context["earnings_period_label"] = "All Time"
        context["earnings"] = {
            "awaiting_approval": f"{earnings_summary['awaiting_approval']:,.2f}",
            "approved_unpaid": f"{earnings_summary['approved_unpaid']:,.2f}",
            "paid_out": f"{earnings_summary['paid_out']:,.2f}",
        }

        points_summary = collector_points_summary(collector_profile)
        context["performance_connected"] = True
        context["verified_points"] = points_summary["total"]
        context["points_breakdown"] = points_summary["breakdown"]

    return render(request, "dashboard/collector.html", context)


@require_GET
@never_cache
@login_required
def collector_profile(request):
    if not request.user.is_active or has_backoffice_access(request.user):
        raise PermissionDenied("Use the Back Office portal with this account.")
    collector_profile = CollectorProfile.objects.filter(
        user=request.user, approval_status="approved"
    ).first()

    if collector_profile is None:
        raise PermissionDenied("An approved collector account is required.")

    client_accesses = (
        ClientAccess.objects.filter(
            user=request.user,
            is_active=True,
            client__is_active=True,
        )
        .select_related("client")
        .order_by("client__name")
    )

    context = {
        "collector_profile": collector_profile,
        "collector_section": "profile",
        "navbar_title": "Profile",
        "client_accesses": client_accesses,
    }
    return render(request, "collector/profile.html", context)


@require_GET
@never_cache
@login_required
def collector_help(request):
    if not request.user.is_active or has_backoffice_access(request.user):
        raise PermissionDenied("Use the Back Office portal with this account.")
    collector_profile = CollectorProfile.objects.filter(
        user=request.user, approval_status="approved"
    ).first()

    if collector_profile is None:
        raise PermissionDenied("An approved collector account is required.")

    context = {
        "collector_profile": collector_profile,
        "collector_section": "help",
        "navbar_title": "Help & Support",
    }
    return render(request, "collector/help_support.html", context)


@never_cache
@login_required
def collector_pending_approval(request):
    """Render a clear awaiting approval screen for pending collectors without work access."""
    if has_backoffice_access(request.user):
        return redirect("supervisor_dashboard")
    profile = CollectorProfile.objects.filter(user=request.user).first()
    if not profile:
        raise PermissionDenied("No collector profile found for this account.")
    if profile.approval_status == CollectorProfile.ApprovalStatus.APPROVED:
        return redirect("collector_dashboard")

    context = {
        "collector_profile": profile,
        "approval_status": profile.approval_status,
        "is_pending": profile.approval_status == CollectorProfile.ApprovalStatus.PENDING,
        "is_suspended": profile.approval_status == CollectorProfile.ApprovalStatus.SUSPENDED,
        "is_rejected": profile.approval_status == CollectorProfile.ApprovalStatus.REJECTED,
    }
    return render(request, "collector/pending_approval.html", context)


@never_cache
@require_http_methods(["GET", "POST"])
def backoffice_invitation_setup(request, token):
    invitation = BackOfficeInvitation.objects.select_related("user").filter(token_hash=hash_invitation_token(token)).first()
    context = {"invitation": invitation, "account_type": "Back Office", "expiry_hours": INVITATION_EXPIRY_HOURS}
    if not invitation or invitation.status != "sent" or invitation.is_expired:
        context["error_message"] = "This invitation link is invalid or has expired. Please ask your administrator to send a new invitation."
        return render(request, "registration/collector_set_password.html", context, status=400)
    form = CollectorSetPasswordForm(request.POST if request.method == "POST" else None, user=invitation.user)
    if request.method == "POST" and form.is_valid():
        try:
            accept_backoffice_invitation(raw_token=token, password=form.cleaned_data["password"])
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            messages.success(request, "Email verified and password set. Sign in to your Back Office account.")
            return redirect("login")
    context.update(form=form, user_obj=invitation.user)
    return render(request, "registration/collector_set_password.html", context)


@never_cache
def collector_invitation_setup(request, token):
    """Validate one-time token, allow password setup, and activate account."""
    token_hash = hash_invitation_token(token)
    invitation = (
        CollectorInvitation.objects.select_related("user")
        .filter(token_hash=token_hash)
        .first()
    )

    error_message = None
    if not invitation:
        error_message = "This invitation link is invalid. Please ask your administrator to send a new invitation."
    elif invitation.status == CollectorInvitation.Status.REVOKED:
        error_message = "This invitation has been revoked. Please ask your administrator to send a new invitation."
    elif invitation.status == CollectorInvitation.Status.ACCEPTED:
        error_message = "This link has already been used. Please sign in with your password."
    elif invitation.is_expired:
        if invitation.status != CollectorInvitation.Status.EXPIRED:
            invitation.status = CollectorInvitation.Status.EXPIRED
            invitation.save(update_fields=["status", "updated_at"])
        error_message = "This link has expired. Please ask your administrator to send a new invitation."
    elif invitation.status != CollectorInvitation.Status.SENT:
        error_message = "This invitation link is no longer valid. Please ask your administrator to send a new invitation."

    if error_message:
        return render(
            request,
            "registration/collector_set_password.html",
            {
                "error_message": error_message,
                "invitation": invitation,
                "expiry_hours": INVITATION_EXPIRY_HOURS,
            },
            status=400,
        )

    if request.method == "POST":
        form = CollectorSetPasswordForm(request.POST, user=invitation.user)
        if form.is_valid():
            user = accept_collector_invitation(
                raw_token=token,
                password=form.cleaned_data["password"],
            )
            login(request, user)
            messages.success(request, "Password set successfully! Your account is now activated.")
            return redirect(portal_url(user))
    else:
        form = CollectorSetPasswordForm(user=invitation.user)

    context = {
        "form": form,
        "invitation": invitation,
        "user_obj": invitation.user,
        "expiry_hours": INVITATION_EXPIRY_HOURS,
    }
    return render(request, "registration/collector_set_password.html", context)

