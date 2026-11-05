from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied


def system_admin_required(view_func):
    """Decorator ensuring that only active superusers can access the view.

    - Anonymous users are redirected to the login page with safe encoded next parameter.
    - Authenticated users who are not active superusers receive a 403 PermissionDenied.
    - is_staff alone does not grant access.
    """
    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if not (getattr(request.user, "is_active", False) and getattr(request.user, "is_superuser", False)):
            raise PermissionDenied("Active superuser access is required.")
        return view_func(request, *args, **kwargs)

    return _wrapped_view
