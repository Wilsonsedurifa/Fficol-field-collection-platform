"""Read-only administrative audit queries, presentation and streaming export."""
import csv
import json
import re
from datetime import date, datetime, time, timedelta, timezone as datetime_timezone
from urllib.parse import urlencode

from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import StreamingHttpResponse
from django.urls import reverse
from django.utils import timezone

from .models import AuditEvent
from .presentation import ACTIONS, action_presentation, humanize_changes


def administrative_events():
    # Include the security actions already offered by the old action dropdown.
    return AuditEvent.objects.filter(
        Q(action__startswith='system_admin_') |
        Q(action__startswith='collector_invitation_') |
        Q(action__startswith='backoffice_invitation_') |
        Q(action__in=['collector_suspended', 'collector_reinstated', 'collector_offboarded',
                     'collector_warning_issued', 'collector_password_reset_sent',
                     'collector_email_changed', 'backoffice_module_access_changed']))


def filtered_events(params):
    available = sorted(administrative_events().values_list('action', flat=True).distinct())
    selected = params.get('range', 'today')
    selected = selected if selected in {'today', '7d', '30d', 'custom'} else 'today'
    filters = {'range': selected}
    today = timezone.localdate()

    def parse_day(value):
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            return None
        try:
            day = date.fromisoformat(value)
            # Reject boundaries that cannot be represented by the DB in UTC.
            timezone.make_aware(datetime.combine(day, time.min)).astimezone(datetime_timezone.utc)
            return day
        except (ValueError, OverflowError):
            return None

    start, end = None, None
    if selected == 'custom':
        start, end = parse_day(params.get('from', '')), parse_day(params.get('to', ''))
        # Ignore a reversed date interval as a whole, not as an empty result.
        if start and end and start > end:
            start = end = None
        if start:
            filters['from'] = start.isoformat()
        if end:
            filters['to'] = end.isoformat()
    else:
        start = today - timedelta(days={'today': 0, '7d': 6, '30d': 29}[selected])
        end = today
    events = administrative_events()
    tz = timezone.get_current_timezone()
    if start:
        events = events.filter(created_at__gte=timezone.make_aware(datetime.combine(start, time.min), tz))
    if end:
        # date.max cannot be incremented; there can be no later DB timestamp.
        if end < date.max:
            events = events.filter(created_at__lt=timezone.make_aware(datetime.combine(end + timedelta(days=1), time.min), tz))
    action = params.get('action', '').strip()
    if action in available or action in ACTIONS:
        filters['action'] = action
        events = events.filter(action=action)
    actor = params.get('actor', '').strip()
    if actor and len(actor) <= 150 and not any(ord(char) < 32 for char in actor):
        filters['actor'] = actor
        events = events.filter(actor__username__icontains=actor)
    return events.select_related('actor').order_by('-created_at', '-pk'), filters, available


def entity_links(events):
    # Only link entities whose current detail target exists; deleted records
    # remain readable as evidence without a broken detail link.
    from apps.accounts.models import Client
    from apps.user.models import CollectorProfile, User
    links = {}
    for kind, model, route in [('User', User, 'system_admin_user_edit'),
                               ('Client', Client, 'system_admin_client_edit'),
                               ('CollectorProfile', CollectorProfile, 'system_admin_collector_edit')]:
        ids = {int(event.entity_id) for event in events if event.entity_type == kind
               and event.entity_id.isascii() and event.entity_id.isdigit() and len(event.entity_id) < 19}
        if not ids:
            continue
        for record in model.objects.filter(pk__in=ids).values('pk', *(['user_id'] if kind == 'CollectorProfile' else [])):
            target = record['user_id'] if kind == 'CollectorProfile' else record['pk']
            links[(kind, str(record['pk']))] = reverse(route, args=[target])
    return links


def audit_context(params):
    events, filters, available = filtered_events(params)
    counts = events.aggregate(events=Count('pk'), actors=Count('actor', distinct=True),
                              statuses=Count('pk', filter=Q(action__endswith='_status_changed') |
                                             Q(action__in=['collector_suspended', 'collector_reinstated', 'collector_offboarded'])),
                              users=Count('pk', filter=Q(action='system_admin_user_created')))
    page = Paginator(events, 25).get_page(params.get('page'))
    links = entity_links(list(page))
    rows = []
    for event in page:
        presentation = action_presentation(event.action)
        presentation['sentence'] = presentation['sentence'].replace('{actor}', event.actor.username if event.actor else 'System')
        rows.append(dict(event=event, **presentation,
                         chips=humanize_changes(event.changes),
                         raw=json.dumps(event.changes, ensure_ascii=False, indent=2),
                         entity_url=links.get((event.entity_type, event.entity_id))))

    def link(values, name='system_admin_audit'):
        return reverse(name) + '?' + urlencode(values)

    labels = {'today': 'Today', '7d': 'Last 7 days', '30d': 'Last 30 days', 'custom': 'Custom'}
    chips = []
    for key, value in filters.items():
        if key == 'range' and value == 'today':
            continue
        remaining = {k: v for k, v in filters.items() if k != key}
        if key == 'range':
            remaining.pop('from', None)
            remaining.pop('to', None)
        label = labels[value] if key == 'range' else action_presentation(value)['label'] if key == 'action' else f'{key.capitalize()}: {value}'
        chips.append(dict(label=label, url=link(remaining)))
    return dict(admin_section='audit', page_obj=page, rows=rows, filters=filters, active_filters=chips,
                range_options=labels.items(), action_options=sorted(
                    [(code, action_presentation(code)['label']) for code in set(available) | set(ACTIONS)
                     if code.startswith(('system_admin_', 'collector_', 'backoffice_'))], key=lambda pair: pair[1]),
                summary_cards=[dict(label=label, value=counts[key]) for label, key in
                               [('Events', 'events'), ('Unique actors', 'actors'), ('Status changes', 'statuses'), ('Users created', 'users')]],
                export_url=link(filters, 'system_admin_audit_export'),
                previous_url=link(dict(filters, page=page.previous_page_number())) if page.has_previous() else None,
                next_url=link(dict(filters, page=page.next_page_number())) if page.has_next() else None)


def csv_export(params):
    events, _, _ = filtered_events(params)

    class Echo:
        def write(self, value):
            return value

    writer = csv.writer(Echo())

    def safe(value):
        value = str(value)
        return "'" + value if value.lstrip().startswith(('=', '+', '-', '@')) else value

    def lines():
        yield writer.writerow(['timestamp', 'actor', 'action code', 'entity', 'changes'])
        for event in events.iterator(chunk_size=500):
            yield writer.writerow([safe(value) for value in [event.created_at.isoformat(),
                event.actor.username if event.actor else 'System', event.action,
                f'{event.entity_type} #{event.entity_id}', json.dumps(event.changes, ensure_ascii=False)]])

    response = StreamingHttpResponse(lines(), content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = 'attachment; filename="ficol-audit-log.csv"'
    return response
