from django.urls import include, path

from apps.assignments.urls import urlpatterns as assignment_urlpatterns

from .views.collector import propose_information
from .views.backoffice import information_proposals, information_decision


collector_urlpatterns = [
    path("accounts/<int:assignment_id>/information/propose/", propose_information, name="propose_information"),
]

backoffice_urlpatterns = [
    path("back-office/information-proposals/", information_proposals, name="backoffice_information_proposals"),
    path("back-office/information-proposals/<int:proposal_id>/decision/", information_decision, name="backoffice_information_decision"),
]

urlpatterns = [
    path("collector/", include((collector_urlpatterns + assignment_urlpatterns, "assignments"))),
    *backoffice_urlpatterns,
]
