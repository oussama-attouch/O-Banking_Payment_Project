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
from decimal import ROUND_HALF_UP, Decimal

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


# --------------------------------------------------------------- KPI deltas
#: Sparkline geometry: a 7-point series in a 60x20 box, 2px of vertical padding.
SPARKLINE_DAYS = 7
SPARKLINE_WIDTH = 60
SPARKLINE_HEIGHT = 20
SPARKLINE_PAD = 2


def _sparkline_points(values):
    """Render up to :data:`SPARKLINE_DAYS` numbers as an SVG ``points`` string.

    The series is right-aligned: missing days are padded with zeros **at the
    front**, so the newest value is always the last point. With a flat series
    (including all zeros) every point sits on the centre line at ``y=10``;
    otherwise the maximum maps to ``y=2`` and the minimum to ``y=18``.

    Note that a padded zero is a *real* zero for scaling purposes: if none of
    the observed values is zero, the padding becomes the minimum and therefore
    renders along the bottom edge.
    """
    series = [Decimal(str(value)) for value in values][-SPARKLINE_DAYS:]
    series = [ZERO] * (SPARKLINE_DAYS - len(series)) + series

    low, high = min(series), max(series)
    step = Decimal(SPARKLINE_WIDTH) / (SPARKLINE_DAYS - 1)
    span = high - low

    parts = []
    for index, value in enumerate(series):
        x = step * index
        if span == 0:
            y = Decimal(SPARKLINE_HEIGHT) / 2
        else:
            # Decimal arithmetic keeps the endpoints exact: max -> 2, min -> 18.
            y = (
                Decimal(SPARKLINE_HEIGHT - SPARKLINE_PAD)
                - (value - low)
                * Decimal(SPARKLINE_HEIGHT - 2 * SPARKLINE_PAD)
                / span
            )
        parts.append("%s,%s" % (_trim(x), _trim(y)))
    return " ".join(parts)


