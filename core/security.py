"""Request-boundary guards for money movement.

Phase 1 is deliberately schema-neutral, so these rules live at the view
boundary rather than in the model layer. ``Transaction.amount`` stays a plain
``DecimalField(max_digits=12, decimal_places=2)`` with no validators added.

Phase G-1 adds the per-user transfer-limit helpers at the foot of this module:
``_period_start`` (calendar window boundaries), ``_outgoing_q`` (the direction
rule for money leaving the user's account), ``_used_in_period`` (the sum inside
one window, failed rows excluded), ``ensure_transfer_limits`` (idempotent lazy
creation of the three ``TransferLimit`` rows) and ``check_transfer_limit``
(``(ok, details)`` for a proposed amount, naming the first period it breaks).
Nothing here enforces anything yet; Phase G-2 calls the checker from inside the
transfer and settlement atomic blocks.
"""
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.db.models import Q, Sum
from django.utils import timezone

from core.models import Transaction, TransferLimit

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


# =====================================================================
# Phase G-1  per-user transfer limits
# =====================================================================
def _period_start(period, now=None):
    """Return the datetime at which the current period begins.

    Uses calendar boundaries:

      day   -> local midnight today
      week  -> most recent Monday at local midnight
      month -> first of the current month at local midnight

    ``now`` is injectable so callers (and tests) can pin the window instead of
    reading the clock three times for three periods.
    """
    now = now or timezone.localtime()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    if period == "day":
        return today
    if period == "week":
        # Monday = 0 in Python's weekday()
        return today - timedelta(days=today.weekday())
    if period == "month":
        return today.replace(day=1)
    raise ValueError(f"unknown period: {period}")


def _outgoing_q(user):
    """Money out of the user's account (direction rule).

    The mirror image of ``account.analytics._sent_q`` and the same rule the
    dashboard's spend figures use: for a transfer the sender pays, and for a
    request the *reciever* pays (the requester is credited on settlement).
    """
    return (
        (Q(transaction_type="transfer") & Q(sender=user))
        | (Q(transaction_type="request") & Q(reciever=user))
    )


def _used_in_period(user, period, now=None):
    """Sum of the user's outgoing transfers in the current period.

    Returns a ``Decimal``. Rows in the ``failed`` status are excluded: nothing
    was debited, so nothing counts against the limit.
    """
    start = _period_start(period, now=now)
    # Exclude failed -- nothing was debited for those.
    qs = Transaction.objects.filter(
        _outgoing_q(user),
        date__gte=start,
    ).exclude(status="failed")
    total = qs.aggregate(total=Sum("amount"))["total"]
    return total if total is not None else Decimal("0.00")


def ensure_transfer_limits(user):
    """Create any missing ``TransferLimit`` rows for the user with default
    amounts. Returns the three rows as a dict keyed by period. Idempotent.

    ``ignore_conflicts`` on the insert makes a concurrent first call safe: the
    winner's rows stand and the loser reads them back below rather than raising
    on the ``unique_limit_per_user_period`` constraint.
    """
    existing = {tl.period: tl for tl in
                TransferLimit.objects.filter(user=user)}
    created = []
    for period, _label in TransferLimit.PERIOD_CHOICES:
        if period not in existing:
            created.append(TransferLimit(
                user=user,
                period=period,
                amount=TransferLimit.DEFAULT_AMOUNTS[period],
            ))
    if created:
        TransferLimit.objects.bulk_create(created, ignore_conflicts=True)
    return {tl.period: tl for tl in
            TransferLimit.objects.filter(user=user)}


def check_transfer_limit(user, amount, now=None):
    """Return (ok, details).

    ok is True if `amount` fits within all three of the user's
    limits, given how much they have already sent in each period.

    details is a dict:
      {
        "period": "day" | "week" | "month" | None,   # first failure
        "used": Decimal,
        "limit": Decimal,
        "would_be": Decimal,
      }
    period is None when ok is True.

    On any doubt (missing user, amount <= 0), returns
    (False, {"period": None, "reason": "..."}).

    The three rows are created on demand, so the first check for a
    brand-new user is also the call that provisions their limits.
    """
    if user is None or not getattr(user, "pk", None):
        return False, {"period": None, "reason": "no user"}
    amount = Decimal(str(amount))
    if amount <= 0:
        return False, {"period": None, "reason": "amount must be positive"}

    limits = ensure_transfer_limits(user)
    for period, _label in TransferLimit.PERIOD_CHOICES:
        tl = limits.get(period)
        if tl is None:
            continue
        used = _used_in_period(user, period, now=now)
        would_be = used + amount
        if would_be > tl.amount:
            return False, {
                "period": period,
                "used": used,
                "limit": tl.amount,
                "would_be": would_be,
            }
    return True, {"period": None}
