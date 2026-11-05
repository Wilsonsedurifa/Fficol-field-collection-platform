from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.mail import send_mail
from django.core.validators import validate_email
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Client
from apps.accounts.importer import TRIAL_IMPORT_CLIENT_CODE
from apps.audit.services import append_audit_event
from apps.user.invitations import (
    INVITATION_EXPIRY_HOURS,
    generate_invitation_token,
    hash_invitation_token,
    send_invitation_email,
)
from apps.user.models import (
    ClientAccess,
    CollectorDisciplinaryRecord,
    CollectorInvitation,
    CollectorProfile,
)
from apps.user.roles import get_backoffice_groups_queryset, sync_backoffice_roles
from .forms import get_available_backoffice_permissions

User = get_user_model()


def set_collector_account_active(*, actor, user_id, active):
    """Change login access without changing collector standing or client scope."""
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=user_id)
        profile = CollectorProfile.objects.select_for_update().filter(user=user).first()
        if user.is_superuser or not profile:
            raise PermissionDenied("Only ordinary Collector accounts can be changed here.")
        if profile.approval_status == CollectorProfile.ApprovalStatus.OFFBOARDED:
            raise ValidationError("Offboarded collectors are read-only.")
        legacy_setup_complete = user.has_usable_password() and not user.collector_invitations.exists()
        setup_complete = user.is_collector_setup_complete or legacy_setup_complete
        previous = user.is_active
        desired_login = bool(active and setup_complete)
        if previous == desired_login and user.collector_account_disabled == (not active):
            return user
        user.is_active = desired_login
        user.collector_account_disabled = not active
        user.save(update_fields=["is_active", "collector_account_disabled"])
        if not active:
            CollectorInvitation.objects.filter(user=user, status=CollectorInvitation.Status.SENT).update(
                status=CollectorInvitation.Status.REVOKED, revoked_at=timezone.now(),
            )
            profile.availability = CollectorProfile.Availability.OFFLINE
            profile.save(update_fields=["availability", "updated_at"])
        append_audit_event(
            actor=actor, action="system_admin_user_updated", entity_type="User",
            entity_id=user.pk, changes={"is_active": {"before": previous, "after": desired_login}, "collector_account_disabled": not active},
        )
        return user


def create_collector_account(
    *,
    actor,
    username=None,
    email=None,
    password=None,
    first_name="",
    last_name="",
    request=None,
):
    """Create a Collector account with inactive login status, an eligible CollectorProfile, and send a secure invitation.
    
    Email is strictly required. No temporary password is sent.
    Stores only the SHA-256 hash of the invitation token.
    Appends audit events within an atomic database transaction.
    """
    if email is None:
        clean_email = f"{(username or '').strip()}@example.com".lower()
    else:
        clean_email = email.strip().lower()
        if not clean_email:
            raise ValidationError("Email address is required for a collector account.")

    try:
        validate_email(clean_email)
    except ValidationError:
        raise ValidationError("Enter a valid email address.")

    if User.objects.filter(email__iexact=clean_email, is_active=True).exists():
        raise ValidationError("An active account with this email address already exists.")

    with transaction.atomic():
        user = User(
            username=(username or clean_email).strip(),
            email=clean_email,
            first_name=first_name.strip(),
            last_name=last_name.strip(),
            is_active=False,
            is_superuser=False,
            is_staff=False,
        )
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save()

        CollectorProfile.objects.create(
            user=user,
            approval_status=CollectorProfile.ApprovalStatus.APPROVED,
            availability=CollectorProfile.Availability.OFFLINE,
        )

        # Trial imports use an internal portfolio so a newly created
        # collector can see trial accounts on the offers map without a
        # separate access-assignment step. Real client access remains manual.
        trial_client = Client.objects.filter(
            code=TRIAL_IMPORT_CLIENT_CODE,
            is_active=True,
        ).first()
        if trial_client:
            ClientAccess.objects.get_or_create(
                user=user,
                client=trial_client,
                defaults={"is_active": True},
            )

        raw_token = generate_invitation_token()
        token_hash = hash_invitation_token(raw_token)
        expires_at = timezone.now() + timedelta(hours=INVITATION_EXPIRY_HOURS)

        invitation = CollectorInvitation.objects.create(
            user=user,
            email=clean_email,
            token_hash=token_hash,
            status=CollectorInvitation.Status.SENT,
            expires_at=expires_at,
            sent_at=timezone.now(),
            created_by=actor,
        )

        send_invitation_email(invitation, raw_token, request=request)

        append_audit_event(
            actor=actor,
            action="system_admin_user_created",
            entity_type="User",
            entity_id=user.pk,
            changes={
                "is_active": user.is_active,
                "collector_profile_created": True,
                "invitation_sent": True,
            },
        )
        append_audit_event(
            actor=actor,
            action="collector_invitation_sent",
            entity_type="CollectorInvitation",
            entity_id=invitation.pk,
            changes={
                "status": "sent",
                "email": clean_email,
            },
        )
        return user


