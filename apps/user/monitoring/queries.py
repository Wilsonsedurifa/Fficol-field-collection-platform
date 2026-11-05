"""Scoped, bounded queries for collector monitoring."""
from django.contrib.auth import get_user_model
from django.db.models import Count, Exists, F, IntegerField, OuterRef, Q, Subquery, Value
from django.db.models.functions import Coalesce, Greatest, Lower, NullIf, Trim, Concat
from django.utils import timezone

from apps.accounts.models import Client
from apps.accounts.information.models import AccountInformationProposal
from apps.activities.models import FieldActivity, FollowUpTask
from apps.assignments.models import AccountAssignment
from apps.collection.models import Payment
from apps.user.models import BACK_OFFICE_PERMISSIONS, ClientAccess, CollectorProfile


def scoped_collectors(viewer):
    """Return ordinary active collectors sharing an authorized active client."""
    clients = Client.objects.filter(is_active=True, user_accesses__user=viewer, user_accesses__is_active=True).distinct()
    staff = get_user_model().objects.filter(is_superuser=True)
    for permission in BACK_OFFICE_PERMISSIONS:
        app, code = permission.split(".")
        staff = staff | get_user_model().objects.filter(
            Q(user_permissions__content_type__app_label=app, user_permissions__codename=code)
            | Q(groups__permissions__content_type__app_label=app, groups__permissions__codename=code)
        )
    grants = ClientAccess.objects.filter(user_id=OuterRef("user_id"), is_active=True, client__in=clients)
    collectors = CollectorProfile.objects.filter(user__is_active=True).exclude(user_id__in=staff.values("pk"))
    return collectors.annotate(in_scope=Exists(grants)).filter(in_scope=True).select_related("user", "location"), clients


def workload(clients):
    """Annotate active open assignments with deduplicated overdue indicators."""
    now = timezone.now()
    actions = FieldActivity.objects.filter(
        assignment_id=OuterRef("pk"), account_id=OuterRef("account_id"),
        recorded_by_id=OuterRef("collector__user_id"), status=FieldActivity.Status.SUBMITTED,
        kind__in=["visit", "phone", "sms", "email"],
    )
    tasks = FollowUpTask.objects.filter(
        assignment_id=OuterRef("pk"), account_id=OuterRef("account_id"),
        assignee_id=OuterRef("collector__user_id"), status="pending",
        authorized_at__isnull=False, due_at__lt=now,
    )
    return AccountAssignment.objects.filter(
        account__client__in=clients, account__status="open", status=AccountAssignment.Status.ACTIVE,
    ).annotate(has_action=Exists(actions), followup_overdue=Exists(tasks)).annotate(
        first_overdue=Q(first_action_due_at__lt=now, has_action=False),
    )


def attach_rows(profiles, assignments, clients):
    """Attach page-sized workload and scoped activity using fixed bulk queries."""
    rows = list(profiles)
    users = {p.user_id: p for p in rows}
    for p in rows:
        p.work = []
        p.last_activity = None
    for item in assignments.filter(collector_id__in=[p.pk for p in rows]).select_related("account", "collector"):
        users[item.collector.user_id].work.append(item)
    sources = (
        (FieldActivity.objects.filter(status="submitted", kind__in=["visit", "phone", "sms", "email"]), "recorded_by_id", "ended_at"),
        (Payment.objects.all(), "reported_by_id", "created_at"),
        (AccountInformationProposal.objects.all(), "submitted_by_id", "created_at"),
    )
    from django.db.models import Max
    for source, user_field, timestamp in sources:
        results = source.filter(account__client__in=clients, **{user_field + "__in": users}).values(user_field).annotate(latest=Max(timestamp))
        for result in results:
            p = users[result[user_field]]
            if result["latest"] and (p.last_activity is None or result["latest"] > p.last_activity):
                p.last_activity = result["latest"]
    for p in rows:
        p.overdue_count = sum(bool(a.first_overdue or a.followup_overdue) for a in p.work)
        p.first_action_overdue = any(a.first_overdue for a in p.work)
        p.followup_overdue = any(a.followup_overdue for a in p.work)
    return rows


def ordered_collectors(profiles, assignments, clients, sort="default", direction="asc"):
    """Sort globally before pagination with scoped scalar subqueries, never row queries."""
    work = assignments.filter(collector_id=OuterRef("pk"))
    late = work.filter(Q(first_overdue=True) | Q(followup_overdue=True))

    def count(queryset):
        return Coalesce(Subquery(queryset.order_by().values("collector_id").annotate(total=Count("pk")).values("total")[:1]), Value(0), output_field=IntegerField())

    def latest(queryset, user_field, timestamp):
        return Subquery(queryset.filter(account__client__in=clients, **{user_field: OuterRef("user_id")}).order_by("-" + timestamp).values(timestamp)[:1])

    profiles = profiles.annotate(
        sort_work=count(work), sort_overdue=count(late), sort_at_risk=Exists(late),
        sort_activity=Greatest(
            latest(FieldActivity.objects.filter(status="submitted", kind__in=["visit", "phone", "sms", "email"]), "recorded_by_id", "ended_at"),
            latest(Payment.objects.all(), "reported_by_id", "created_at"),
            latest(AccountInformationProposal.objects.all(), "submitted_by_id", "created_at"),
        ),
        sort_name=Lower(Coalesce(NullIf(Trim(Concat("user__first_name", Value(" "), "user__last_name")), Value("")), "user__username")),
    )
    fields = {"collector": "sort_name", "active": "sort_work", "activity": "sort_activity", "overdue": "sort_overdue"}
    if sort not in fields:
        return profiles.order_by("-sort_at_risk", F("sort_activity").asc(nulls_first=True), "sort_name", "pk")
    expression = F(fields[sort])
    ordering = expression.desc(nulls_last=True) if direction == "desc" else expression.asc(nulls_first=True)
    return profiles.order_by(ordering, "sort_name", "pk")
