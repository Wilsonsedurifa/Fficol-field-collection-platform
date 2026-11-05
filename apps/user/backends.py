from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.db.models import Q


class CollectorEmailBackend(ModelBackend):
    """Allow collector email login while keeping legacy username authentication."""
    def authenticate(self, request, username=None, password=None, **kwargs):
        if username and '@' in username:
            User = get_user_model()
            identity = username.strip()
            matches = list(User.objects.filter(
                Q(username__iexact=identity) | Q(email__iexact=identity, collector_profile__isnull=False)
            ).distinct()[:2])
            if len(matches) != 1:
                User().set_password(password)
                return None
            username = matches[0].username
        return super().authenticate(request, username=username, password=password, **kwargs)