def _trim(value):
    """Decimal -> "2" / "12.7" -- no trailing ``.0``, at most one decimal."""
    if value == value.to_integral_value():
        return str(int(value))
    return str(value.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def _delta_pct(current, previous):
    """Percentage change, or ``None`` when there is nothing to compare against."""
    if not previous:
        return None
    return round(float((Decimal(current) - Decimal(previous)) * 100 / Decimal(previous)), 1)


def get_kpi_deltas(user, days=30):
    """Current-vs-previous movement for each KPI card, plus a sparkline series.

    ``current`` is the trailing ``days`` window, ``previous`` the window before
    it. Two KPIs do not follow that pattern:

    * ``balance`` is point-in-time. Its "previous" value is reconstructed by
      subtracting the window's net flow from today's balance, the same
      assumption the balance-trajectory chart makes.
    * ``pending_count`` compares the last 7 days against the 7 before that, and
      its sparkline is the number of still-pending movements created on each of
      the last 7 days.

    ``largest_amount`` is a single event, so it gets ``delta_pct = None`` and an
    empty sparkline: a one-point trend line would be meaningless.

    Query count: **3**.
    """
    now = timezone.now()
    window_start = now - timedelta(days=days)
    previous_start = window_start - timedelta(days=days)
    completed = Q(status__in=COMPLETED_STATUSES)

    # pending_count is the one KPI measured over a week rather than a month.
    pending_start = now - timedelta(days=SPARKLINE_DAYS)
    pending_previous_start = pending_start - timedelta(days=SPARKLINE_DAYS)

    # --- query 1: both 30-day windows in one pass -------------------------
    totals = Transaction.objects.filter(
        _party_q(user), date__gte=previous_start
    ).aggregate(
        cur_received=Sum("amount", filter=Q(date__gte=window_start) & _received_q(user) & completed),
        prev_received=Sum("amount", filter=Q(date__lt=window_start) & _received_q(user) & completed),
        cur_sent=Sum("amount", filter=Q(date__gte=window_start) & _sent_q(user) & completed),
        prev_sent=Sum("amount", filter=Q(date__lt=window_start) & _sent_q(user) & completed),
        cur_count=Count("pk", filter=Q(date__gte=window_start)),
        prev_count=Count("pk", filter=Q(date__lt=window_start)),
        cur_average=Avg("amount", filter=Q(date__gte=window_start) & completed),
        prev_average=Avg("amount", filter=Q(date__lt=window_start) & completed),
        cur_largest=Max("amount", filter=Q(date__gte=window_start) & completed),
        prev_largest=Max("amount", filter=Q(date__lt=window_start) & completed),
        cur_pending=Count("pk", filter=Q(date__gte=pending_start) & Q(status__in=PENDING_STATUSES)),
        prev_pending=Count(
            "pk",
            filter=Q(date__gte=pending_previous_start)
            & Q(date__lt=pending_start)
            & Q(status__in=PENDING_STATUSES),
        ),
    )

    cur_received = _money(totals["cur_received"])
    cur_sent = _money(totals["cur_sent"])
    prev_received = _money(totals["prev_received"])
    prev_sent = _money(totals["prev_sent"])
    cur_net = (cur_received - cur_sent).quantize(ZERO)
    prev_net = (prev_received - prev_sent).quantize(ZERO)

    # --- query 2: the last 7 days, one row per day ------------------------
    today = timezone.localdate()
    flow_start = today - timedelta(days=SPARKLINE_DAYS - 1)
    daily_rows = (
        Transaction.objects.filter(
            _party_q(user), date__date__gte=flow_start
        )
        .annotate(bucket=TruncDate("date"))
        .values("bucket")
        .annotate(
            received=Sum("amount", filter=_received_q(user) & completed),
            sent=Sum("amount", filter=_sent_q(user) & completed),
            count=Count("pk"),
            pending=Count("pk", filter=Q(status__in=PENDING_STATUSES)),
        )
    )
    by_day = {row["bucket"]: row for row in daily_rows}

    daily_received, daily_sent, daily_count, daily_pending = [], [], [], []
    for offset in range(SPARKLINE_DAYS):
        row = by_day.get(flow_start + timedelta(days=offset))
        daily_received.append(_money(row["received"]) if row else ZERO)
        daily_sent.append(_money(row["sent"]) if row else ZERO)
        daily_count.append(row["count"] if row else 0)
        daily_pending.append(row["pending"] if row else 0)

    daily_net = [r - s for r, s in zip(daily_received, daily_sent)]

    # --- query 3: today's balance ----------------------------------------
    account = Account.objects.filter(user=user).only("account_balance").first()
    balance_now = _money(account.account_balance) if account else ZERO

    # The balance a full window ago: today's balance minus the window's net flow.
    # Deliberately the *days* window (not the 7 day sparkline window) -- a
    # movement from 10 days ago belongs in this comparison but not in the
    # 7-point series.
    balance_window_ago = (balance_now - cur_net).quantize(ZERO)

    # The sparkline, by contrast, is a 7 day closing-balance series, so it is
    # anchored a week back and walked forward.
    balance_week_ago = balance_now - sum(daily_net, ZERO)
    balance_series, running = [], balance_week_ago
    for value in daily_net:
        running += value
        balance_series.append(running)

    def entry(current, previous, series=None, sparkline=True):
        return {
            "current": current,
            "previous": previous,
            "delta_pct": _delta_pct(current, previous),
            "sparkline_points": _sparkline_points(series) if sparkline else "",
        }

    return {
        "balance": entry(balance_now, balance_window_ago, balance_series),
        "received": entry(cur_received, prev_received, daily_received),
        "sent": entry(cur_sent, prev_sent, daily_sent),
        "net": entry(cur_net, prev_net, daily_net),
        "transaction_count": entry(totals["cur_count"], totals["prev_count"], daily_count),
        "average_amount": entry(
            _money(totals["cur_average"]), _money(totals["prev_average"]), daily_received
        ),
        "pending_count": entry(totals["cur_pending"], totals["prev_pending"], daily_pending),
        # A largest-ever single movement cannot be trended.
        "largest_amount": {
            "current": _money(totals["cur_largest"]),
            "previous": _money(totals["prev_largest"]),
            "delta_pct": None,
            "sparkline_points": "",
        },
    }
