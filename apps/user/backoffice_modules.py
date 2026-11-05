"""Sidebar modules and the complete transaction permissions each grants."""
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import ValidationError


MODULES = (
    ('collectors', 'Collector Monitoring', 'View collector availability, workload, locations, and field activity.', ('user.view_collector_monitoring',)),
    ('visits', 'Visit Reviews', 'View submitted visits and evidence, and approve or reject field reports.', ('activities.change_fieldactivity',)),
    ('followups', 'Follow-up Scheduling', 'View follow-up requests and schedule or review follow-up tasks.', ('activities.change_followuptask',)),
    ('payments', 'Payment Verifications', 'View, verify, and reject reported payments for authorized clients.', ('collection.change_payment',)),
    ('releases', 'Release Requests', 'View, approve, and reject requests to release assigned accounts.', ('assignments.review_release_requests',)),
    ('assignments', 'Account Assignments', 'View assignments and assign or reassign accounts to eligible collectors.', ('assignments.manage_account_assignments',)),
    ('information', 'Information Validation', 'Review and approve or reject proposed contact and address information.', ('accounts.validate_account_information',)),
    ('earning_rules', 'Earning Rates', 'View and configure client earning rates and their versions.', ('collection.manage_earning_rules',)),
    ('point_rules', 'Point Rules', 'View and configure verified activity points and incentive rules.', ('collection.manage_point_rules',)),
    ('commissions', 'Commissions', 'View, approve, and reject collector commissions for authorized clients.', ('collection.review_commissions',)),
    ('payouts', 'Payouts', 'View and create payout batches, approve payouts, and record payment settlement.', ('collection.manage_payouts',)),
)
MODULE_MAP = {key: (label, description, permissions) for key, label, description, permissions in MODULES}

# Presentation grouping reuses the existing labels, descriptions, and access keys.
PERMISSION_GROUPS = tuple(
    dict(key=key, label=MODULE_MAP[key][0], description=MODULE_MAP[key][1], group=group)
    for group, keys in (
        ('Collector Operations', ('collectors', 'visits', 'followups', 'assignments')),
        ('Verification', ('payments', 'releases', 'information')),
        ('Finance', ('earning_rules', 'commissions', 'point_rules', 'payouts')),
    )
    for key in keys
)


def module_group_names():
    return {f'Back Office / {label}' for _, label, _, _ in MODULES}


def selected_modules(user):
    return [key for key, _, _, permissions in MODULES if any(user.has_perm(p) for p in permissions)]


def apply_modules(user, keys, actor=None):
    """Called inside account services' transactions; preserve unrelated access."""
    from apps.user.roles import get_backoffice_groups_queryset
    keys = set(keys)
    if keys - MODULE_MAP.keys():
        raise ValidationError('Select valid Back Office modules.')
    if not keys:
        raise ValidationError('Select at least one Back Office module.')
    managed = {p for _, _, _, permissions in MODULES for p in permissions}
    previous_modules = set(selected_modules(user))
    old_groups = list(get_backoffice_groups_queryset().filter(user=user))
    # Preserve custom permissions outside the module catalog when replacing templates.
    for group in old_groups:
        for permission in group.permissions.select_related('content_type'):
            if f'{permission.content_type.app_label}.{permission.codename}' not in managed:
                user.user_permissions.add(permission)
    managed_ids = []
    for permission in managed:
        app, code = permission.split('.', 1)
        managed_ids.extend(Permission.objects.filter(content_type__app_label=app, codename=code).values_list('pk', flat=True))
    user.user_permissions.remove(*managed_ids)
    user.groups.remove(*old_groups)
    for key in keys:
        label, _, codes = MODULE_MAP[key]
        group, _ = Group.objects.get_or_create(name=f'Back Office / {label}')
        permissions = []
        for code in codes:
            app, codename = code.split('.', 1)
            permission = Permission.objects.filter(content_type__app_label=app, codename=codename).first()
            if not permission:
                raise ValidationError(f'Missing permission for {label}. Apply database migrations first.')
            permissions.append(permission)
        group.permissions.set(permissions)
        user.groups.add(group)
    for cache in ('_perm_cache', '_group_perm_cache', '_user_perm_cache'):
        user.__dict__.pop(cache, None)
    if actor:
        from apps.audit.services import append_audit_event
        for key in previous_modules ^ keys:
            append_audit_event(actor=actor, action='backoffice_module_access_changed',
                               entity_type='User', entity_id=user.pk,
                               changes={'module': key, 'enabled': key in keys})
