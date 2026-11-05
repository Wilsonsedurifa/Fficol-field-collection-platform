"""URL routing for account status transitions."""

from django.urls import path
from .views import backoffice_change_account_status

urlpatterns = [
    path(
        "back-office/assignments/<int:account_id>/status/",
        backoffice_change_account_status,
        name="backoffice_change_account_status",
    ),
]