def create_backoffice_user_account(
    *,
    actor,
    username,
    password=None,
    email="",
    first_name="",
    last_name="",
    role_group_ids=(),
    module_keys=None,
    selected_client_ids=(),
    request=None,
):
    """Create a Back Office user account assigned to role groups and client access scopes.
    
    Does NOT create a CollectorProfile.
    """
    from apps.user.models import BackOfficeInvitation
    clean_email = (email or "").strip().lower()
    if module_keys is not None:
        validate_email(clean_email)
        if User.objects.filter(email__iexact=clean_email).exists():
            raise ValidationError("An account with this email address already exists.")
    with transaction.atomic():
        user = User.objects.create_user(
            username=username,
            password=None if module_keys is not None else password,
            email=clean_email,
            first_name=first_name,
            last_name=last_name,
            is_superuser=False,
            is_staff=False,
            is_active=module_keys is None,
        )

        # Attach Back Office role groups
        all_bo_groups = get_backoffice_groups_queryset()
        valid_groups = all_bo_groups.filter(pk__in=[int(gid) for gid in role_group_ids])
        if valid_groups.exists():
            user.groups.add(*valid_groups)
        if module_keys is not None:
            from apps.user.backoffice_modules import apply_modules
            apply_modules(user, module_keys, actor=actor)
            raw_token = generate_invitation_token()
            invitation = BackOfficeInvitation.objects.create(
                user=user, email=clean_email, token_hash=hash_invitation_token(raw_token),
                expires_at=timezone.now() + timedelta(hours=INVITATION_EXPIRY_HOURS), sent_at=timezone.now(), created_by=actor,
            )
            send_invitation_email(invitation, raw_token, request=request, account_type="Back Office", setup_route="backoffice_invitation_setup")
            append_audit_event(actor=actor, action="backoffice_invitation_sent", entity_type="BackOfficeInvitation",
                               entity_id=invitation.pk, changes={"status": "sent"})

        # Attach client accesses for active clients
        desired_cids = {int(cid) for cid in selected_client_ids}
        active_clients = Client.objects.filter(pk__in=desired_cids, is_active=True)
        for client in active_clients:
            ClientAccess.objects.create(user=user, client=client, is_active=True)

        append_audit_event(
            actor=actor,
            action="system_admin_user_created",
            entity_type="User",
            entity_id=user.pk,
            changes={
                "is_active": user.is_active,
                "collector_profile_created": False,
                "role_group_count": valid_groups.count(),
            },
        )
        return user


def accept_backoffice_invitation(*, raw_token, password):
    from apps.user.models import BackOfficeInvitation, has_backoffice_role
    with transaction.atomic():
        invitation = BackOfficeInvitation.objects.select_for_update().filter(token_hash=hash_invitation_token(raw_token)).first()
        if not invitation or invitation.status != "sent" or invitation.is_expired:
            raise ValidationError("This invitation is invalid, expired, revoked, or already used. Contact your administrator.")
        user = User.objects.select_for_update().get(pk=invitation.user_id)
        if user.is_superuser or not has_backoffice_role(user) or user.email.lower() != invitation.email.lower():
            raise ValidationError("This invitation is no longer valid for this account.")
        validate_password(password, user=user)
        user.set_password(password)
        user.is_active = True
        user.email_verified_at = user.account_activated_at = timezone.now()
        user.save(update_fields=["password", "is_active", "email_verified_at", "account_activated_at"])
        invitation.status = "accepted"
        invitation.accepted_at = timezone.now()
        invitation.save(update_fields=["status", "accepted_at"])
        append_audit_event(actor=user, action="backoffice_invitation_accepted", entity_type="BackOfficeInvitation",
                           entity_id=invitation.pk, changes={"status": "accepted"})
        return user


def create_user_account(
    *,
    actor,
    username,
    password,
    email="",
    first_name="",
    last_name="",
    create_collector_profile=False,
):
    """Legacy helper to create an ordinary user account and optional collector profile.
    
    Preserved for backwards compatibility with existing tests and callers.
    """
    with transaction.atomic():
        user = User.objects.create_user(
            username=username,
            password=password,
            email=email,
            first_name=first_name,
            last_name=last_name,
            is_superuser=False,
            is_staff=False,
        )
        if create_collector_profile:
            CollectorProfile.objects.create(
                user=user,
                approval_status=CollectorProfile.ApprovalStatus.APPROVED,
                availability=CollectorProfile.Availability.OFFLINE,
            )

        append_audit_event(
            actor=actor,
            action="system_admin_user_created",
            entity_type="User",
            entity_id=user.pk,
            changes={
                "is_active": user.is_active,
                "collector_profile_created": bool(create_collector_profile),
            },
        )
        return user


