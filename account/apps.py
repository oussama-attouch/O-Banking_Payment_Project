from django.apps import AppConfig


class AccountConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'account'

    def ready(self):
        # Import for side effects: registers the Transaction signal
        # handlers in account/notifications.py.
        from . import notifications  # noqa: F401
