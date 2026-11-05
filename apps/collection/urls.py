from django.urls import path
from . import views

app_name = "collection"

urlpatterns = [
    path("accounts/<int:assignment_id>/payments/report/", views.report_payment_api, name="report_payment"),
    path("documents/<int:document_id>/download/", views.download_document, name="download_document"),
]
