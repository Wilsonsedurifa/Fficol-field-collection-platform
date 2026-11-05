"""Read-only user-list filtering and presentation; account actions are unchanged."""
from urllib.parse import urlencode

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.paginator import Paginator
from django.db.models import BooleanField, Case, CharField, Count, Exists, OuterRef, Q, Value, When
from django.db.models.functions import Concat
from django.urls import reverse

from apps.user.models import BackOfficeInvitation, CollectorInvitation, CollectorProfile
from apps.user.roles import get_backoffice_role_group_names
from ..dashboard.services import backoffice_filter


def users_queryset():
    # Exists keeps invitation/group joins from multiplying users in the counts.
    queryset = get_user_model().objects.annotate(
        has_collector_invitation=Exists(CollectorInvitation.objects.filter(user_id=OuterRef('pk'))),
        has_backoffice_invitation=Exists(BackOfficeInvitation.objects.filter(user_id=OuterRef('pk'))),
        is_backoffice_role=Exists(Group.objects.filter(user=OuterRef('pk')).filter(
            Q(name__in=get_backoffice_role_group_names()) | Q(name__startswith='Back Office'))),
    ).annotate(
        role_key=Case(When(is_superuser=True, then=Value('admin')),
                      When(is_backoffice_role=True, then=Value('backoffice')),
                      When(collector_profile__isnull=False, then=Value('collector')),
                      default=Value('user'), output_field=CharField()),
        # Preserve the existing usable-password fallback for legacy collectors
        # who have never received an invitation.
        setup_complete=Case(
            When(Q(account_activated_at__isnull=False) |
                 (~Q(password__startswith='!') & (Q(is_active=True) | Q(has_collector_invitation=False))), then=Value(True)),
            default=Value(False), output_field=BooleanField()),
    ).annotate(
        admin_account_disabled=Case(
            When(Q(collector_account_disabled=True) | Q(is_active=False, setup_complete=True), then=Value(True)),
            default=Value(False), output_field=BooleanField()),
    ).annotate(
        status_key=Case(
            When(role_key='collector', admin_account_disabled=True, then=Value('deactivated')),
            When(role_key='collector', collector_profile__approval_status='suspended', then=Value('suspended')),
            When(role_key='collector', collector_profile__approval_status='offboarded', then=Value('deactivated')),
            When(role_key='collector', setup_complete=False, then=Value('awaiting')),
            When(~Q(role_key='admin'), is_active=False, account_activated_at__isnull=True,
                 has_backoffice_invitation=True, then=Value('awaiting')),
            When(is_active=True, then=Value('active')),
            default=Value('deactivated'), output_field=CharField()),
    )
    return queryset


def present_user(user):
    user.display_name = user.get_full_name().strip() or user.username
    words = user.get_full_name().split()
    user.initials = (words[0][0] + words[-1][0] if len(words) > 1 else words[0][0] if words else user.username[:2]).upper()
    user.role_label = {'admin': 'System Admin', 'backoffice': 'Back Office', 'collector': 'Collector', 'user': 'User'}[user.role_key]
    user.status_label = {'active': 'Active', 'awaiting': 'Awaiting setup', 'suspended': 'Suspended', 'deactivated': 'Deactivated'}[user.status_key]
    user.admin_setup_complete = user.setup_complete
    profile = getattr(user, 'collector_profile', None)
    user.is_offboarded_collector = bool(profile and profile.approval_status == 'offboarded')
    user.can_resend_invitation = user.role_key == 'collector' and not user.setup_complete and not user.is_offboarded_collector and not user.admin_account_disabled
    return user


def get_users_context(params):
    q = params.get('q', '').strip()
    role = params.get('role', '')
    role = role if role in {'admin', 'collector', 'backoffice'} else ''
    status = params.get('status', '')
    status = status if status == 'awaiting' else ''
    # Preserve existing dashboard drilldown URLs.
    if params.get('setup') == 'pending':
        status = 'awaiting'
    collector_status = params.get('collector_status', '')
    collector_status = collector_status if collector_status in dict(CollectorProfile.ApprovalStatus.choices) else ''
    base = users_queryset()
    counts = base.aggregate(total=Count('pk'), active=Count('pk', filter=Q(status_key='active')),
                            awaiting=Count('pk', filter=Q(status_key='awaiting')),
                            collectors=Count('pk', filter=Q(role_key='collector')),
                            admins=Count('pk', filter=Q(role_key='admin')))
    filtered = base
    if q:
        filtered = filtered.annotate(full_name=Concat('first_name', Value(' '), 'last_name')).filter(
            Q(username__icontains=q) | Q(full_name__icontains=q) | Q(email__icontains=q))
    if role == 'backoffice':
        filtered = filtered.filter(backoffice_filter()).distinct()
    elif role:
        filtered = filtered.filter(role_key=role)
    if status:
        filtered = filtered.filter(status_key=status)
    if collector_status:
        filtered = filtered.filter(collector_profile__approval_status=collector_status)
    page = Paginator(filtered.select_related('collector_profile').order_by('username', 'pk'), 20).get_page(params.get('page'))
    page.object_list = [present_user(user) for user in page.object_list]
    current = {key: value for key, value in [('q', q), ('role', role), ('status', status), ('collector_status', collector_status)] if value}
    path = reverse('system_admin_users')

    def link(values):
        query = urlencode(values)
        return path + ('?' + query if query else '')

    tabs = []
    for label, count, updates in [('All', counts['total'], {'role': '', 'status': ''}),
                                  ('System admin', counts['admins'], {'role': 'admin'}),
                                  ('Collector', counts['collectors'], {'role': 'collector'}),
                                  ('Awaiting setup', counts['awaiting'], {'status': 'awaiting'})]:
        values = dict(current, **updates)
        if 'role' in updates:
            values.pop('collector_status', None)
        active = (not role and not status and not collector_status) if label == 'All' else (role == updates['role'] if 'role' in updates else status == 'awaiting')
        tabs.append(dict(label=label, count=count, url=link({k: v for k, v in values.items() if v}), active=active))
    return dict(admin_section='users', page_obj=page, search_query=q, selected_role=role,
                selected_status=status, collector_status=collector_status, counts=counts, filter_tabs=tabs,
                summary_cards=[dict(label=label, value=counts[key], tone=tone) for label, key, tone in
                               [('Total users', 'total', 'neutral'), ('Active', 'active', 'success'),
                                ('Awaiting setup', 'awaiting', 'warning'), ('Collectors', 'collectors', 'neutral')]],
                previous_url=link(dict(current, page=page.previous_page_number())) if page.has_previous() else None,
                next_url=link(dict(current, page=page.next_page_number())) if page.has_next() else None)
