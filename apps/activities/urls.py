from django.urls import path
from . import views

app_name = "activities"
urlpatterns = [
    path("back-office/assignments/<int:assignment_id>/schedule/", views.schedule_followup_form, name="authorize_followup"),
    path("follow-ups/", views.followups_page, name="followups"),
    path("visits/sync/", views.offline_sync_visit, name="offline_sync_visit"),
    path("accounts/<int:assignment_id>/visits/start/", views.start, name="start_visit"),
    path("accounts/<int:assignment_id>/visits/<int:activity_id>/submit/", views.submit, name="submit_visit"),
    path("accounts/<int:assignment_id>/visits/<int:activity_id>/cancel/", views.cancel, name="cancel_visit"),
    path("accounts/<int:assignment_id>/visits/<int:activity_id>/revisit/", views.schedule, name="schedule_revisit"),
    path("accounts/<int:assignment_id>/contact-attempts/", views.record_contact_attempt, name="record_contact_attempt"),
]
