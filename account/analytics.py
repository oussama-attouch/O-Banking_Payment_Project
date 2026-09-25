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

from account.models import Account, Category, KYC
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
#: ``transaction_type`` metadata from the model. The dashboard's page-level type
#: filter has its own list below, so this one keeps the model's pair shape and
#: is what :data:`TYPE_KEYS` and the transaction-history form consume.
TYPE_CHOICES = tuple(Transaction._meta.get_field("transaction_type").choices)
STATUS_KEYS = tuple(key for key, _ in STATUS_CHOICES)
TYPE_KEYS = tuple(key for key, _ in TYPE_CHOICES)

#: Dashboard period keys, widest window last. ``all`` is the only one whose
#: :data:`PERIOD_DAYS` entry is ``None``, which every helper reads as "no lower
#: bound" rather than as a number of days.
PERIOD_CHOICES = [
    ("7d", "7 days"),
    ("30d", "30 days"),
    ("90d", "90 days"),
    ("1y", "1 year"),
    ("all", "All time"),
]
PERIOD_DAYS = {"7d": 7, "30d": 30, "90d": 90, "1y": 365, "all": None}
PERIOD_SHORT = {"7d": "7d", "30d": "30d", "90d": "90d", "1y": "1y", "all": "all"}

#: Type filter for the dashboard panel: the two real ``transaction_type`` values
#: plus an ``all`` pseudo-type. The selection reaches the helpers as
#: ``transaction_type=None`` for "all", so no ``Q`` is added at all.
TYPE_FILTER_CHOICES = [
    ("all", "All types"),
    ("transfer", "Transfers"),
    ("request", "Requests"),
]

#: Days of history the "all time" window may cover. Caps the number of buckets a
#: hand-edited query string can ask the server to build.
MAX_FLOW_DAYS = 730

#: Sparkline points are capped regardless of how wide the KPI window is: a 365
#: day period must not try to plot 365 points into a 60x20 box.
MAX_SPARKLINE_DAYS = 30


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
    """``Q`` limiting a queryset to the trailing window, or an empty ``Q``.

    ``days=None`` means "all time" and is *not* an error: an empty ``Q`` adds no
    ``WHERE`` term at all, so the caller sees the whole history.
    """
    if days is None:
        return Q()
    return Q(date__gte=timezone.now() - timedelta(days=days))


def _type_q(transaction_type):
    """``Q`` narrowing a queryset to one ``transaction_type``.

    ``None`` (the "all types" selection) yields an empty ``Q``, so the caller's
    SQL is byte-for-byte what it was before the filter existed.
    """
    if transaction_type is None:
        return Q()
    return Q(transaction_type=transaction_type)


# ------------------------------------------------------------------- public API
def get_balance(account):
    """Current balance of ``account``.

    Query count: **0** -- reads the already-loaded attribute.
    """
    return _money(account.account_balance)


def get_kpis(user, days=30, account=None, transaction_type=None):
    """The dashboard's headline figures.

    ``days`` scopes every flow metric to the trailing window; pass ``None`` for
    all time. ``transaction_type`` narrows the same window to ``transfer`` or
    ``request``; ``None`` means both, and adds nothing to the query. ``balance``
    is always point-in-time and never filtered.

    Query count: **2** when ``account`` is supplied, **3** otherwise.
    """
    if account is None:
        account = Account.objects.filter(user=user).first()

    scope = (
        Transaction.objects.filter(_party_q(user))
        .filter(_window(days))
        .filter(_type_q(transaction_type))
    )
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


def get_daily_net_flow(user, days=90, transaction_type=None):
    """One bucket per day, oldest first, zero-filled.

    ``days`` is the trailing window. ``days=None`` means "all time", bounded by
    :data:`MAX_FLOW_DAYS`: the bucket range starts at the user's earliest party
    transaction, or 730 days ago, whichever is later. Because that span is read
    off the earliest bucket of the *same* query, the "all time" path costs no
    extra round trip -- and when the caller passes an explicit ``days`` the
    earliest bucket is never even inspected.

    ``transaction_type`` narrows to ``transfer`` or ``request``; ``None`` adds
    nothing to the query.

    Query count: **1**.
    """
    today = timezone.localdate()

    queryset = (
        Transaction.objects.filter(_party_q(user), status__in=COMPLETED_STATUSES)
        .filter(_type_q(transaction_type))
    )
    if days is not None:
        # An explicit span needs no lower bound on the bucket count: the caller
        # already decided how many days to plot.
        start = today - timedelta(days=days - 1)
        queryset = queryset.filter(date__date__gte=start)

    rows = (
        queryset.annotate(bucket=TruncDate("date"))
        .values("bucket")
        .annotate(
            received=Sum("amount", filter=_received_q(user)),
            sent=Sum("amount", filter=_sent_q(user)),
        )
    )
    by_day = {row["bucket"]: row for row in rows}

    if days is None:
        # The bucket range is derived from the rows already in hand, so the "all
        # time" path still costs exactly one query. Buckets are local dates, so
        # the `__date` lookup above and this comparison share one calendar.
        earliest = min(by_day) if by_day else today
        span = max(1, min((today - earliest).days + 1, MAX_FLOW_DAYS))
        start = today - timedelta(days=span - 1)
        days = span

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