def update_collector_account(
    *,
    actor,
    user_id,
    first_name=None,
    last_name=None,
    email=None,
    selected_client_ids=(),
    is_active=None,
    collector_status=None,
    new_password=None,
    request=None,
):
    """Update a Collector user account profile and client access scope.
    
    Offboarded collectors are read-only and cannot be modified.
    Changing email clears email_verified_at, sets account to awaiting activation,
    revokes prior invitations, and dispatches a fresh verification invite.
    """
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=user_id)
        if user.is_superuser:
            raise PermissionDenied("Superusers are read-only system accounts and cannot be modified.")

        profile = CollectorProfile.objects.select_for_update().filter(user=user).first()
        if profile and profile.approval_status == CollectorProfile.ApprovalStatus.OFFBOARDED:
            raise ValidationError("Offboarded collectors are read-only and cannot be modified.")

        from apps.user.models import has_backoffice_role
        if collector_status == CollectorProfile.ApprovalStatus.APPROVED and has_backoffice_role(user):
            raise ValidationError(
                "Cannot approve collector profile for a user assigned to Back Office role groups. Active roles must be mutually exclusive."
            )

        if first_name is not None:
            user.first_name = first_name.strip()
        if last_name is not None:
            user.last_name = last_name.strip()

        email_changed = False
        old_email = user.email
        if email is not None:
            clean_email = email.strip().lower()
            if not clean_email:
                raise ValidationError("Email address cannot be empty.")
            try:
                validate_email(clean_email)
            except ValidationError:
                raise ValidationError("Enter a valid email address.")
            if clean_email != (user.email or "").strip().lower():
                if User.objects.filter(email__iexact=clean_email, is_active=True).exclude(pk=user.pk).exists():
                    raise ValidationError("An active account with this email address already exists.")
                email_changed = True
                user.email = clean_email
                user.email_verified_at = None
                user.account_activated_at = None
                user.is_active = False
                user.set_unusable_password()

        old_active = user.is_active
        if is_active is not None:
            user.is_active = bool(is_active)

        if new_password:
            user.set_password(new_password)
            append_audit_event(
                actor=actor,
                action="system_admin_password_reset",
                entity_type="User",
                entity_id=user.pk,
                changes={"password_reset": True},
            )

        # Client access scope
        old_client_count = user.client_accesses.filter(is_active=True).count()
        desired_cids = {int(cid) for cid in selected_client_ids}
        active_clients = Client.objects.filter(pk__in=desired_cids, is_active=True)
        valid_active_cids = set(active_clients.values_list("pk", flat=True))

        for client in active_clients:
            access, created = ClientAccess.objects.get_or_create(
                user=user, client=client, defaults={"is_active": True}
            )
            if not created and not access.is_active:
                access.is_active = True
                access.save(update_fields=["is_active", "updated_at"])

        ClientAccess.objects.filter(user=user, is_active=True).exclude(
            client_id__in=valid_active_cids
        ).update(is_active=False)

        new_client_count = user.client_accesses.filter(is_active=True).count()

        # Collector profile status
        profile = CollectorProfile.objects.filter(user=user).first()
        if profile and collector_status and collector_status != profile.approval_status:
            old_status = profile.approval_status
            if collector_status == CollectorProfile.ApprovalStatus.APPROVED:
                profile.approved_by = actor
                profile.approved_at = timezone.now()
            profile.approval_status = collector_status
            profile.save()
            append_audit_event(
                actor=actor,
                action="system_admin_collector_status_changed",
                entity_type="CollectorProfile",
                entity_id=profile.pk,
                changes={"approval_status": {"before": old_status, "after": collector_status}},
            )

        user.save()
        user_changes = {}
        if old_active != user.is_active:
            user_changes["is_active"] = {"before": old_active, "after": user.is_active}
        if old_client_count != new_client_count:
            user_changes["active_client_access_count"] = {"before": old_client_count, "after": new_client_count}

        if user_changes:
            append_audit_event(
                actor=actor,
                action="system_admin_user_updated",
                entity_type="User",
                entity_id=user.pk,
                changes=user_changes,
            )

        if email_changed:
            # Revoke current active invitations
            prior_active = CollectorInvitation.objects.filter(
                user=user, status=CollectorInvitation.Status.SENT
            )
            for old_inv in prior_active:
                old_inv.status = CollectorInvitation.Status.REVOKED
                old_inv.revoked_at = timezone.now()
                old_inv.save(update_fields=["status", "revoked_at", "updated_at"])

            # Create and dispatch a new verification invitation
            raw_token = generate_invitation_token()
            token_hash = hash_invitation_token(raw_token)
            expires_at = timezone.now() + timedelta(hours=INVITATION_EXPIRY_HOURS)

            new_invitation = CollectorInvitation.objects.create(
                user=user,
                email=user.email,
                token_hash=token_hash,
                status=CollectorInvitation.Status.SENT,
                expires_at=expires_at,
                sent_at=timezone.now(),
                created_by=actor,
            )

            send_invitation_email(new_invitation, raw_token, request=request)

            append_audit_event(
                actor=actor,
                action="collector_email_changed",
                entity_type="User",
                entity_id=user.pk,
                changes={
                    "old_email": (old_email or "")[:100],
                    "new_email": user.email[:100],
                    "email_verified_cleared": True,
                },
            )
            append_audit_event(
                actor=actor,
                action="collector_invitation_sent",
                entity_type="CollectorInvitation",
                entity_id=new_invitation.pk,
                changes={
                    "status": "sent",
                    "email": user.email,
                },
            )

        return user


