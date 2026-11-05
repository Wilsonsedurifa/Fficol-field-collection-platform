from datetime import timedelta
from django.utils import timezone

from apps.user.models import CollectorProfile


class CollectorPortalActivityMiddleware:
    """Track and throttle authenticated Collector Portal activity.
    
    Throttles database writes for `last_portal_activity_at` to at most once
    every 5 minutes per collector. Does not count login, invitation, pending,
    static, or unauthenticated requests.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        # Do not track on errors, redirects to login, or unauthenticated requests
        if response.status_code >= 400:
            return response

        user = getattr(request, "user", None)
        if not user or not getattr(user, "is_authenticated", False) or not getattr(user, "is_active", False):
            return response

        path = request.path_info
        # Only portal URLs starting with /collector/
        if not path.startswith("/collector/"):
            return response

        # Do not count invitation or pending approval pages
        if "/invitation" in path or path.startswith("/collector/pending"):
            return response

        profile = getattr(user, "collector_profile", None)
        if profile and profile.approval_status == CollectorProfile.ApprovalStatus.APPROVED:
            now = timezone.now()
            last_activity = profile.last_portal_activity_at
            if last_activity is None or (now - last_activity) >= timedelta(minutes=5):
                CollectorProfile.objects.filter(pk=profile.pk).update(last_portal_activity_at=now)
                profile.last_portal_activity_at = now

        return response
