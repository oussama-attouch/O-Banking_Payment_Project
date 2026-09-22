"""Dashboard analytics for O-Banking.

Every public helper takes a ``User`` (or ``Account``) and returns plain Python
data -- dicts, lists and ``Decimal`` -- never a QuerySet. Two reasons:

* the HTML dashboard and the JSON endpoint must render the same numbers, so the
  computation lives in exactly one place;
* a QuerySet handed to a template can silently issue one query per row. Freezing
  the data here keeps the query count predictable and testable.

Query counts are stated in each docstring and asserted from
``account.tests.DashboardAnalyticsTests``.

Two conventions this module has to fix, because neither is recorded in the
schema:

**Which statuses mean the money actually moved.**
:data:`COMPLETED_STATUSES` is ``completed`` plus ``request_settled``. A settled
request *is* a completed settlement; excluding it would hide real movement from
every figure on the dashboard.

**Which direction the money went.**
Direction is not simply "the sender paid, the receiver was paid". That holds for
a transfer, but a payment request reverses it: the requester is stored as
``sender``, and when the other party settles, the money moves from ``reciever``
back to ``sender``. :func:`_received_q` and :func:`_sent_q` encode that, so a
settled request credits the requester instead of debiting them.
"""
from datetime import timedelta
from decimal import Decimal

from django.core.paginator import EmptyPage, Paginator
from django.db.models import (
    Avg,
    BigIntegerField,
    Case,
    Count,
    F,
    Max,
    Q,
    Sum,
    When,
)
from django.db.models.functions import TruncDate, TruncWeek
from django.utils import timezone

from account.models import Account, KYC
from core.models import Transaction
from userauths.models import User

ZERO = Decimal("0.00")

#: Statuses in which money has actually moved.
COMPLETED_STATUSES = ("completed", "request_settled")

#: Statuses representing a movement that is still in flight.
PENDING_STATUSES = ("processing", "request_sent", "request_processing")

#: ``Transaction`` declares its choices as module-level constants, so the keys
#: are read back off the field rather than off the class. The 2a-dash-1 brief
#: referred to ``Transaction.STATUS_CHOICES`` and
#: ``Transaction.TRANSACTION_TYPE``; neither attribute exists.
STATUS_CHOICES = tuple(Transaction._meta.get_field("status").choices)
TYPE_CHOICES = tuple(Transaction._meta.get_field("transaction_type").choices)
STATUS_KEYS = tuple(key for key, _ in STATUS_CHOICES)
TYPE_KEYS = tuple(key for key, _ in TYPE_CHOICES)


# --------------------------------------------------------------------- helpers
def _money(value):
    """Normalise an aggregate to a 2-place ``Decimal``."""
    return (value or ZERO).quantize(ZERO)


def _party_q(user):
    """Transactions the user is a party to.

    Deliberately narrower than ``core.security.party_transaction_filter``: that
    one also joins ``sender_account`` / ``reciever_account`` for authorisation.
    Every row this application creates already sets ``sender`` and ``reciever``,
    so the account joins would only cost the planner work.
    """
    return Q(user=user) | Q(sender=user) | Q(reciever=user)


def _received_q(user):
    """Money that moved *into* the user's account (see module docstring)."""
    return Q(transaction_type="transfer", reciever=user) | Q(
        transaction_type="request", sender=user
    )


def _sent_q(user):
    """Money that moved *out of* the user's account (see module docstring)."""
    return Q(transaction_type="transfer", sender=user) | Q(
        transaction_type="request", reciever=user
    )


def _display_name(person):
    """KYC full name when there is one, else the username.

    ``person.kyc`` is a reverse one-to-one: when no row exists Django raises
    ``RelatedObjectDoesNotExist``, which subclasses ``AttributeError``, so
    ``getattr`` returns the default instead of exploding.
    """
    if person is None:
        return ""
    kyc = getattr(person, "kyc", None)
    full_name = (getattr(kyc, "full_name", None) or "").strip()
    return full_name or person.username


def _counterparty_of(user, transaction):
    """The other party on ``transaction``, from ``user``'s point of view."""
    if transaction.sender_id == user.pk:
        return transaction.reciever
    return transaction.sender


