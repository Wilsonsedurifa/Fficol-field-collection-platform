from apps.accounts.views import backoffice_account_import, backoffice_account_import_confirm, download_import_template
from django.contrib.auth.views import LogoutView
from django.urls import include, path
from apps.activities.views import review_dashboard, review_visit_form, backoffice_visits, backoffice_followups
from apps.assignments.views import (
    backoffice_release_requests,
    decide_release_request_form,
    backoffice_assignments,
    backoffice_account_assignment_detail,
    assign_account_form,
)
from apps.collection.views import (
    approve_payout_form,
    backoffice_commissions,
    backoffice_earning_rules,
    backoffice_payments,
    backoffice_payouts,
    backoffice_point_rules,
    collector_commissions,
    collector_performance,
    create_payout_form,
    mark_payout_paid_form,
    review_commission_form,
    review_payment_form,
)

from .views import (
    backoffice_invitation_setup,
    FiColLoginView,
    collector_dashboard,
    collector_help,
    collector_invitation_setup,
    collector_pending_approval,
    collector_profile,
)

urlpatterns = [
    path("invitations/back-office/<str:token>/", backoffice_invitation_setup, name="backoffice_invitation_setup"),
    path("invitations/collector/<str:token>/", collector_invitation_setup, name="collector_invitation_setup"),
    path("collector/pending/", collector_pending_approval, name="collector_pending_approval"),
    path("system-admin/", include("apps.user.admin_portal.urls")),
    path("", include("apps.user.monitoring.urls")),
    path("", include("apps.accounts.status.urls")),
    path("back-office/",review_dashboard,name="supervisor_dashboard"),
    path("back-office/visits/",backoffice_visits,name="backoffice_visits"),
    path("back-office/follow-ups/",backoffice_followups,name="backoffice_followups"),
    path("back-office/payments/",backoffice_payments,name="backoffice_payments"),
    path("back-office/release-requests/", backoffice_release_requests, name="backoffice_release_requests"),
    path("back-office/accounts/import/", backoffice_account_import, name="backoffice_account_import"),
    path("back-office/accounts/import/confirm/", backoffice_account_import_confirm, name="backoffice_account_import_confirm"),
    path("back-office/accounts/import/template/", download_import_template, name="download_import_template"),
    path("back-office/assignments/", backoffice_assignments, name="backoffice_assignments"),
    path("back-office/assignments/<int:account_id>/", backoffice_account_assignment_detail, name="backoffice_account_assignment_detail"),
    path("back-office/assignments/<int:account_id>/assign/", assign_account_form, name="assign_account_form"),
    path("back-office/visits/<int:activity_id>/review/",review_visit_form,name="review_visit"),
    path("back-office/payments/<int:payment_id>/verify/",review_payment_form,name="review_payment"),
    path("back-office/release-requests/<int:request_id>/decision/", decide_release_request_form, name="decide_release_request"),
    path("backoffice/earning-rules/", backoffice_earning_rules, name="backoffice_earning_rules"),
    path("backoffice/commissions/", backoffice_commissions, name="backoffice_commissions"),
    path("backoffice/commissions/<int:commission_id>/review/", review_commission_form, name="review_commission"),
    path("backoffice/payouts/", backoffice_payouts, name="backoffice_payouts"),
    path("backoffice/payouts/create/", create_payout_form, name="create_payout_form"),
    path("backoffice/payouts/<int:payout_id>/approve/", approve_payout_form, name="approve_payout_form"),
    path("backoffice/payouts/<int:payout_id>/pay/", mark_payout_paid_form, name="mark_payout_paid_form"),
    # Also support back-office/ prefix aliases for earning-rules, commissions, payouts
    path("back-office/earning-rules/", backoffice_earning_rules),
    path("back-office/commissions/", backoffice_commissions),
    path("back-office/payouts/", backoffice_payouts),
    path("backoffice/point-rules/", backoffice_point_rules, name="backoffice_point_rules"),
    path("back-office/point-rules/", backoffice_point_rules),
    path("login/", FiColLoginView.as_view(), name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path(
        "collector/dashboard/",
        collector_dashboard,
        name="collector_dashboard",
    ),
    path(
        "collector/commissions/",
        collector_commissions,
        name="collector_commissions",
    ),
    path(
        "collector/performance/",
        collector_performance,
        name="collector_performance",
    ),
    path(
        "collector/profile/",
        collector_profile,
        name="collector_profile",
    ),
    path(
        "collector/help/",
        collector_help,
        name="collector_help",
    ),
]
