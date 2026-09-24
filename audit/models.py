"""Append-only audit log.

``LogEntry`` is the single table this app owns. It has no foreign keys to
``Transaction``, ``Transfer`` or anything else: a ``(target_type, target_id)``
pair records what was acted on, which keeps the audit schema independent of the
rest of the project.

Every write goes through :func:`audit.utils.log`. The model refuses updates and
deletes, and the manager refuses the queryset-level bulk writes, so an existing
row cannot be rewritten through the ORM.
"""

from django.db import models


class ImmutableQuerySet(models.QuerySet):
    """The refusals have to live on the QuerySet, not only on the manager.

    ``LogEntry.objects.update(...)`` reaches ``ImmutableManager``, but
    ``LogEntry.objects.filter(...)`` hands back a query object whose ``update()``
    and ``delete()`` never consult the manager at all. With only the manager
    overridden the append-only rule held for whole-table writes and leaked
    through every *filtered* one -- which is the shape both the Phase A gate list
    (``objects.filter(pk=...).update(...)`` -> PermissionError) and
    ``audit.tests`` use. ``ImmutableManager.get_queryset`` returns this class.

    The delete collector is unaffected: it nulls ``actor`` through
    ``LogEntry._base_manager``, whose queryset is Django's plain one, so deleting
    a user still works and only the reference is cleared.
    """

    def update(self, **kwargs):
        raise PermissionError("LogEntry is append-only: queryset.update is forbidden.")

    def delete(self):
        raise PermissionError("LogEntry is append-only: queryset.delete is forbidden.")

    def bulk_create(self, *args, **kwargs):
        raise PermissionError("LogEntry is append-only: bulk_create is forbidden.")

    def get_or_create(self, *args, **kwargs):
        raise PermissionError("LogEntry is append-only: get_or_create is forbidden.")

    def update_or_create(self, *args, **kwargs):
        raise PermissionError("LogEntry is append-only: update_or_create is forbidden.")


class ImmutableManager(models.Manager):
    def get_queryset(self):
        return ImmutableQuerySet(self.model, using=self._db)

    def update(self, **kwargs):
        raise PermissionError("LogEntry is append-only: queryset.update is forbidden.")

    def delete(self):
        raise PermissionError("LogEntry is append-only: queryset.delete is forbidden.")

    def bulk_create(self, *args, **kwargs):
        raise PermissionError("LogEntry is append-only: bulk_create is forbidden.")

    def get_or_create(self, *args, **kwargs):
        raise PermissionError("LogEntry is append-only: get_or_create is forbidden.")

    def update_or_create(self, *args, **kwargs):
        raise PermissionError("LogEntry is append-only: update_or_create is forbidden.")


class LogEntry(models.Model):
    ACTION_CHOICES = [
        ("user_register", "User registered"),
        ("user_login", "User logged in"),
        ("user_logout", "User logged out"),
        ("user_login_failed", "Login failed"),
        ("kyc_submitted", "KYC submitted"),
        ("settings_changed", "Settings changed"),
        ("password_changed", "Password changed"),
        ("transfer_created", "Transfer created"),
        ("transfer_confirmed", "Transfer confirmed"),
        ("transfer_failed", "Transfer failed"),
        ("request_created", "Payment request created"),
        ("request_sent", "Payment request sent"),
        ("settlement_confirmed", "Settlement confirmed"),
        ("settlement_failed", "Settlement failed"),
        ("recipient_added", "Recipient added"),
        ("recipient_removed", "Recipient removed"),
        ("support_ticket_created", "Support ticket created"),
        ("support_reply_created", "Support reply created"),
        ("rate_limited", "Rate limited"),
        ("2fa_enabled", "Two-factor authentication enabled"),
        ("2fa_disabled", "Two-factor authentication disabled"),
        ("2fa_challenge_passed", "Two-factor challenge passed"),
        ("2fa_challenge_failed", "Two-factor challenge failed"),
    ]

    actor = models.ForeignKey(
        "userauths.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_entries",
    )
    action = models.CharField(max_length=40, choices=ACTION_CHOICES)
    target_type = models.CharField(max_length=40, blank=True)
    target_id = models.CharField(max_length=64, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=500, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["-created_at"]),
            models.Index(fields=["actor", "-created_at"]),
            models.Index(fields=["action", "-created_at"]),
            models.Index(fields=["target_type", "target_id"]),
        ]

    def __str__(self):
        who = self.actor.username if self.actor else "anonymous"
        return f"{self.created_at:%Y-%m-%d %H:%M:%S} {who} {self.action}"

    # ---- Immutability enforcement ----

    def save(self, *args, **kwargs):
        if self.pk is not None and LogEntry.objects.filter(pk=self.pk).exists():
            raise PermissionError("LogEntry is append-only: update is forbidden.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionError("LogEntry is append-only: delete is forbidden.")

    objects = ImmutableManager()
