from django.contrib.auth.models import Group, Permission
from django.db import transaction

ROLE_OPERATIONS = "Back Office – Operations"
ROLE_SUPERVISOR = "Back Office – Collections Supervisor"
ROLE_FINANCE = "Back Office – Finance"
ROLE_SYSTEM_ADMIN = "Back Office – System Administrator"

# Also support ASCII hyphen variants for tolerance
BACK_OFFICE_ROLE_GROUPS = {
    ROLE_OPERATIONS: [
        "accounts.validate_account_information",
        "activities.change_followuptask",
        "accounts.change_account_status",
        "activities.change_fieldactivity",
    ],
    ROLE_SUPERVISOR: [
        "user.view_collector_monitoring",
        "user.approve_collector",
        "assignments.manage_account_assignments",
        "assignments.review_release_requests",
    ],
    ROLE_FINANCE: [
        "collection.review_commissions",
        "collection.manage_payouts",
        "collection.change_payment",
        "collection.manage_earning_rules",
        "collection.manage_point_rules",
    ],
    ROLE_SYSTEM_ADMIN: [
        "user.add_user",
        "user.change_user",
        "user.view_user",
        "user.view_collector_monitoring",
        "auth.view_group",
        "accounts.view_client",
        "accounts.change_client",
        "accounts.add_client",
    ],
}

PRIMARY_ROLE_NAMES = list(BACK_OFFICE_ROLE_GROUPS.keys())


def get_backoffice_role_group_names():
    """Return all recognized Back Office group name variations (en-dash and hyphen)."""
    from .backoffice_modules import module_group_names
    names = set(PRIMARY_ROLE_NAMES) | module_group_names()
    for name in PRIMARY_ROLE_NAMES:
        names.add(name.replace("–", "-"))
    return names


def sync_backoffice_roles():
    """Safe, idempotent synchronization of initial Back Office groups and their permissions."""
    created_or_updated = {}
    with transaction.atomic():
        for group_name, perm_strings in BACK_OFFICE_ROLE_GROUPS.items():
            group, _ = Group.objects.get_or_create(name=group_name)
            
            perms_to_add = []
            for perm_str in perm_strings:
                app_label, codename = perm_str.split(".", 1)
                p = Permission.objects.filter(
                    content_type__app_label=app_label, codename=codename
                ).first()
                if p:
                    perms_to_add.append(p)
            
            # Idempotently add permissions without removing any extra custom permissions
            if perms_to_add:
                group.permissions.add(*perms_to_add)
            
            created_or_updated[group_name] = group
            
    return created_or_updated


def get_backoffice_groups_queryset():
    """Return queryset of all groups that represent Back Office roles."""
    names = get_backoffice_role_group_names()
    return Group.objects.filter(name__in=names).order_by("name")


def get_user_role_info(user):
    """Resolve active application role and portal type for UI display."""
    if not getattr(user, "is_authenticated", False):
        return {"role": "Anonymous", "portal": "None", "badge": "secondary"}
    
    is_active = getattr(user, "is_active", False)
    is_su = getattr(user, "is_superuser", False)
    
    if is_su:
        return {
            "role": "Superuser",
            "portal": "System Admin Portal" if is_active else "None (Inactive)",
            "badge": "system",
            "is_superuser": True,
            "is_backoffice": True,
            "is_collector": False,
        }
    
    # Check Back Office group assignment
    bo_group_names = get_backoffice_role_group_names()
    user_groups = []
    if hasattr(user, "groups"):
        if callable(getattr(user.groups, "all", None)):
            user_groups = list(user.groups.all())
        else:
            user_groups = list(user.groups)
            
    bo_roles = [
        getattr(g, "name", str(g))
        for g in user_groups
        if getattr(g, "name", str(g)) in bo_group_names or getattr(g, "name", str(g)).startswith("Back Office")
    ]
    
    has_collector_profile = False
    collector_status = None
    if hasattr(user, "collector_profile"):
        try:
            profile = user.collector_profile
            if profile is not None:
                has_collector_profile = True
                collector_status = getattr(profile, "approval_status", None)
        except Exception:
            has_collector_profile = False
            
    if bo_roles:
        role_label = ", ".join(bo_roles)
        portal_label = "Back Office Portal" if is_active else "None (Inactive)"
        return {
            "role": role_label,
            "portal": portal_label,
            "badge": "primary",
            "is_superuser": False,
            "is_backoffice": True,
            "is_collector": has_collector_profile,
            "collector_status": collector_status,
        }
        
    if has_collector_profile:
        status_display = collector_status.capitalize() if collector_status else "Unknown"
        portal_label = (
            "Collector Portal"
            if (is_active and collector_status == "approved")
            else f"None ({status_display})"
        )
        return {
            "role": f"Collector ({status_display})",
            "portal": portal_label,
            "badge": "info" if collector_status == "approved" else "warning",
            "is_superuser": False,
            "is_backoffice": False,
            "is_collector": True,
            "collector_status": collector_status,
        }
        
    return {
        "role": "Unassigned",
        "portal": "None",
        "badge": "secondary",
        "is_superuser": False,
        "is_backoffice": False,
        "is_collector": False,
    }