def _window(days):
    """``Q`` limiting a queryset to the trailing window, or an empty ``Q``."""
    if days is None:
        return Q()
    return Q(date__gte=timezone.now() - timedelta(days=days))


# ------------------------------------------------------------------- public API
def get_balance(account):
    """Current balance of ``account``.

    Query count: **0** -- reads the already-loaded attribute.
    """
    return _money(account.account_balance)


def get_kpis(user, days=30, account=None):
    """The dashboard's headline figures.

    ``days`` scopes every flow metric to the trailing window; pass ``None`` for
    all time. ``balance`` is always point-in-time.

    Query count: **2** when ``account`` is supplied, **3** otherwise.
    """
    if account is None:
        account = Account.objects.filter(user=user).first()

    scope = Transaction.objects.filter(_party_q(user)).filter(_window(days))
    completed = Q(status__in=COMPLETED_STATUSES)

    totals = scope.aggregate(
        # One aggregate, six figures: each filter becomes a CASE WHEN.
        received=Sum("amount", filter=_received_q(user) & completed),
        sent=Sum("amount", filter=_sent_q(user) & completed),
        pending_count=Count("pk", filter=Q(status__in=PENDING_STATUSES)),
        transaction_count=Count("pk"),
        average_amount=Avg("amount", filter=completed),
        largest_amount=Max("amount", filter=completed),
    )

    # The name of the counterparty on the largest completed movement needs the
    # row itself, so it is one further query.
    largest = (
        scope.filter(completed)
        .select_related("sender", "sender__kyc", "reciever", "reciever__kyc")
        .order_by("-amount", "-id")
        .first()
    )

    received = _money(totals["received"])
    sent = _money(totals["sent"])

    return {
        "balance": get_balance(account) if account else ZERO,
        "received": received,
        "sent": sent,
        "net": (received - sent).quantize(ZERO),
        "pending_count": totals["pending_count"] or 0,
        "transaction_count": totals["transaction_count"] or 0,
        "average_amount": _money(totals["average_amount"]),
        "largest_amount": _money(totals["largest_amount"]),
        "largest_counterparty": _display_name(_counterparty_of(user, largest)) if largest else "",
    }


def get_daily_net_flow(user, days=90):
    """One bucket per day, oldest first, zero-filled.

    Query count: **1**.
    """
    today = timezone.localdate()
    start = today - timedelta(days=days - 1)

    rows = (
        Transaction.objects.filter(
            _party_q(user),
            status__in=COMPLETED_STATUSES,
            date__date__gte=start,
        )
        .annotate(bucket=TruncDate("date"))
        .values("bucket")
        .annotate(
            received=Sum("amount", filter=_received_q(user)),
            sent=Sum("amount", filter=_sent_q(user)),
        )
    )
    by_day = {row["bucket"]: row for row in rows}

    out = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        row = by_day.get(day)
        received = _money(row["received"]) if row else ZERO
        sent = _money(row["sent"]) if row else ZERO
        out.append(
            {
                "date": day.isoformat(),
                "received": received,
                "sent": sent,
                "net": (received - sent).quantize(ZERO),
            }
        )
    return out


def get_weekly_volume(user, weeks=12):
    """One bucket per ISO week (Monday start), oldest first, zero-filled.

    Query count: **1**.
    """
    today = timezone.localdate()
    this_monday = today - timedelta(days=today.weekday())
    start = this_monday - timedelta(weeks=weeks - 1)

    rows = (
        Transaction.objects.filter(
            _party_q(user),
            status__in=COMPLETED_STATUSES,
            date__date__gte=start,
        )
        .annotate(bucket=TruncWeek("date"))
        .values("bucket")
        .annotate(
            sent=Sum("amount", filter=_sent_q(user)),
            received=Sum("amount", filter=_received_q(user)),
        )
    )
    by_week = {}
    for row in rows:
        bucket = row["bucket"]
        by_week[bucket.date() if hasattr(bucket, "date") else bucket] = row

    out = []
    for offset in range(weeks):
        week_start = start + timedelta(weeks=offset)
        row = by_week.get(week_start)
        out.append(
            {
                "week_start": week_start.isoformat(),
                "sent": _money(row["sent"]) if row else ZERO,
                "received": _money(row["received"]) if row else ZERO,
            }
        )
    return out


