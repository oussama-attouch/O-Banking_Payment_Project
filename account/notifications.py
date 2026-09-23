"""Notification creation for money events.

The signal observes core.Transaction without modifying it. Only one
notification is created per state transition: a pre_save captures the
previous status, and post_save compares and emits only when the status
has changed into a settled state (completed or request_settled), or into
request_sent for a payment request.

Registration: account/apps.py::AccountConfig.ready() imports this module for
its side effects. INSTALLED_APPS lists 'account' (payment_prj/settings.py
line 77) and apps.py defines exactly one AppConfig subclass, so Django's
app-config auto-discovery instantiates AccountConfig and calls ready().
"""
from django.db.models.signals import pre_save, post_save
from django.dispatch import receiver

from core.models import Transaction
from .models import Notification


SETTLED_STATUSES = ("completed", "request_settled")
PENDING_STATUSES = ("processing", "request_sent", "request_processing")


def notify(user, kind, title, body="", link=""):
    """Create one Notification row for `user`. Safe to call with a
    None user (no-op). Returns the row or None."""
    if user is None:
        return None
    return Notification.objects.create(
        user=user,
        kind=kind,
        title=title[:200],
        body=body[:400],
        link=link[:300],
    )


def _previous_status(instance):
    """Return the status currently stored in the DB for this row,
    or None if the row has never been saved."""
    if instance.pk is None:
        return None
    return (Transaction.objects
            .filter(pk=instance.pk)
            .values_list("status", flat=True)
            .first())


@receiver(pre_save, sender=Transaction)
def _capture_previous_status(sender, instance, **kwargs):
    instance._previous_status = _previous_status(instance)


@receiver(post_save, sender=Transaction)
def _create_notification_on_settle(sender, instance, created, **kwargs):
    previous = getattr(instance, "_previous_status", None)
    new = instance.status

    # Only emit when the row has newly entered a settled state.
    # On create, previous is None and new is what the view wrote.
    # On update, previous is the DB's old value.
    if new not in SETTLED_STATUSES:
        return
    if previous == new:
        # Admin re-save, or the same settled value written twice.
        return

    # Direction rule (do not re-derive):
    #   transfer: sender pays, reciever receives
    #   request with status request_settled: reciever pays back, sender receives
    link = f"/transaction-detail/{instance.transaction_id}/"
    amount = instance.amount

    if instance.transaction_type == "transfer":
        notify(
            instance.reciever,
            Notification.KIND_MONEY_IN,
            f"You received {amount}",
            f"From {instance.sender.username if instance.sender else 'unknown'}",
            link,
        )
        notify(
            instance.sender,
            Notification.KIND_MONEY_OUT,
            f"You sent {amount}",
            f"To {instance.reciever.username if instance.reciever else 'unknown'}",
            link,
        )
    elif instance.transaction_type == "request":
        notify(
            instance.sender,
            Notification.KIND_SETTLED,
            f"Your request was settled: {amount}",
            f"Paid by {instance.reciever.username if instance.reciever else 'unknown'}",
            link,
        )
        notify(
            instance.reciever,
            Notification.KIND_SETTLED,
            f"You settled a request: {amount}",
            f"To {instance.sender.username if instance.sender else 'unknown'}",
            link,
        )


@receiver(post_save, sender=Transaction)
def _create_pending_notification(sender, instance, created, **kwargs):
    """Notify the requester's counterparty once, when a payment
    request first enters request_sent.

    The live view creates the row as request_processing and then
    saves it again as request_sent, so the created flag is False on
    the transition that matters. Rely on the pre_save captured
    _previous_status instead, mirroring _create_notification_on_settle.
    """
    if instance.transaction_type != "request":
        return
    if instance.status != "request_sent":
        return
    previous = getattr(instance, "_previous_status", None)
    if previous == "request_sent":
        return  # already notified on an earlier save
    notify(
        instance.reciever,
        Notification.KIND_REQUEST,
        f"{instance.sender.username if instance.sender else 'Someone'} requested {instance.amount}",
        "Open the request to settle or ignore it.",
        f"/transaction-detail/{instance.transaction_id}/",
    )