def update_backoffice_user_account(
    *,
    actor,
    user_id,
    is_active,
    role_group_ids,
    selected_permission_ids=(),
    selected_client_ids=(),
    new_password=None,
    module_keys=None,
):
    """Update a Back Office user account's active state, role groups, direct permissions, and client scope.
    
    Enforces mutual exclusivity: cannot assign Back Office role groups to a user with an active approved collector profile.
    Preserves all group memberships and direct permissions outside Back Office scope.
    """
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=user_id)
        if user.is_superuser:
            raise PermissionDenied("Superusers are read-only system accounts and cannot be modified.")

        # Mutual exclusivity check
        profile = getattr(user, "collector_profile", None)
        if profile and profile.approval_status == CollectorProfile.ApprovalStatus.APPROVED and (role_group_ids or module_keys):
            raise ValidationError(
                "Cannot assign Back Office role groups to a user with an approved Collector Profile. Please use the conversion workflow."
            )

        old_active = user.is_active
        user.is_active = bool(is_active)
        if user.backoffice_invitations.exists() and not user.account_activated_at:
            user.is_active = False

        if new_password:
            user.set_password(new_password)
            append_audit_event(
                actor=actor,
                action="system_admin_password_reset",
                entity_type="User",
                entity_id=user.pk,
                changes={"password_reset": True},
            )

        # Apply modules before legacy reconciliation, retaining the resulting memberships.
        if module_keys is not None:
            from apps.user.backoffice_modules import apply_modules
            apply_modules(user, module_keys, actor=actor)
            role_group_ids = list(get_backoffice_groups_queryset().filter(user=user).values_list("pk", flat=True))
            selected_permission_ids = list(user.user_permissions.values_list("pk", flat=True))

        # Update Back Office role groups while preserving non-backoffice groups
        all_bo_groups = get_backoffice_groups_queryset()
        all_bo_group_ids = {g.pk for g in all_bo_groups}

        current_bo_group_ids = set(
            user.groups.filter(pk__in=all_bo_group_ids).values_list("pk", flat=True)
        )
        desired_bo_group_ids = {
            int(gid) for gid in role_group_ids if int(gid) in all_bo_group_ids
        }

        groups_to_remove = current_bo_group_ids - desired_bo_group_ids
        groups_to_add = desired_bo_group_ids - current_bo_group_ids

        if groups_to_remove:
            user.groups.remove(*groups_to_remove)
        if groups_to_add:
            user.groups.add(*groups_to_add)

        old_group_count = len(current_bo_group_ids)
        new_group_count = len(desired_bo_group_ids)

        # Direct Back Office permissions (exceptional overrides)
        available_bo_perms = get_available_backoffice_permissions()
        available_bo_perm_ids = {p.pk for p in available_bo_perms}
        current_bo_perm_ids = set(
            user.user_permissions.filter(pk__in=available_bo_perm_ids).values_list("pk", flat=True)
        )
        desired_bo_perm_ids = {
            int(pid) for pid in selected_permission_ids if int(pid) in available_bo_perm_ids
        }

        to_remove = current_bo_perm_ids - desired_bo_perm_ids
        to_add = desired_bo_perm_ids - current_bo_perm_ids

        if to_remove:
            user.user_permissions.remove(*to_remove)
        if to_add:
            user.user_permissions.add(*to_add)

        old_bo_count = len(current_bo_perm_ids)
        new_bo_count = len(desired_bo_perm_ids)

        # Client access scope
        old_client_count = user.client_accesses.filter(is_active=True).count()
        desired_cids = {int(cid) for cid in selected_client_ids}
        active_clients = Client.objects.filter(pk__in=desired_cids, is_active=True)
        valid_active_cids = set(active_clients.values_list("pk", flat=True))

        for client in active_clients:
            access, created = ClientAccess.objects.get_or_create(
                user=user, client=client, defaults={"is_active": True}
            )
            if not created and not access.is_active:
                access.is_active = True
                access.save(update_fields=["is_active", "updated_at"])

        ClientAccess.objects.filter(user=user, is_active=True).exclude(
            client_id__in=valid_active_cids
        ).update(is_active=False)

        new_client_count = user.client_accesses.filter(is_active=True).count()

        user.save()
        user_changes = {}
        if old_active != user.is_active:
            user_changes["is_active"] = {"before": old_active, "after": user.is_active}
        if old_group_count != new_group_count:
            user_changes["role_group_count"] = {"before": old_group_count, "after": new_group_count}
        if old_bo_count != new_bo_count:
            user_changes["backoffice_permission_count"] = {"before": old_bo_count, "after": new_bo_count}
        if old_client_count != new_client_count:
            user_changes["active_client_access_count"] = {"before": old_client_count, "after": new_client_count}

        if user_changes:
            append_audit_event(
                actor=actor,
                action="system_admin_user_updated",
                entity_type="User",
                entity_id=user.pk,
                changes=user_changes,
            )

        return user


def convert_collector_to_backoffice(
    *,
    actor,
    user_id,
    role_group_ids,
    selected_client_ids=(),
):
    """Explicit conversion workflow from Collector to Back Office user.
    
    1. Suspend or deactivate the collector role/profile
    2. Assign Back Office role group(s)
    3. Update client access scopes if provided
    4. Record audit events
    Preserves historical collector profile and operational records.
    """
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=user_id)
        if user.is_superuser:
            raise PermissionDenied("Superusers are read-only system accounts and cannot be modified.")

        profile = CollectorProfile.objects.select_for_update().filter(user=user).first()
        if not profile:
            raise ValidationError("This user does not have a Collector Profile to convert.")

        # 1. Suspend the collector profile
        old_status = profile.approval_status
        profile.approval_status = CollectorProfile.ApprovalStatus.SUSPENDED
        profile.availability = CollectorProfile.Availability.OFFLINE
        profile.save()

        append_audit_event(
            actor=actor,
            action="system_admin_collector_status_changed",
            entity_type="CollectorProfile",
            entity_id=profile.pk,
            changes={"approval_status": {"before": old_status, "after": CollectorProfile.ApprovalStatus.SUSPENDED}},
        )

        # 2. Assign Back Office role group(s)
        all_bo_groups = get_backoffice_groups_queryset()
        valid_groups = all_bo_groups.filter(pk__in=[int(gid) for gid in role_group_ids])
        if not valid_groups.exists():
            raise ValidationError("At least one valid Back Office role group must be assigned.")

        user.groups.add(*valid_groups)

        # 3. Client access scope if selected
        if selected_client_ids:
            desired_cids = {int(cid) for cid in selected_client_ids}
            active_clients = Client.objects.filter(pk__in=desired_cids, is_active=True)
            valid_active_cids = set(active_clients.values_list("pk", flat=True))
            for client in active_clients:
                access, created = ClientAccess.objects.get_or_create(
                    user=user, client=client, defaults={"is_active": True}
                )
                if not created and not access.is_active:
                    access.is_active = True
                    access.save(update_fields=["is_active", "updated_at"])

            ClientAccess.objects.filter(user=user, is_active=True).exclude(
                client_id__in=valid_active_cids
            ).update(is_active=False)

        # 4. Record conversion audit event
        append_audit_event(
            actor=actor,
            action="system_admin_collector_converted",
            entity_type="User",
            entity_id=user.pk,
            changes={
                "collector_status": CollectorProfile.ApprovalStatus.SUSPENDED,
                "role_group_count": valid_groups.count(),
            },
        )

        return user