def get_weekly_volume(user, weeks=12, transaction_type=None):
    """One bucket per ISO week (Monday start), oldest first, zero-filled.

    The caller computes ``weeks`` from the chosen period. ``transaction_type``
    narrows to ``transfer`` or ``request``; ``None`` adds nothing to the query.

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
        .filter(_type_q(transaction_type))
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


def get_status_breakdown(user, transaction_type=None):
    """Count of the user's transactions per status, including zeroes.

    ``transaction_type`` narrows to ``transfer`` or ``request``; ``None`` adds
    nothing to the query. Every status key is still returned, so the doughnut
    keeps a stable legend.

    Query count: **1**.
    """
    counts = {
        row["status"]: row["n"]
        for row in Transaction.objects.filter(_party_q(user))
        .filter(_type_q(transaction_type))
        .values("status")
        .annotate(n=Count("pk"))
    }
    return {key: counts.get(key, 0) for key in STATUS_KEYS}


def get_recent_transactions(user, limit=8, transaction_type=None):
    """The user's most recent transactions, newest first.

    ``transaction_type`` narrows to ``transfer`` or ``request``; ``None`` adds
    nothing to the query.

    Query count: **1** -- related users and their KYC rows are joined, so a
    template can read ``transaction.sender.kyc.full_name`` without an N+1.
    """
    return list(
        Transaction.objects.filter(_party_q(user))
        .filter(_type_q(transaction_type))
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


def get_top_counterparties(user, limit=5, transaction_type=None):
    """Who the user moves the most money with, by volume, descending.

    ``transaction_type`` narrows to ``transfer`` or ``request``; ``None`` adds
    nothing to the query.

    Query count: **2** -- one grouped aggregate, one name lookup.
    """
    rows = (
        Transaction.objects.filter(_party_q(user), status__in=COMPLETED_STATUSES)
        .filter(_type_q(transaction_type))
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


def get_active_counterparties(user, days=None, transaction_type=None):
    """Return the number of distinct other accounts the user
    transacted with in the current window, plus the count in the
    immediately previous window of the same length.

    A counterparty is another User the user sent money to OR
    received money from. The user's own account is excluded.
    Failed transactions are not counted (no money moved).

    When days is None, the previous window is None and delta_pct
    is None -- there is no "previous" for "all time".

    Returns:
      {
        "current": int,
        "previous": int | None,
        "delta_pct": float | None,
      }

    Query count: **4** for a window (two per window: the distinct
    receivers the user sent to, then the distinct senders they received
    from), **2** for "all time".
    """
    now = timezone.now()
    base = (
        Transaction.objects.filter(_party_q(user))
        .exclude(status="failed")
    )

    if transaction_type:
        base = base.filter(transaction_type=transaction_type)

    def _unique_count(qs):
        # Two queries: distinct senders the user received from,
        # distinct recievers the user sent to. Union in Python.
        sent_to = set(
            qs.filter(sender=user)
              .values_list("reciever_id", flat=True)
              .distinct()
        )
        received_from = set(
            qs.filter(reciever=user)
              .values_list("sender_id", flat=True)
              .distinct()
        )
        combined = (sent_to | received_from) - {None, user.pk}
        return len(combined)

    if days is None:
        current = _unique_count(base)
        return {"current": current, "previous": None, "delta_pct": None}

    current_start = now - timedelta(days=days)
    previous_start = now - timedelta(days=days * 2)

    current_qs = base.filter(date__gte=current_start)
    previous_qs = base.filter(
        date__gte=previous_start, date__lt=current_start
    )

    current = _unique_count(current_qs)
    previous = _unique_count(previous_qs)

    if previous == 0:
        delta_pct = None
    else:
        delta_pct = round(((current - previous) / previous) * 100, 1)

    return {
        "current": current,
        "previous": previous,
        "delta_pct": delta_pct,
    }


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


def get_transaction_history(user, status=None, ttype=None,
                            q=None, page=1, per_page=20):
    """A page of the user's transactions, newest first, with filters.

    ``status`` and ``ttype`` are ignored when they are not valid choice keys, so
    a hand-edited query string cannot widen or break the query.

    ``q`` is free text matched against the description, the transaction id and
    both parties' ``username`` and ``email`` -- one box, six columns. It uses
    SQLite's ``LIKE`` (Django's ``icontains``), which is what the demo database
    can do; at 3,000 rows that is instant. The production upgrade on PostgreSQL
    is a ``SearchVector`` field with a ``GinIndex``.

    Query count: **2** -- one COUNT, one page of rows.
    """
    queryset = Transaction.objects.filter(_party_q(user))
    if status in STATUS_KEYS:
        queryset = queryset.filter(status=status)
    if ttype in TYPE_KEYS:
        queryset = queryset.filter(transaction_type=ttype)
    q = (q or "").strip()
    if q:
        # ``reciever`` is the schema's spelling, not a typo here.
        queryset = queryset.filter(
            Q(description__icontains=q)
            | Q(transaction_id__icontains=q)
            | Q(sender__username__icontains=q)
            | Q(sender__email__icontains=q)
            | Q(reciever__username__icontains=q)
            | Q(reciever__email__icontains=q)
        )
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


#: Slices the doughnut draws individually; the tail is folded into "Other".
MAX_CATEGORY_SLICES = 8

#: The two synthetic slices. Neither can collide with a real Category row:
#: slugs are non-empty for real rows, and "__other__" is slugified from the
#: label "Other" only if a user names a category exactly that.
UNCATEGORIZED = {"name": "Uncategorized", "slug": "", "color": "gray"}
OTHER_SLICE = {"name": "Other", "slug": "__other__", "color": "gray"}


def get_spend_by_category(user, days=None, transaction_type=None):
    """Spending totals per category, descending, for the doughnut.

    "Spending" is money *out* of the user's account -- ``_sent_q``'s rule:
    ``(transfer and sender) or (request and reciever)`` -- counting only
    ``COMPLETED_STATUSES``. ``days`` scopes it to the trailing window
    (``None`` = all time) and ``transaction_type`` narrows it to one type;
    both reuse the helpers every other metric uses.

    Transactions with no category are one slice named "Uncategorized": a
    deleted category has already been SET_NULLed on the row, so NULL is the
    only case. At most :data:`MAX_CATEGORY_SLICES` slices are returned
    individually and the tail is summed into "Other".

    Query count: **2** -- one grouped aggregate, one lookup for the names,
    slugs and colours. The second is skipped when every slice is
    uncategorized, so the count is 1 in that case.
    """
    rows = list(
        Transaction.objects.filter(_sent_q(user))
        .filter(status__in=COMPLETED_STATUSES)
        .filter(_type_q(transaction_type))
        .filter(_window(days))
        .values("category_id")
        .annotate(total=Sum("amount"))
        .order_by("-total")
    )
    total = _money(sum((row["total"] for row in rows), ZERO))
    if total == ZERO:
        return {"total": ZERO, "slices": []}

    head, tail = rows[:MAX_CATEGORY_SLICES], rows[MAX_CATEGORY_SLICES:]
    ids = [row["category_id"] for row in head if row["category_id"] is not None]
    categories = {}
    if ids:
        categories = {c.pk: c for c in Category.objects.filter(pk__in=ids)}

    def slice_of(amount, name, slug, color):
        return {
            "name": name,
            "slug": slug,
            "color": color,
            "amount": _money(amount),
            # A float, for the template and the tooltip; 1 decimal is enough
            # for a percentage label.
            "percent": round(float(_money(amount) / total * 100), 1),
        }

    slices = []
    for row in head:
        category = categories.get(row["category_id"])
        if category is None:
            slices.append(slice_of(row["total"], **UNCATEGORIZED))
        else:
            slices.append(slice_of(row["total"], category.name, category.slug,
                                   category.color))
    if tail:
        slices.append(slice_of(sum((row["total"] for row in tail), ZERO),
                               **OTHER_SLICE))

    return {"total": total, "slices": slices}


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


def get_kpi_deltas(user, days=30, transaction_type=None):
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

    ``days=None`` is "all time". There is no window before the whole history, so
    every ``delta_pct`` is ``None`` and only the sparkline -- still the last
    :data:`MAX_SPARKLINE_DAYS` days -- carries a trend. ``transaction_type``
    narrows every figure, including the point-in-time balance reconstruction
    that depends on the window's net flow.

    Query count: **3**.
    """
    now = timezone.now()
    completed = Q(status__in=COMPLETED_STATUSES)

    if days is None:
        window_start = None
        previous_start = None
        spark_days = MAX_SPARKLINE_DAYS
    else:
        window_start = now - timedelta(days=days)
        previous_start = window_start - timedelta(days=days)
        spark_days = min(days, MAX_SPARKLINE_DAYS)

    # pending_count is the one KPI measured over a week rather than the period.
    pending_start = now - timedelta(days=SPARKLINE_DAYS)
    pending_previous_start = pending_start - timedelta(days=SPARKLINE_DAYS)

    # --- query 1: both windows in one pass ---------------------------------
    # The window bounds are Q objects rather than queryset filters so that "all
    # time" (window_start is None) simply drops them.
    cur_window = Q(date__gte=window_start) if window_start is not None else Q()
    prev_window = Q(date__lt=window_start) if window_start is not None else Q()

    totals = (
        Transaction.objects.filter(_party_q(user))
        .filter(Q(date__gte=previous_start) if previous_start is not None else Q())
        .filter(_type_q(transaction_type))
        .aggregate(
            cur_received=Sum("amount", filter=cur_window & _received_q(user) & completed),
            prev_received=Sum("amount", filter=prev_window & _received_q(user) & completed),
            cur_sent=Sum("amount", filter=cur_window & _sent_q(user) & completed),
            prev_sent=Sum("amount", filter=prev_window & _sent_q(user) & completed),
            cur_count=Count("pk", filter=cur_window),
            prev_count=Count("pk", filter=prev_window),
            cur_average=Avg("amount", filter=cur_window & completed),
            prev_average=Avg("amount", filter=prev_window & completed),
            cur_largest=Max("amount", filter=cur_window & completed),
            prev_largest=Max("amount", filter=prev_window & completed),
            cur_pending=Count("pk", filter=Q(date__gte=pending_start) & Q(status__in=PENDING_STATUSES)),
            prev_pending=Count(
                "pk",
                filter=Q(date__gte=pending_previous_start)
                & Q(date__lt=pending_start)
                & Q(status__in=PENDING_STATUSES),
            ),
        )
    )

    cur_received = _money(totals["cur_received"])
    cur_sent = _money(totals["cur_sent"])
    prev_received = _money(totals["prev_received"])
    prev_sent = _money(totals["prev_sent"])
    cur_net = (cur_received - cur_sent).quantize(ZERO)
    prev_net = (prev_received - prev_sent).quantize(ZERO)

    # --- query 2: one row per sparkline day -------------------------------
    today = timezone.localdate()
    flow_start = today - timedelta(days=spark_days - 1)
    daily_rows = (
        Transaction.objects.filter(_party_q(user), date__date__gte=flow_start)
        .filter(_type_q(transaction_type))
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
    for offset in range(spark_days):
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
    # Deliberately the *days* window, not the sparkline window -- a movement from
    # 10 days ago belongs in this comparison but not in the 7-point series. Under
    # "all time" there is no earlier balance to reconstruct, so it is None and
    # the card falls back to its "no comparison" text.
    balance_window_ago = (
        None if days is None else (balance_now - cur_net).quantize(ZERO)
    )

    # The sparkline, by contrast, is a closing-balance series over the sparkline
    # window, so it is anchored at its start and walked forward.
    balance_series_start = balance_now - sum(daily_net, ZERO)
    balance_series, running = [], balance_series_start
    for value in daily_net:
        running += value
        balance_series.append(running)

    def entry(current, previous, series=None, sparkline=True):
        # Under "all time" there is no earlier window, so the aggregate returns 0
        # for the previous sums. That 0 is not a measurement -- reporting it would
        # render a confident "0.0%" against a series with no history behind it.
        if days is None:
            previous = None
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
            "previous": None if days is None else _money(totals["prev_largest"]),
            "delta_pct": None,
            "sparkline_points": "",
        },
    }