def get_status_breakdown(user):
    """Count of the user's transactions per status, including zeroes.

    Query count: **1**.
    """
    counts = {
        row["status"]: row["n"]
        for row in Transaction.objects.filter(_party_q(user))
        .values("status")
        .annotate(n=Count("pk"))
    }
    return {key: counts.get(key, 0) for key in STATUS_KEYS}


def get_recent_transactions(user, limit=8):
    """The user's most recent transactions, newest first.

    Query count: **1** -- related users and their KYC rows are joined, so a
    template can read ``transaction.sender.kyc.full_name`` without an N+1.
    """
    return list(
        Transaction.objects.filter(_party_q(user))
        .select_related(
            "sender",
            "sender__kyc",
            "reciever",
            "reciever__kyc",
            "sender_account",
            "reciever_account",
        )
        .order_by("-date", "-id")[:limit]
    )


def get_top_counterparties(user, limit=5):
    """Who the user moves the most money with, by volume, descending.

    Query count: **2** -- one grouped aggregate, one name lookup.
    """
    rows = (
        Transaction.objects.filter(_party_q(user), status__in=COMPLETED_STATUSES)
        .annotate(
            counterparty=Case(
                When(sender=user, then=F("reciever")),
                default=F("sender"),
                output_field=BigIntegerField(),  # User.pk is a BigAutoField
            )
        )
        .values("counterparty")
        .annotate(count=Count("pk"), volume=Sum("amount"))
        .order_by("-volume", "-count")[:limit]
    )
    rows = list(rows)

    ids = [row["counterparty"] for row in rows if row["counterparty"] is not None]
    names = {
        person.pk: _display_name(person)
        for person in User.objects.filter(pk__in=ids).select_related("kyc")
    }

    return [
        {
            "name": names.get(row["counterparty"], ""),
            "count": row["count"],
            "volume": _money(row["volume"]),
        }
        for row in rows
    ]


def get_kyc_status(user, account=None, has_kyc=None):
    """Where the user stands in the KYC flow.

    ``submitted`` is true when the model flag is set *or* a KYC row exists. The
    flag is never written anywhere in this application, so the row is the only
    real signal that anything was submitted; relying on the flag alone would
    report "none / 0%" for someone who has demonstrably filed their details.

    Query count: **0** when both ``account`` and ``has_kyc`` are supplied,
    otherwise **1** or **2**.
    """
    if account is None:
        account = Account.objects.filter(user=user).first()
    if has_kyc is None:
        has_kyc = KYC.objects.filter(user=user).exists()

    submitted = bool(account and account.kyc_submitted) or bool(has_kyc)
    confirmed = bool(account and account.kyc_confirmed)

    if confirmed:
        state, progress = "confirmed", 100
    elif submitted:
        state, progress = "pending", 50
    else:
        state, progress = "none", 0

    return {
        "submitted": submitted,
        "confirmed": confirmed,
        "state": state,
        "progress": progress,
    }


def get_transaction_history(user, status=None, ttype=None, page=1, per_page=20):
    """A page of the user's transactions, newest first, with filters.

    ``status`` and ``ttype`` are ignored when they are not valid choice keys, so
    a hand-edited query string cannot widen or break the query.

    Query count: **2** -- one COUNT, one page of rows.
    """
    queryset = Transaction.objects.filter(_party_q(user))
    if status in STATUS_KEYS:
        queryset = queryset.filter(status=status)
    if ttype in TYPE_KEYS:
        queryset = queryset.filter(transaction_type=ttype)
    queryset = queryset.select_related(
        "sender",
        "sender__kyc",
        "reciever",
        "reciever__kyc",
        "sender_account",
        "reciever_account",
    ).order_by("-date", "-id")

    paginator = Paginator(queryset, per_page)
    try:
        page_obj = paginator.page(page)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages or 1)

    return {
        "page": page_obj.number,
        "per_page": per_page,
        "total": paginator.count,
        "total_pages": paginator.num_pages,
        "has_next": page_obj.has_next(),
        "has_prev": page_obj.has_previous(),
        "items": list(page_obj.object_list),
    }