def update_user_account(
    *,
    actor,
    user_id,
    is_active,
    selected_client_ids,
    selected_permission_ids,
    collector_status=None,
    create_collector_profile=False,
    new_password=None,
):
    """Legacy update method for non-superuser accounts. Preserved for backwards compatibility."""
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=user_id)
        if user.is_superuser:
            raise PermissionDenied("Superusers are read-only system accounts and cannot be modified.")

        old_active = user.is_active
        user.is_active = bool(is_active)

        # 1. Password reset
        if new_password:
            user.set_password(new_password)
            append_audit_event(
                actor=actor,
                action="system_admin_password_reset",
                entity_type="User",
                entity_id=user.pk,
                changes={"password_reset": True},
            )

        # 2. Direct Back Office permissions
        available_bo_perms = get_available_backoffice_permissions()
        available_bo_perm_ids = {p.pk for p in available_bo_perms}
        current_bo_perm_ids = set(
            user.user_permissions.filter(pk__in=available_bo_perm_ids).values_list("pk", flat=True)
        )
        desired_bo_perm_ids = {
            int(pid) for pid in selected_permission_ids if int(pid) in available_bo_perm_ids
        }

        to_remove = current_bo_perm_ids - desired_bo_perm_ids
        to_add = desired_bo_perm_ids - current_bo_perm_ids

        if to_remove:
            user.user_permissions.remove(*to_remove)
        if to_add:
            user.user_permissions.add(*to_add)

        old_bo_count = len(current_bo_perm_ids)
        new_bo_count = len(desired_bo_perm_ids)

        # 3. Client Access
        old_client_count = user.client_accesses.filter(is_active=True).count()
        desired_cids = {int(cid) for cid in selected_client_ids}
        active_clients = Client.objects.filter(pk__in=desired_cids, is_active=True)
        valid_active_cids = set(active_clients.values_list("pk", flat=True))

        for client in active_clients:
            access, created = ClientAccess.objects.get_or_create(
                user=user, client=client, defaults={"is_active": True}
            )
            if not created and not access.is_active:
                access.is_active = True
                access.save(update_fields=["is_active", "updated_at"])

        ClientAccess.objects.filter(user=user, is_active=True).exclude(
            client_id__in=valid_active_cids
        ).update(is_active=False)

        new_client_count = user.client_accesses.filter(is_active=True).count()

        # 4. Collector Profile
        profile_created = False
        profile = CollectorProfile.objects.filter(user=user).first()
        if profile:
            if collector_status and collector_status != profile.approval_status:
                old_status = profile.approval_status
                if collector_status == CollectorProfile.ApprovalStatus.APPROVED:
                    profile.approved_by = actor
                    profile.approved_at = timezone.now()
                profile.approval_status = collector_status
                profile.save()
                append_audit_event(
                    actor=actor,
                    action="system_admin_collector_status_changed",
                    entity_type="CollectorProfile",
                    entity_id=profile.pk,
                    changes={"approval_status": {"before": old_status, "after": collector_status}},
                )
        elif create_collector_profile:
            CollectorProfile.objects.create(
                user=user,
                approval_status=CollectorProfile.ApprovalStatus.APPROVED,
                availability=CollectorProfile.Availability.OFFLINE,
            )
            profile_created = True

        # 5. User update audit
        user.save()
        user_changes = {}
        if old_active != user.is_active:
            user_changes["is_active"] = {"before": old_active, "after": user.is_active}
        if old_bo_count != new_bo_count:
            user_changes["backoffice_permission_count"] = {"before": old_bo_count, "after": new_bo_count}
        if old_client_count != new_client_count:
            user_changes["active_client_access_count"] = {"before": old_client_count, "after": new_client_count}
        if profile_created:
            user_changes["collector_profile_created"] = True

        if user_changes:
            append_audit_event(
                actor=actor,
                action="system_admin_user_updated",
                entity_type="User",
                entity_id=user.pk,
                changes=user_changes,
            )

        return user


def create_client_record(*, actor, code, name, is_active=True):
    """Create a new Client with only code, name, and is_active. Default model values for other fields."""
    with transaction.atomic():
        client = Client(code=code, name=name, is_active=is_active)
        client.full_clean()
        client.save()
        append_audit_event(
            actor=actor,
            action="system_admin_client_created",
            entity_type="Client",
            entity_id=client.pk,
            changes={"code": client.code, "is_active": client.is_active},
        )
        return client


