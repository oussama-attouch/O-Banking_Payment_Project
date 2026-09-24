from django.apps import AppConfig


class AuditConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "audit"
    verbose_name = "Audit log"

    def ready(self):
        # Import for side effects: registers user_login_failed.
        from . import signals  # noqa: F401
