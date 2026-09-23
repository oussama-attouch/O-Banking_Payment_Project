"""Template tags for the notification bell and list (Phase 5g-2).

Deliberately a template-tag library rather than a context processor: the
topbar asks for exactly what it renders, so pages that do not show the bell
cost no extra query, and no settings.py change is needed to enable it.

Every tag tolerates an AnonymousUser and returns an empty value.
"""
from django import template

register = template.Library()


@register.simple_tag
def unread_notification_count(user):
    """Count unread notifications for the bell badge. Safe to call
    with an AnonymousUser."""
    if not getattr(user, "is_authenticated", False):
        return 0
    from account.models import Notification
    return Notification.objects.filter(user=user, is_read=False).count()


@register.simple_tag
def recent_notifications(user, limit=5):
    """The most recent N notifications for the dropdown. Returns an
    empty list for an anonymous user."""
    if not getattr(user, "is_authenticated", False):
        return []
    from account.models import Notification
    return list(Notification.objects.filter(user=user)[:limit])


@register.simple_tag
def notification_url(link):
    """Reverse a notification's stored ``link`` into a real URL.

    ``Notification.link`` is written by account/notifications.py as
    ``"/transaction-detail/<transaction_id>/"``. ``{% url %}`` needs just the
    id, and slicing the string is not safe here: the prefix
    ``"/transaction-detail/"`` is exactly 20 characters, but ``link|slice:"20:"``
    yields ``"<id>/"`` -- trailing slash included -- which reverses to
    ``/transaction-detail/<id>//`` and 404s, because the route is
    ``<transaction_id>/`` and ``str`` does not match ``/``.

    Parsing the last path segment avoids that, and a blank or unparseable link
    (the admin can create one, ``link`` is ``blank=True``) falls back to the
    notifications list instead of raising NoReverseMatch and 500-ing the page.
    """
    from django.urls import NoReverseMatch, reverse

    txn_id = (link or "").strip("/").rpartition("/")[2]
    if txn_id:
        try:
            return reverse("core:transaction-detail", args=[txn_id])
        except NoReverseMatch:
            pass
    return reverse("account:notifications")