def update_client_record(*, actor, client_id, code, name, is_active):
    """Update a Client's code, name, and active status without modifying policy fields."""
    with transaction.atomic():
        client = Client.objects.select_for_update().get(pk=client_id)
        old_code = client.code
        old_active = client.is_active
        previous_name = client.name

        client.code = code
        client.name = name
        client.is_active = bool(is_active)
        client.full_clean()
        client.save()

        if not client.is_active:
            ClientAccess.objects.filter(client=client, is_active=True).update(is_active=False)

        changes = {}
        if old_code != client.code:
            changes["code"] = {"before": old_code, "after": client.code}
        if old_active != client.is_active:
            changes["is_active"] = {"before": old_active, "after": client.is_active}
        if previous_name != client.name:
            changes["name_changed"] = True

        if changes:
            append_audit_event(
                actor=actor,
                action="system_admin_client_updated",
                entity_type="Client",
                entity_id=client.pk,
                changes=changes,
            )

        return client


def resend_collector_invitation(*, actor, user_id, request=None):
    """Resend a Collector invitation, immediately revoking any prior active invitations.
    
    Generates a fresh token hash and sends a new invitation email.
    """
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=user_id)
        if user.is_superuser:
            raise PermissionDenied("Superusers cannot receive collector invitations.")
        if user.collector_account_disabled:
            raise ValidationError("Activate the account before resending the setup invitation.")

        if user.is_collector_setup_complete:
            raise ValidationError(
                "Collector has already completed initial account setup. Use 'Send Password Reset Email' instead."
            )

        profile = getattr(user, "collector_profile", None)
        if profile and profile.approval_status == CollectorProfile.ApprovalStatus.OFFBOARDED:
            raise ValidationError("Cannot resend invitation to an offboarded collector.")

        # Invalidate/revoke any prior active invitations
        prior_active = CollectorInvitation.objects.filter(
            user=user, status=CollectorInvitation.Status.SENT
        )
        for old_inv in prior_active:
            old_inv.status = CollectorInvitation.Status.REVOKED
            old_inv.revoked_at = timezone.now()
            old_inv.save(update_fields=["status", "revoked_at", "updated_at"])

        raw_token = generate_invitation_token()
        token_hash = hash_invitation_token(raw_token)
        expires_at = timezone.now() + timedelta(hours=INVITATION_EXPIRY_HOURS)

        new_invitation = CollectorInvitation.objects.create(
            user=user,
            email=user.email,
            token_hash=token_hash,
            status=CollectorInvitation.Status.SENT,
            expires_at=expires_at,
            sent_at=timezone.now(),
            created_by=actor,
        )

        send_invitation_email(new_invitation, raw_token, request=request)

        append_audit_event(
            actor=actor,
            action="collector_invitation_resent",
            entity_type="CollectorInvitation",
            entity_id=new_invitation.pk,
            changes={"status": "sent", "email": user.email},
        )
        return new_invitation, raw_token


def revoke_collector_invitation(*, actor, user_id):
    """Revoke all active collector invitations for a user immediately."""
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=user_id)
        active_invitations = CollectorInvitation.objects.filter(
            user=user, status=CollectorInvitation.Status.SENT
        )
        revoked_count = 0
        for inv in active_invitations:
            inv.status = CollectorInvitation.Status.REVOKED
            inv.revoked_at = timezone.now()
            inv.save(update_fields=["status", "revoked_at", "updated_at"])
            append_audit_event(
                actor=actor,
                action="collector_invitation_revoked",
                entity_type="CollectorInvitation",
                entity_id=inv.pk,
                changes={"status": "revoked"},
            )
            revoked_count += 1
        return revoked_count


def accept_collector_invitation(*, raw_token, password):
    """Accept an invitation: validate token, set password, and activate account.
    
    Safe failure modes for expired, revoked, reused, and invalid tokens.
    """
    token_hash = hash_invitation_token(raw_token)
    with transaction.atomic():
        invitation = (
            CollectorInvitation.objects.select_for_update()
            .select_related("user")
            .filter(token_hash=token_hash)
            .first()
        )
        if not invitation:
            raise ValidationError("This invitation link is invalid. Please ask your administrator to send a new invitation.")

        if invitation.status == CollectorInvitation.Status.REVOKED:
            raise ValidationError("This invitation has been revoked. Please ask your administrator to send a new invitation.")

        if invitation.status == CollectorInvitation.Status.ACCEPTED:
            raise ValidationError("This link has already been used. Please sign in with your password.")

        if invitation.is_expired:
            if invitation.status != CollectorInvitation.Status.EXPIRED:
                invitation.status = CollectorInvitation.Status.EXPIRED
                invitation.save(update_fields=["status", "updated_at"])
            raise ValidationError("This link has expired. Please ask your administrator to send a new invitation.")

        if invitation.status != CollectorInvitation.Status.SENT:
            raise ValidationError("This invitation link is no longer valid. Please ask your administrator to send a new invitation.")

        user = invitation.user
        user = User.objects.select_for_update().get(pk=user.pk)
        if user.collector_account_disabled:
            raise ValidationError("This account is deactivated. Contact your administrator.")
        validate_password(password, user=user)

        user.set_password(password)
        user.is_active = True
        now = timezone.now()
        user.email_verified_at = now
        user.account_activated_at = now
        user.save(update_fields=["password", "is_active", "email_verified_at", "account_activated_at"])

        invitation.status = CollectorInvitation.Status.ACCEPTED
        invitation.accepted_at = now
        invitation.save(update_fields=["status", "accepted_at", "updated_at"])

        append_audit_event(
            actor=user,
            action="collector_invitation_accepted",
            entity_type="CollectorInvitation",
            entity_id=invitation.pk,
            changes={"status": "accepted"},
        )
        return user


