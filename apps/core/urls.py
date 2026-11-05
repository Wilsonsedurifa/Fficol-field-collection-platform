from django.urls import path

from . import views

app_name = "notifications"

urlpatterns = [
    path("", views.inbox, name="inbox"),
    path("offers/open/", views.open_offers, name="open_offers"),
    path("read-all/", views.mark_all_read, name="mark_all_read"),
    path("<int:notification_id>/open/", views.open_notification, name="open"),
    path("<int:notification_id>/read/", views.mark_read, name="mark_read"),
    path("<int:notification_id>/unread/", views.mark_unread, name="mark_unread"),
]
