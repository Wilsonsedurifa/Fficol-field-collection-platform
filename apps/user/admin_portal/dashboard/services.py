"""Read-only System Admin dashboard queries and presentation data."""
from datetime import datetime, time, timedelta
from itertools import islice
from decimal import Decimal
from zoneinfo import ZoneInfo
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models import Case, CharField, Count, Exists, OuterRef, Q, Sum, Value, When
from django.db.models.functions import TruncDate
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from apps.accounts.models import Account, Client
from apps.activities.models import FieldActivity
from apps.assignments.models import AccountAssignment, AccountOffer
from apps.audit.models import AuditEvent
from apps.audit.presentation import ACTIONS
from apps.collection.models import Commission, Payment, PromiseToPay
from apps.user.invitations import INVITATION_EXPIRY_HOURS
from apps.user.models import BACK_OFFICE_PERMISSIONS, BackOfficeInvitation, CollectorInvitation, CollectorProfile
from apps.user.roles import get_backoffice_role_group_names

STAGES = (('available', 'Available'), ('offered', 'Offered'), ('assigned', 'Assigned'),
          ('contact', 'Contact attempt'), ('promise', 'Promise to pay'), ('resolved', 'Resolved'))
ACTION_LABELS = {code: data[0] for code, data in ACTIONS.items()}


def url(name, **params):
    try:
        path = reverse(name)
    except NoReverseMatch:
        return None
    return path + ('?' + urlencode(params) if params else '')


def backoffice_filter():
    condition = Q(groups__name__in=get_backoffice_role_group_names())
    for permission in BACK_OFFICE_PERMISSIONS:
        app, code = permission.split('.')
        condition |= Q(groups__permissions__content_type__app_label=app, groups__permissions__codename=code)
    return condition & Q(is_superuser=False)


def account_queryset(now):
    """Disjoint workflow buckets, prioritizing resolved, promise, then contact."""
    return Account.objects.annotate(
        has_assignment=Exists(AccountAssignment.objects.filter(account_id=OuterRef('pk'), status='active')),
        has_offer=Exists(AccountOffer.objects.filter(account_id=OuterRef('pk'), status='pending', expires_at__gt=now)),
        has_contact=Exists(FieldActivity.objects.filter(account_id=OuterRef('pk')).exclude(status='cancelled')),
        has_promise=Exists(PromiseToPay.objects.filter(activity__account_id=OuterRef('pk'), status='open')),
    ).annotate(stage=Case(
        When(status='resolved', then=Value('resolved')),
        When(status='open', has_promise=True, then=Value('promise')),
        When(status='open', has_contact=True, then=Value('contact')),
        When(status='open', has_assignment=True, then=Value('assigned')),
        When(status='open', has_offer=True, then=Value('offered')),
        When(status='open', then=Value('available')),
        default=Value('other'), output_field=CharField(),
    ))


def stale_accounts(queryset, now, days):
    cutoff = now - timedelta(days=days)
    recent = FieldActivity.objects.filter(account_id=OuterRef('pk'), occurred_at__gte=cutoff).exclude(status='cancelled')
    return queryset.annotate(recent_work=Exists(recent)).filter(status='open', created_at__lte=cutoff, recent_work=False)


def humanize_changes(changes):
    def display(value):
        if value is None:
            return 'None'
        if isinstance(value, bool):
            return 'Yes' if value else 'No'
        return str(value).replace('_', ' ').capitalize()
    parts = []
    for key, value in (changes or {}).items():
        label = key.replace('_', ' ').capitalize()
        if isinstance(value, dict) and 'before' in value and 'after' in value:
            parts.append(f'{label}: {display(value["before"])} → {display(value["after"])}')
        else:
            parts.append(f'{label}: {display(value)}')
    return '; '.join(parts) or 'No additional details'


DASHBOARD_TZ = ZoneInfo('Asia/Singapore')
ACTION_PRESENTATION = {code: (data[2], data[3], data[1]) for code, data in ACTIONS.items()}


def range_window(selected_range='7', now=None):
    now = now or timezone.now()
    key = selected_range if selected_range in {'today', '7', '30'} else '7'
    days = {'today': 1, '7': 7, '30': 30}[key]
    today = now.astimezone(DASHBOARD_TZ).date()
    dates = [today - timedelta(days=offset) for offset in reversed(range(days))]
    start = datetime.combine(dates[0], time.min, tzinfo=DASHBOARD_TZ)
    return dict(key=key, days=days, dates=dates, start=start, end=now,
                label={'today': 'Today', '7': 'Last 7 days', '30': 'Last 30 days'}[key])