def approve_collector_profile(*, actor, user_id):
    """Approve a collector profile, granting Collector Portal access if account is active."""
    with transaction.atomic():
        profile = CollectorProfile.objects.select_for_update().select_related("user").get(user_id=user_id)
        from apps.user.models import has_backoffice_role
        if has_backoffice_role(profile.user):
            raise ValidationError(
                "Cannot approve collector profile for a user assigned to Back Office role groups. Active roles must be mutually exclusive."
            )
        old_status = profile.approval_status
        if old_status == CollectorProfile.ApprovalStatus.APPROVED:
            return profile

        profile.approval_status = CollectorProfile.ApprovalStatus.APPROVED
        profile.approved_by = actor
        profile.approved_at = timezone.now()
        profile.save()

        append_audit_event(
            actor=actor,
            action="system_admin_collector_status_changed",
            entity_type="CollectorProfile",
            entity_id=profile.pk,
            changes={"approval_status": {"before": old_status, "after": "approved"}},
        )
        return profile


def reject_collector_profile(*, actor, user_id):
    """Reject a collector profile."""
    with transaction.atomic():
        profile = CollectorProfile.objects.select_for_update().get(user_id=user_id)
        old_status = profile.approval_status
        if old_status == CollectorProfile.ApprovalStatus.REJECTED:
            return profile

        profile.approval_status = CollectorProfile.ApprovalStatus.REJECTED
        profile.save()

        append_audit_event(
            actor=actor,
            action="system_admin_collector_status_changed",
            entity_type="CollectorProfile",
            entity_id=profile.pk,
            changes={"approval_status": {"before": old_status, "after": "rejected"}},
        )
        return profile


def suspend_collector_profile(*, actor, user_id, reason=""):
    """Suspend a collector profile. Requires a non-empty reason."""
    clean_reason = (reason or "").strip()
    if not clean_reason:
        raise ValidationError("A reason is required to suspend a collector.")

    with transaction.atomic():
        profile = CollectorProfile.objects.select_for_update().select_related("user").get(user_id=user_id)
        if profile.approval_status == CollectorProfile.ApprovalStatus.OFFBOARDED:
            raise ValidationError("An offboarded collector cannot be suspended.")

        old_status = profile.approval_status
        profile.approval_status = CollectorProfile.ApprovalStatus.SUSPENDED
        profile.availability = CollectorProfile.Availability.OFFLINE
        profile.save()

        CollectorDisciplinaryRecord.objects.create(
            collector=profile,
            action_type=CollectorDisciplinaryRecord.ActionType.SUSPENSION,
            reason=clean_reason,
            issued_by=actor,
        )

        append_audit_event(
            actor=actor,
            action="collector_suspended",
            entity_type="CollectorProfile",
            entity_id=profile.pk,
            changes={"approval_status": "suspended", "reason": clean_reason[:100]},
        )
        append_audit_event(
            actor=actor,
            action="system_admin_collector_status_changed",
            entity_type="CollectorProfile",
            entity_id=profile.pk,
            changes={"approval_status": {"before": old_status, "after": "suspended"}, "reason": clean_reason[:100]},
        )
        return profile


def reinstate_collector_profile(*, actor, user_id, note=""):
    """Reinstate a suspended collector profile to active approved standing. Optional note."""
    clean_note = (note or "").strip()
    with transaction.atomic():
        profile = CollectorProfile.objects.select_for_update().select_related("user").get(user_id=user_id)
        if profile.approval_status == CollectorProfile.ApprovalStatus.OFFBOARDED:
            raise ValidationError("An offboarded collector cannot be reinstated.")
        if profile.approval_status != CollectorProfile.ApprovalStatus.SUSPENDED:
            raise ValidationError("Only a suspended collector can be reinstated.")

        from apps.user.models import has_backoffice_role
        if has_backoffice_role(profile.user):
            raise ValidationError("Cannot reinstate collector profile for a user assigned to Back Office role groups.")

        profile.approval_status = CollectorProfile.ApprovalStatus.APPROVED
        profile.save(update_fields=["approval_status", "updated_at"])

        profile.user.is_active = True
        profile.user.save(update_fields=["is_active"])

        CollectorDisciplinaryRecord.objects.create(
            collector=profile,
            action_type=CollectorDisciplinaryRecord.ActionType.REINSTATEMENT,
            reason=clean_note,
            issued_by=actor,
        )

        append_audit_event(
            actor=actor,
            action="collector_reinstated",
            entity_type="CollectorProfile",
            entity_id=profile.pk,
            changes={"approval_status": "approved", "note": clean_note[:100]},
        )
        return profile


