from django.urls import path
from . import views

app_name = "assignments"
urlpatterns = [
    path("location/", views.submit_location, name="submit_location"),
    path("availability/", views.availability, name="availability"),
    path("offers/", views.account_offers_page, name="account_offers"),
    path("offers/data/", views.offers_list, name="offers_list"),
    path("offers/refresh/", views.offers_refresh, name="offers_refresh"),
    path("offers/<int:offer_id>/accept/", views.accept_offer, name="accept_offer"),
    path("offers/<int:offer_id>/decline/", views.decline_offer, name="decline_offer"),
    path("accounts/", views.my_accounts_page, name="my_accounts"),
    path("accounts/offline-pack/", views.offline_pack_api, name="offline_pack"),
    path("accounts/<int:assignment_id>/", views.account_detail_page, name="account_detail"),
    path("accounts/<int:assignment_id>/release-request/", views.request_release_form, name="request_release"),
]
