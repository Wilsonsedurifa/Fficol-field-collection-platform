from django.conf import settings
from django.db import models


class AuditEvent(models.Model):
    """Append-only application evidence; database-level write controls are separate."""

    id = models.BigAutoField(primary_key=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="audit_events")
    account = models.ForeignKey("accounts.Account", on_delete=models.PROTECT, null=True, blank=True, related_name="audit_events")
    action = models.CharField(max_length=60)
    entity_type = models.CharField(max_length=80)
    entity_id = models.CharField(max_length=100)
    changes = models.JSONField(default=dict, help_text="Redacted allowlisted changes only; never credentials or complete customer records.")
    request_id = models.UUIDField(null=True, blank=True)
    device_label = models.CharField(max_length=100, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["account", "created_at"], name="audit_account_created"),
            models.Index(fields=["actor", "created_at"], name="audit_actor_created"),
        ]
