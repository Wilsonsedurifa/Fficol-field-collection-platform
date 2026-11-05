from django.urls import path
from .views import collectors

urlpatterns = [path("back-office/collectors/", collectors, name="backoffice_collectors")]