def _iter_activity_groups(entries, window_minutes):
    current = None
    for event in entries:
        if (current and current['actor_id'] == event.actor_id
                and current['action'] == event.action
                and timedelta(0) <= current['end'] - event.created_at <= timedelta(minutes=window_minutes)):
            current['start'] = event.created_at
            current['count'] += 1
        else:
            if current:
                yield current
            current = dict(actor_id=event.actor_id, actor=event.actor.username if event.actor else 'System',
                           action=event.action, start=event.created_at, end=event.created_at,
                           count=1, record=f'{event.entity_type} #{event.entity_id}', changes=event.changes)
    if current:
        yield current


def group_activity(entries, window_minutes=30):
    """Pure grouping of newest-first entries by actor/action and total time span."""
    return list(_iter_activity_groups(entries, window_minutes))


def activity_presentation(group):
    action = group['action']
    fallback = ('✓', 'green') if action.endswith(('_accepted', '_approved', '_verified')) else (
        ('↗', 'blue') if action.endswith(('_created', '_sent', '_resent')) else (
            ('×', 'red') if action.endswith(('_revoked', '_rejected')) else ('•', 'amber')))
    icon, color, sentence = ACTION_PRESENTATION.get(
        action, (*fallback, '{actor}: ' + action.replace('_', ' ').lower()))
    if group['action'] == 'system_admin_collector_status_changed':
        change = group['changes'].get('approval_status', {})
        status = change.get('after') if isinstance(change, dict) else change
        if status in {'approved', 'rejected', 'suspended'}:
            verb = {'approved': 'enabled', 'rejected': 'rejected', 'suspended': 'suspended'}[status]
            icon, color = ('✓', 'green') if status == 'approved' else ('×', 'red') if status == 'rejected' else ('!', 'amber')
            sentence = '{actor} ' + verb + ' a collector'
    return dict(group, icon=icon, color=color, sentence=sentence.replace('{actor}', group['actor']))


def sparkline_points(values):
    maximum = max(values, default=0) or 1
    if len(values) == 1:
        y = 52 - float(values[0] / maximum) * 40
        return f'0,{y:.2f} 240,{y:.2f}'
    return ' '.join(f'{index * 240 / (len(values) - 1):.2f},{52 - float(value / maximum) * 40:.2f}'
                    for index, value in enumerate(values))


