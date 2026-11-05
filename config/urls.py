"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.templatetags.static import static
from django.urls import include, path
from django.views.generic import RedirectView

from apps.collection.views import download_document
from apps.core.views import landing_page, pwa_manifest, pwa_offline, pwa_service_worker

urlpatterns = [
    path("manifest.webmanifest", pwa_manifest, name="pwa_manifest"),
    path("service-worker.js", pwa_service_worker, name="pwa_service_worker"),
    path("offline/", pwa_offline, name="pwa_offline"),
    path(
        "favicon.ico",
        RedirectView.as_view(url=static("img/favicon.svg"), permanent=False),
        name="favicon",
    ),
    path("", landing_page, name="home"),
    path("admin/", admin.site.urls),
    path("", include("apps.user.urls")),
    path("", include("apps.accounts.information.urls")),
    path("collector/", include("apps.activities.urls")),
    path("collector/", include("apps.collection.urls")),
    path("notifications/", include("apps.core.urls")),
    path("documents/<int:document_id>/download/", download_document, name="download_document"),
]

