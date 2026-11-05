from django.conf import settings
from django.db import models


class Notification(models.Model):
    id = models.BigAutoField(primary_key=True)
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    kind = models.CharField(max_length=30, choices=[(value, value.title()) for value in ["offer", "follow_up", "review", "commission", "system"]])
    account = models.ForeignKey("accounts.Account", on_delete=models.PROTECT, null=True, blank=True, related_name="notifications")
    offer = models.ForeignKey("assignments.AccountOffer", on_delete=models.PROTECT, null=True, blank=True, related_name="notifications")
    message = models.CharField(max_length=300)
    read_at = models.DateTimeField(null=True, blank=True)
    delivery_status = models.CharField(max_length=20, choices=[(value, value.title()) for value in ["queued", "sent", "failed"]], default="queued")
    dedup_key = models.CharField(max_length=150, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(kind__in=["offer", "follow_up", "review", "commission", "system"]), name="notification_valid_kind"),
            models.CheckConstraint(condition=models.Q(delivery_status__in=["queued", "sent", "failed"]), name="notification_valid_delivery"),
        ]
        indexes = [models.Index(fields=["recipient", "read_at", "created_at"], name="notification_recipient_read")]