def get_dashboard_context(selected_range='7'):
    now = timezone.now()
    window = range_window(selected_range, now)
    User = get_user_model()
    invited = User.objects.annotate(
        collector_invited=Exists(CollectorInvitation.objects.filter(user_id=OuterRef('pk'))),
        backoffice_invited=Exists(BackOfficeInvitation.objects.filter(user_id=OuterRef('pk'))),
    ).filter(Q(collector_invited=True) | Q(backoffice_invited=True),
             is_active=False, account_activated_at__isnull=True)
    users = User.objects.aggregate(total=Count('pk'), active=Count('pk', filter=Q(is_active=True)))
    collectors = CollectorProfile.objects.aggregate(
        total=Count('pk'), approved=Count('pk', filter=Q(approval_status='approved')),
        active=Count('pk', filter=Q(approval_status='approved', user__is_active=True)),
        pending=Count('pk', filter=Q(approval_status='pending')),
        recent_pending=Count('pk', filter=Q(approval_status='pending', created_at__gte=window['start'], created_at__lte=now)),
        suspended=Count('pk', filter=Q(approval_status='suspended')),
    )
    clients = Client.objects.aggregate(total=Count('pk'), inactive=Count('pk', filter=Q(is_active=False)))
    accounts = account_queryset(now).aggregate(
        total=Count('pk'), worked=Count('pk', filter=Q(status='open', has_assignment=True)),
        **{key: Count('pk', filter=Q(stage=key)) for key, _ in STAGES},
        other=Count('pk', filter=Q(stage='other')),
    )
    payment_rows = list(Payment.objects.filter(
        status='verified', paid_at__gte=window['start'], paid_at__lte=now,
    ).annotate(day=TruncDate('paid_at', tzinfo=DASHBOARD_TZ)).values('day', 'account__currency')
                       .annotate(amount=Sum('amount')).order_by('day', 'account__currency'))
    daily_php = {day: Decimal('0') for day in window['dates']}
    totals = {}
    for payment in payment_rows:
        currency = payment['account__currency']
        totals[currency] = totals.get(currency, Decimal('0')) + payment['amount']
        if currency == 'PHP':
            daily_php[payment['day']] += payment['amount']
    php_total = totals.get('PHP', Decimal('0'))
    sparkline = list(daily_php.values())

    # Count actual transition evidence, never infer a status-change date from updated_at.
    transitions = AuditEvent.objects.filter(
        entity_type='CollectorProfile', created_at__gte=window['start'], created_at__lte=now,
        action='system_admin_collector_status_changed',
    ).aggregate(
        rejected=Count('entity_id', filter=Q(changes__approval_status__after='rejected'), distinct=True),
        suspended=Count('entity_id', filter=Q(changes__approval_status__after='suspended'), distinct=True),
    )
    # Window count keeps the attention total and the bounded preview in one query.
    from django.db.models import Window
    waiting = list(invited.select_related('collector_profile').annotate(
        waiting_total=Window(expression=Count('pk')),
    ).order_by('date_joined', 'pk')[:5])
    attention = []
    for user in waiting:
        profile = getattr(user, 'collector_profile', None)
        can_resend = bool(profile and profile.approval_status == 'approved'
                          and not user.collector_account_disabled and not user.has_usable_password())
        attention.append(dict(
            name=user.get_full_name() or user.username, email=user.email,
            resend_url=reverse('system_admin_collector_resend_invitation', args=[user.pk]) if can_resend else None,
            account_url=reverse('system_admin_collector_edit' if profile else 'system_admin_backoffice_user_edit', args=[user.pk]),
        ))

    # Read only enough sorted events to form eight complete groups, without an N+1 query.
    events = AuditEvent.objects.filter(created_at__gte=window['start'], created_at__lte=now).select_related('actor').order_by('-created_at', '-pk')
    activity = [activity_presentation(group) for group in
                islice(_iter_activity_groups(events.iterator(chunk_size=200), 30), 8)]

    pipeline = []
    offset = 0
    for key, label in (*STAGES, ('other', 'Other')):
        count = accounts[key]
        if count:
            width = count * 100 / accounts['total']
            pipeline.append(dict(key=key, label=label, count=count, x=f'{offset:.4f}', width=f'{width:.4f}'))
            offset += width
    records_url = 'system_admin_dashboard_records'
    cards = [
        dict(label='Accounts', value=accounts['total'], note=f'{accounts["available"]} available · {accounts["worked"]} assigned', url=url(records_url, kind='accounts')),
        dict(label='Collectors', value=collectors['total'], note=f'{collectors["active"]} active · {collectors["total"] - collectors["active"]} inactive', url=url('system_admin_users', role='collector')),
        dict(label='Users', value=users['total'], note=f'{users["active"]} active · {users["total"] - users["active"]} inactive', url=url('system_admin_users')),
        dict(label='Clients', value=clients['total'], note=f'{clients["total"] - clients["inactive"]} active · {clients["inactive"]} inactive', url=url('system_admin_clients')),
    ]
    return dict(
        admin_section='dashboard', updated_at=now.astimezone(DASHBOARD_TZ),
        selected_range=window['key'], range_label=window['label'],
        range_options=[dict(key=key, label=label) for key, label in [('today', 'Today'), ('7', '7 days'), ('30', '30 days')]],
        collected=f'PHP {php_total:,.2f}', has_payments=bool(payment_rows), cards=cards,
        other_currencies=[f'{currency} {amount:,.2f}' for currency, amount in sorted(totals.items()) if currency != 'PHP'],
        sparkline_data=sparkline, sparkline_points=sparkline_points(sparkline),
        collection_url=url(records_url, kind='payments', currency='PHP', range=window['key']),
        attention=attention, attention_total=waiting[0].waiting_total if waiting else 0,
        attention_url=url('system_admin_users', setup='pending'),
        invitation_expiry_hours=INVITATION_EXPIRY_HOURS,
        pipeline=pipeline, account_total=accounts['total'],
        zero_stages=', '.join(label for key, label in STAGES if not accounts[key]),
        collectors=collectors, recent_collector_changes=transitions,
        activity=activity, audit_url=url('system_admin_audit'),
    )


def get_dashboard_records(params):
    """Exact, read-only drilldowns for the global admin aggregates."""
    kind = params.get('kind', 'accounts')
    if kind == 'payments':
        queryset = Payment.objects.filter(status='verified').select_related('account')
        if params.get('range'):
            window = range_window(params['range'])
            queryset = queryset.filter(paid_at__gte=window['start'], paid_at__lte=window['end'])
        if params.get('currency'):
            queryset = queryset.filter(account__currency=params['currency'])
        return 'Collected payments', [dict(record=f'Payment #{p.pk}', detail=f'{p.account.currency} {p.amount:,.2f}', status=p.get_status_display()) for p in queryset.order_by('-pk')[:200]]
    if kind == 'commissions':
        return 'Commissions waiting for approval', [dict(record=f'Commission #{c.pk}', detail=f'{c.currency} {c.amount:,.2f}', status=c.get_status_display()) for c in Commission.objects.filter(status='pending').order_by('-pk')[:200]]
    now = timezone.now()
    queryset = account_queryset(now)
    stage = params.get('stage')
    if stage == 'worked':
        queryset = queryset.filter(status='open', has_assignment=True)
    elif stage == 'stale':
        queryset = stale_accounts(queryset, now, getattr(settings, 'ADMIN_DASHBOARD_UNWORKED_DAYS', 7))
    elif stage in dict(STAGES):
        queryset = queryset.filter(stage=stage)
    return 'Collection accounts', [dict(record=a.reference, detail=a.client.name, status=a.get_status_display()) for a in queryset.select_related('client').order_by('-pk')[:200]]