def offboard_collector_profile(*, actor, user_id, reason=""):
    """Offboard a collector profile. Requires a reason. Puts collector into read-only mode."""
    clean_reason = (reason or "").strip()
    if not clean_reason:
        raise ValidationError("A reason is required to offboard a collector.")

    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=user_id)
        if user.is_superuser:
            raise PermissionDenied("Superusers cannot be offboarded.")

        profile = CollectorProfile.objects.select_for_update().get(user=user)
        if profile.approval_status == CollectorProfile.ApprovalStatus.OFFBOARDED:
            return profile

        profile.approval_status = CollectorProfile.ApprovalStatus.OFFBOARDED
        profile.availability = CollectorProfile.Availability.OFFLINE
        profile.save()

        user.is_active = False
        user.save(update_fields=["is_active"])

        # Revoke any active invitations
        CollectorInvitation.objects.filter(
            user=user, status=CollectorInvitation.Status.SENT
        ).update(status=CollectorInvitation.Status.REVOKED, revoked_at=timezone.now())

        # Deactivate client access
        ClientAccess.objects.filter(user=user, is_active=True).update(is_active=False)

        CollectorDisciplinaryRecord.objects.create(
            collector=profile,
            action_type=CollectorDisciplinaryRecord.ActionType.OFFBOARDING,
            reason=clean_reason,
            issued_by=actor,
        )

        append_audit_event(
            actor=actor,
            action="collector_offboarded",
            entity_type="CollectorProfile",
            entity_id=profile.pk,
            changes={"approval_status": "offboarded", "reason": clean_reason[:100]},
        )
        return profile


def issue_collector_warning(*, actor, user_id, level="warning", reason=""):
    """Issue a formal warning or final warning to an active collector. Requires a reason."""
    clean_reason = (reason or "").strip()
    if not clean_reason:
        raise ValidationError("A reason is required to issue a warning.")
    if level not in ["warning", "final_warning"]:
        raise ValidationError("Invalid warning level.")

    with transaction.atomic():
        profile = CollectorProfile.objects.select_for_update().select_related("user").get(user_id=user_id)
        if profile.approval_status == CollectorProfile.ApprovalStatus.OFFBOARDED:
            raise ValidationError("Cannot issue a warning to an offboarded collector.")

        action_type = (
            CollectorDisciplinaryRecord.ActionType.FINAL_WARNING
            if level == "final_warning"
            else CollectorDisciplinaryRecord.ActionType.WARNING
        )

        record = CollectorDisciplinaryRecord.objects.create(
            collector=profile,
            action_type=action_type,
            reason=clean_reason,
            issued_by=actor,
        )

        append_audit_event(
            actor=actor,
            action="collector_warning_issued",
            entity_type="CollectorProfile",
            entity_id=profile.pk,
            changes={"level": level, "reason": clean_reason[:100]},
        )
        return record


def send_collector_password_reset_email(*, actor, user_id, request=None):
    """Send a secure one-time password reset link to an activated collector.
    
    Available only for collectors with an activated account who are not offboarded.
    Never exposes passwords or tokens in response/logs.
    """
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=user_id)
        if user.is_superuser:
            raise PermissionDenied("Cannot reset password for superuser accounts.")

        profile = getattr(user, "collector_profile", None)
        if not profile:
            raise ValidationError("This user is not a collector.")

        if profile.approval_status == CollectorProfile.ApprovalStatus.OFFBOARDED:
            raise ValidationError("Password reset cannot be sent to an offboarded collector.")

        if not user.is_collector_setup_complete:
            raise ValidationError(
                "Password reset is only available for collectors who have completed account setup. "
                "Use 'Resend Setup Invitation' instead."
            )

        # Invalidate prior active invitations/reset tokens
        CollectorInvitation.objects.filter(
            user=user, status=CollectorInvitation.Status.SENT
        ).update(status=CollectorInvitation.Status.REVOKED, revoked_at=timezone.now())

        raw_token = generate_invitation_token()
        token_hash = hash_invitation_token(raw_token)
        expires_at = timezone.now() + timedelta(hours=INVITATION_EXPIRY_HOURS)

        invitation = CollectorInvitation.objects.create(
            user=user,
            email=user.email,
            token_hash=token_hash,
            status=CollectorInvitation.Status.SENT,
            expires_at=expires_at,
            sent_at=timezone.now(),
            created_by=actor,
        )

        display_name = user.get_full_name() or user.username
        if request is not None:
            reset_url = request.build_absolute_uri(
                reverse("collector_invitation_setup", kwargs={"token": raw_token})
            )
        else:
            path = reverse("collector_invitation_setup", kwargs={"token": raw_token})
            reset_url = f"http://localhost:8000{path}"

        subject = "Reset your FiCol Collector password"
        message = (
            f"Hello {display_name},\n\n"
            f"A password reset has been requested for your FiCol Collector account.\n\n"
            f"Click the link below to set a new password:\n\n"
            f"{reset_url}\n\n"
            f"Security Notice:\n"
            f"- This link is valid for {INVITATION_EXPIRY_HOURS} hours.\n"
            f"- This link is one-time use only.\n"
            f"- Never share this link with anyone.\n\n"
            f"If you did not request this, please contact your administrator immediately.\n\n"
            f"Regards,\n"
            f"FiCol Security Team"
        )
        from_email = getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@ficol.local")
        send_mail(
            subject=subject,
            message=message,
            from_email=from_email,
            recipient_list=[user.email],
            fail_silently=False,
        )

        append_audit_event(
            actor=actor,
            action="collector_password_reset_sent",
            entity_type="User",
            entity_id=user.pk,
            changes={"status": "sent", "email": user.email},
        )
        return invitation


