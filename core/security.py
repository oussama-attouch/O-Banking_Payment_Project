"""Request-boundary guards for money movement.

Phase 1 is deliberately schema-neutral, so these rules live at the view
boundary rather than in the model layer. ``Transaction.amount`` stays a plain
``DecimalField(max_digits=12, decimal_places=2)`` with no validators added.
"""
from decimal import Decimal, InvalidOperation

from django.db.models import Q

from core.models import Transaction

#: Largest single money movement accepted from a request.
#:
#: ``Transaction.amount`` is DecimalField(max_digits=12, decimal_places=2), so
#: the column can hold up to 9999999999.99. This cap is deliberately lower and
#: is a *business* limit, not a storage limit. Adjust it here and nowhere else.
MAX_AMOUNT = Decimal("1000000.00")

#: Must stay in step with ``Transaction.amount``'s decimal_places.
MAX_DECIMAL_PLACES = 2


class AmountError(ValueError):
    """A submitted amount was missing, malformed, or outside the allowed range."""


def parse_amount(raw, field_name="Amount"):
    """Return a validated ``Decimal`` for a user-submitted amount.

    Raises :class:`AmountError` (message safe to show to the user) for:

    * missing or blank input
    * non-numeric input -- ``abc``, ``1,000``, ``1.2.3``
    * NaN and +/-Infinity -- ``nan``, ``inf``, ``-inf`` (all valid ``Decimal``
      instances, none of them valid money)
    * zero and negative values -- ``0``, ``0.00``, ``-0``, ``-0.01``
    * more than :data:`MAX_DECIMAL_PLACES` decimal places -- ``1.234``
    * anything above :data:`MAX_AMOUNT`
    """
    text = "" if raw is None else str(raw).strip()
    if not text:
        raise AmountError("Please enter an amount.")

    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        raise AmountError("%s must be a number." % field_name)

    if not value.is_finite():
        raise AmountError("%s must be a finite number." % field_name)

    if value <= 0:
        raise AmountError("%s must be greater than zero." % field_name)

    # as_tuple().exponent is negative for fractional values: 1.234 -> -3.
    if -value.as_tuple().exponent > MAX_DECIMAL_PLACES:
        raise AmountError(
            "%s cannot have more than %d decimal places."
            % (field_name, MAX_DECIMAL_PLACES)
        )

    if value > MAX_AMOUNT:
        raise AmountError("%s cannot exceed %s." % (field_name, MAX_AMOUNT))

    return value


def party_transaction_filter(user, transaction_id):
    """``Q`` matching ``transaction_id`` only when ``user`` is one of its parties.

    A transaction is reachable by the user who created it (``user``), by either
    side of the movement (``sender`` / ``reciever`` -- those misspellings are the
    real field names), and by the holder of either account
    (``sender_account`` / ``reciever_account``).

    Scoping is expressed in the query rather than as a check after the fetch, so
    an unrelated user matches nothing at all.
    """
    return Q(transaction_id=transaction_id) & (
        Q(user=user)
        | Q(sender=user)
        | Q(reciever=user)
        | Q(sender_account__user=user)
        | Q(reciever_account__user=user)
    )


def find_party_transaction(user, transaction_id):
    """Return the transaction if ``user`` is a party to it, otherwise ``None``.

    Existence and authorization are deliberately indistinguishable: a caller
    cannot tell "no such transaction" from "not yours", so transaction IDs
    cannot be probed for existence.
    """
    return Transaction.objects.filter(
        party_transaction_filter(user, transaction_id)
    ).first()
