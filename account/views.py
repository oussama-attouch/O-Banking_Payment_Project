import csv
import datetime
from decimal import Decimal

from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncMonth
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render, redirect
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.text import slugify
from django.views.decorators.http import require_POST

from account import analytics
from account.analytics import (
    PERIOD_CHOICES,
    PERIOD_DAYS,
    PERIOD_SHORT,
    TYPE_FILTER_CHOICES,
    get_active_counterparties,
    get_spend_by_category,
)
from account.models import (
    KYC,
    Account,
    Category,
    Notification,
    Recipient,
    SavingsGoal,
    SupportReply,
    SupportTicket,
)
from account.forms import (
    AddToGoalForm,
    CategoryForm,
    KYCForm,
    ProfileForm,
    RecipientForm,
    SavingsGoalForm,
    SupportReplyForm,
    SupportTicketForm,
)
from core.models import Transaction
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from audit.utils import log as audit_log
from userauths.models import TOTPDevice

def _kyc_required(request):
    """Shared gate for the dashboard views.

    Returns a redirect response when the request must not see banking data, and
    ``None`` when it may. Mirrors ``account.views.account``'s rules exactly: an
    anonymous visitor goes to sign-in, a signed-in visitor without a KYC row goes
    to the KYC form, with the same message text.

    ``account.views.account`` is deliberately left untouched.
    """
    if not request.user.is_authenticated:
        messages.warning(request, "You need to log in to access the dashboard")
        return redirect("userauths:sign-in")
    if not KYC.objects.filter(user=request.user).exists():
        messages.warning(request, "You need to submit your KYC")
        return redirect("account:kyc-reg")
    return None


def _dashboard_filters(request):
    """Return ``(period_key, type_key, days, txn_type)`` for the dashboard.

    Both values come straight off the query string, so both are validated
    against the choice tables before use; anything unrecognised falls back to the
    default rather than reaching a queryset.

    The type filter reads ``?txn_type=``, not ``?type=``. ``?type=`` belongs to
    the transaction-history form, which has posted it since before the page-level
    filter existed; sharing the name meant the bar silently scoped the history
    table and the history form silently reset the period.
    """
    period = request.GET.get("period", "30d")
    if period not in PERIOD_DAYS:
        period = "30d"
    ttype = request.GET.get("txn_type", "all")
    if ttype not in dict(TYPE_FILTER_CHOICES):
        ttype = "all"
    days = PERIOD_DAYS[period]
    txn_type = None if ttype == "all" else ttype
    return period, ttype, days, txn_type


#: Window sizes the period filter maps onto the two time-series charts.
#: ``weeks`` is derived from ``days`` for the fixed periods; "all time" uses the
#: same 2 year cap ``analytics.get_daily_net_flow`` applies to its own buckets.
ALL_TIME_WEEKS = 104


def _dashboard_period_windows(days):
    """``(flow_days, weeks)`` -- the chart windows implied by a period."""
    flow_days = days if days is not None else analytics.MAX_FLOW_DAYS
    weeks = ALL_TIME_WEEKS if days is None else max(2, days // 7)
    return flow_days, weeks


#: Every figure the dashboard and its JSON twin share for one request.
#: ``account`` is passed in rather than looked up again, so neither view costs an
#: extra query for it.
def _dashboard_analytics(request, account, days, txn_type):
    """Run every dashboard helper under the validated filters."""
    flow_days, weeks = _dashboard_period_windows(days)

    return {
        "kpis": analytics.get_kpis(
            request.user, days=days, account=account, transaction_type=txn_type
        ),
        "kpi_deltas": analytics.get_kpi_deltas(
            request.user, days=days, transaction_type=txn_type
        ),
        "daily_flow": analytics.get_daily_net_flow(
            request.user, days=flow_days, transaction_type=txn_type
        ),
        "weekly_volume": analytics.get_weekly_volume(
            request.user, weeks=weeks, transaction_type=txn_type
        ),
        "status_breakdown": analytics.get_status_breakdown(
            request.user, transaction_type=txn_type
        ),
        "recent_transactions": analytics.get_recent_transactions(
            request.user, limit=8, transaction_type=txn_type
        ),
        "top_counterparties": analytics.get_top_counterparties(
            request.user, limit=5, transaction_type=txn_type
        ),
        "kyc_status": analytics.get_kyc_status(
            request.user, account=account, has_kyc=True
        ),
    }


@login_required
def dashboard(request):
    """The expanded dashboard: KPIs, charts data, and a filterable history.

    One filter bar drives every KPI, every table and every chart: ``?period=``
    picks the window and ``?txn_type=`` narrows it to transfers or requests. The
    transaction history at the foot of the page keeps its own independent
    ``?status=`` / ``?type=`` filters and carries the period forward in a hidden
    input so submitting it does not reset the page-level window.
    """
    blocked = _kyc_required(request)
    if blocked:
        return blocked

    kyc = KYC.objects.get(user=request.user)
    account = Account.objects.get(user=request.user)

    period, ttype, days, txn_type = _dashboard_filters(request)

    history_status = (request.GET.get("status") or "").strip()
    history_type = (request.GET.get("type") or "").strip()
    history_q = (request.GET.get("q") or "").strip()
    try:
        page = int(request.GET.get("page", 1))
    except (TypeError, ValueError):
        page = 1

    context = {
        "kyc": kyc,
        "account": account,
        **_dashboard_analytics(request, account, days, txn_type),
        "transaction_history": analytics.get_transaction_history(
            request.user,
            status=history_status or None,
            ttype=history_type or None,
            q=history_q or None,
            page=page,
        ),
        "history_status": history_status,
        "history_type": history_type,
        "history_q": history_q,
        "spend_by_category": get_spend_by_category(
            request.user, days=days, transaction_type=txn_type,
        ),
        # Phase G-3. The 8th KPI: how many distinct other accounts the user
        # banked with in the selected window. A count, not a series, so it
        # carries no sparkline -- only the current/previous delta.
        "active_counterparties": get_active_counterparties(
            request.user, days=days, transaction_type=txn_type,
        ),
        "history_choices": analytics.STATUS_CHOICES,
        "history_type_choices": analytics.TYPE_CHOICES,
        # The page-level filter, echoed back so the bar and the labels agree with
        # the figures actually rendered.
        "active_period": period,
        "active_period_label": dict(PERIOD_CHOICES)[period],
        "active_period_short": PERIOD_SHORT[period],
        "active_type": ttype,
        "active_type_label": dict(TYPE_FILTER_CHOICES)[ttype],
        "period_choices": PERIOD_CHOICES,
        "type_choices": TYPE_FILTER_CHOICES,
        # "All time" has no window before it, so the delta labels have nothing
        # to compare against and the template says so.
        "compare_label": None if days is None else dict(PERIOD_CHOICES)[period],
        # Phase F-2. The two most recent active goals, for the compact
        # "Savings goals" card below the history. A plain queryset: the
        # template checks truthiness, so a user with no goals renders no card
        # and costs no extra query.
        "active_goals": (SavingsGoal.objects
                         .filter(user=request.user, is_completed=False)
                         .order_by("-created_at")[:2]),
    }
    return render(request, "account/dashboard.html", context)


def _json_ready(value):
    """Recursively render Decimals as strings so JSON stays exact."""
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    return value


@login_required
def dashboard_data(request):
    """Aggregates as JSON, for the dashboard's polling refresh (Phase 2a-dash-4).

    Only aggregates are returned -- no model rows -- so the payload stays small
    and nothing here can leak another user's transaction detail.

    Reads the same ``?period=`` / ``?type=`` pair as the HTML dashboard through
    the same validator, so the two can never disagree about the window. The
    serialization shape is unchanged.
    """
    blocked = _kyc_required(request)
    if blocked:
        return blocked

    account = Account.objects.get(user=request.user)
    # period/ttype only name the selection; the helpers take days/txn_type.
    _period, _ttype, days, txn_type = _dashboard_filters(request)
    figures = _dashboard_analytics(request, account, days, txn_type)

    payload = {
        "generated_at": timezone.now().isoformat(),
        "kpis": _json_ready(figures["kpis"]),
        "daily_flow": _json_ready(figures["daily_flow"]),
        "weekly_volume": _json_ready(figures["weekly_volume"]),
        "status_breakdown": figures["status_breakdown"],
        "top_counterparties": _json_ready(figures["top_counterparties"]),
        "kyc_status": figures["kyc_status"],
    }
    return JsonResponse(payload)


# Define a view function named 'account' to handle user account dashboard
def account(request):
    # Check if the user is logged in
    if request.user.is_authenticated:
        try:
            # Retrieve KYC (Know Your Customer) information associated with the user
            kyc = KYC.objects.get(user=request.user)
        except:
            # If no KYC information found, show a warning message and redirect to KYC registration
            messages.warning(request, "You need to submit your KYC")
            return redirect("account:kyc-reg")
        
        # If KYC information exists, retrieve the user's account details
        account = Account.objects.get(user=request.user)
    else:
        # If the user is not logged in, show a warning message and redirect to the sign-in page
        messages.warning(request, "You need to log in to access the dashboard")
        return redirect("userauths:sign-in")

    # Create a context dictionary with KYC and account details
    context = {
        "kyc": kyc,
        "account": account,
    }
    # Render the 'account.html' template with the provided context
    return render(request, "account/account.html", context)

# Define another view function named 'kyc_registration' for KYC form submission
@login_required
def kyc_registration(request):
    # Get the logged-in user's information
    user = request.user
    # Retrieve the user's account details
    account = Account.objects.get(user=user)

    try:
        # Try to retrieve the user's existing KYC information
        kyc = KYC.objects.get(user=user)
    except:
        # If no existing KYC information found, set 'kyc' to None
        kyc = None
    
    # Check if the form is being submitted via POST request
    if request.method == "POST":
        # Create a KYCForm instance with submitted data and the existing KYC instance (if any)
        form = KYCForm(request.POST, request.FILES, instance=kyc)
        if form.is_valid():
            # If the form data is valid, save it to the database
            new_form = form.save(commit=False)
            new_form.user = user
            new_form.account = account
            new_form.save()

            # Phase 3a: record that this account has filed its details. The flag
            # was declared on the model but never written by anything, so it read
            # False even for a user with a complete KYC row. kyc_confirmed is
            # deliberately left alone -- confirming identity is an admin action,
            # not something the submitter can assert.
            Account.objects.filter(pk=account.pk).update(kyc_submitted=True)
            audit_log("kyc_submitted", target=new_form)

            # Show a success message and redirect to the account dashboard
            messages.success(request, "KYC Form submitted successfully. It's now under review.")
            return redirect("account:account")
    else:
        # If not a POST request, create a KYCForm instance with existing KYC data (if any)
        form = KYCForm(instance=kyc)
    
    # Create a context dictionary with account details, KYC form, and existing KYC information
    context = {
        "account": account,
        "form": form,
        "kyc": kyc,
    }
    # Render the 'kyc-form.html' template with the provided context
    return render(request, "account/kyc-form.html", context)


# =====================================================================
# Phase 5d  user settings
# =====================================================================
@login_required
def settings_view(request):
    """Profile and credentials for the signed-in user.

    Deliberately *not* behind ``_kyc_required``: that gate guards the dashboard's
    banking data, while a user who has not filed KYC yet -- or who needs to fix a
    typo in the name they are about to file -- must still reach their own
    settings. Only the login gate applies.

    Editing ``email`` changes the sign-in address, because
    ``userauths.User.USERNAME_FIELD`` is ``email``; the template says so next to
    the field. Changing a *password* is not done here at all -- that is
    ``account:password_change``, which asks for the current password.
    """
    # Every User is provisioned with an Account by the post_save receiver in
    # account.models, but this page only reads a few of its columns, so a missing
    # row degrades to "no account details" instead of a 500. Both relations are
    # reverse one-to-ones: their DoesNotExist subclasses AttributeError, which is
    # why getattr's default is reached rather than raised.
    account = getattr(request.user, "account", None)
    kyc = getattr(request.user, "kyc", None)

    if request.method == "POST":
        profile_form = ProfileForm(request.POST, instance=request.user)
        if profile_form.is_valid():
            profile_form.save()
            audit_log("settings_changed", target=request.user)
            messages.success(request, "Your profile has been updated.")
            return redirect("account:settings")
    else:
        profile_form = ProfileForm(instance=request.user)

    context = {
        "account": account,
        "kyc": kyc,
        "profile_form": profile_form,
        "totp_device": TOTPDevice.objects.filter(user=request.user).first(),
    }
    return render(request, "account/settings.html", context)


# =====================================================================
# Phase 5e  statements
# =====================================================================
#: ``(query-string value, button label)``, in the order the range selector
#: renders them.
STATEMENT_RANGES = (
    ("this_month", "This month"),
    ("last_3_months", "Last 3 months"),
    ("this_year", "This year"),
    ("last_12_months", "Last 12 months"),
)
STATEMENT_RANGE_KEYS = tuple(key for key, _ in STATEMENT_RANGES)
STATEMENT_RANGE_LABELS = dict(STATEMENT_RANGES)

#: Statuses on which money has actually moved. Restated rather than imported:
#: this mirrors ``account.analytics.COMPLETED_STATUSES`` and Phase 5e may not
#: change that module.
SETTLED_STATUSES = ("completed", "request_settled")

#: Rows drawn in "Recent activity", and the hard cap on one export.
STATEMENT_ROW_LIMIT = 50
EXPORT_ROW_CAP = 5000

ZERO = Decimal("0.00")


def _money(value):
    """Normalise an aggregate to a 2-place ``Decimal``."""
    return (value or ZERO).quantize(ZERO)


def _month_floor(anchor, months_back=0):
    """First day of the calendar month ``months_back`` months before ``anchor``.

    Negative values move forward. Plain ``datetime`` arithmetic: the project has
    no dateutil dependency and this phase adds none.
    """
    index = (anchor.year * 12) + (anchor.month - 1) - months_back
    year, month = divmod(index, 12)
    return datetime.date(year, month + 1, 1)


def _statement_range_key(request):
    """The requested range, or ``this_month`` for anything unrecognised.

    Normalising here (rather than reading ``request.GET`` again at the call
    site) is what keeps an unknown ``?range=`` rendering identically to no
    parameter at all, active button included.
    """
    key = (request.GET.get("range") or "").strip()
    return key if key in STATEMENT_RANGE_KEYS else "this_month"


def _statement_scope(request):
    """Return ``(start_date, end_date, label)`` for the ``?range=`` parameter.

    Every window ends *today* and starts on a calendar boundary, so the monthly
    breakdown never opens on a partial month. An unknown or missing value falls
    back to ``this_month``.
    """
    key = _statement_range_key(request)
    today = timezone.localdate()

    if key == "last_3_months":
        start = _month_floor(today, 2)
    elif key == "this_year":
        start = today.replace(month=1, day=1)
    elif key == "last_12_months":
        start = _month_floor(today, 11)
    else:
        start = _month_floor(today)

    return start, today, STATEMENT_RANGE_LABELS[key]


def _statement_transactions(user, start, end):
    """The user's transactions inside ``[start, end]``, both ends included.

    The party filter is written out here instead of importing
    ``analytics._party_q``: that name is private, and this phase may not change
    that module. ``date__date`` compares local calendar days -- the same days
    the page prints.
    """
    return Transaction.objects.filter(
        Q(user=user) | Q(sender=user) | Q(reciever=user),
        date__date__gte=start,
        date__date__lte=end,
    )


def _settled_in_q(user):
    """Rows that moved money *into* the user's account.

    Direction rule: a transfer pays the reciever, and a *settled* request pays
    the requester (the stored ``sender``). Only settled statuses count.
    """
    return (
        Q(transaction_type="transfer", reciever=user)
        | Q(transaction_type="request", sender=user)
    ) & Q(status__in=SETTLED_STATUSES)


def _settled_out_q(user):
    """Rows that moved money *out of* the user's account."""
    return (
        Q(transaction_type="transfer", sender=user)
        | Q(transaction_type="request", reciever=user)
    ) & Q(status__in=SETTLED_STATUSES)


def _statement_direction(user, txn):
    """``"in"``, ``"out"``, or ``""`` when no money has moved on this row."""
    if txn.status not in SETTLED_STATUSES:
        return ""
    if txn.transaction_type == "transfer":
        return "in" if txn.reciever_id == user.pk else "out"
    if txn.transaction_type == "request":
        return "in" if txn.sender_id == user.pk else "out"
    # recieved / withdraw / refund / none: not a direction this page can vouch for.
    return ""


def _statement_counterparty(user, txn):
    """The other party on ``txn``, from ``user``'s point of view."""
    if txn.sender_id == user.pk:
        return txn.reciever
    return txn.sender


def _statement_name(person):
    """KYC full name when there is one, else the username."""
    if person is None:
        return ""
    kyc = getattr(person, "kyc", None)
    full_name = (getattr(kyc, "full_name", None) or "").strip()
    return full_name or person.username


def _monthly_breakdown(user, scope, start, end):
    """One row per calendar month in the range, ascending, zero-filled.

    ``count`` is every transaction in the month whatever its status; ``in`` and
    ``out`` only count settled movements, exactly like the summary cards.
    """
    buckets = {}
    cursor = _month_floor(start)
    while cursor <= end:
        buckets[cursor] = {"month_label": cursor.strftime("%b %Y"), "in": ZERO,
                           "out": ZERO, "net": ZERO, "count": 0}
        cursor = _month_floor(cursor, -1)

    rows = (
        scope.annotate(month=TruncMonth("date"))
        .values("month")
        .annotate(
            month_in=Sum("amount", filter=_settled_in_q(user)),
            month_out=Sum("amount", filter=_settled_out_q(user)),
            month_count=Count("pk"),
        )
        .order_by("month")
    )

    for row in rows:
        month = row["month"]
        if timezone.is_aware(month):
            month = timezone.localtime(month)
        # setdefault: a month outside the zero-filled range cannot occur, but if
        # the timezone ever shifted one across a boundary it would still be shown
        # rather than silently dropped.
        bucket = buckets.setdefault(
            month.date().replace(day=1),
            {"month_label": month.strftime("%b %Y"), "in": ZERO, "out": ZERO,
             "net": ZERO, "count": 0},
        )
        bucket["in"] = _money(row["month_in"])
        bucket["out"] = _money(row["month_out"])
        bucket["net"] = (bucket["in"] - bucket["out"]).quantize(ZERO)
        bucket["count"] = row["month_count"] or 0

    return list(buckets.values())


def _statement_rows(user, queryset):
    """Rows for the activity table and the CSV: row, direction, counterparty."""
    return [
        {
            "transaction": txn,
            "direction": _statement_direction(user, txn),
            "counterparty": _statement_name(_statement_counterparty(user, txn)),
        }
        for txn in queryset
    ]


@login_required
def statements_view(request):
    """A date-ranged statement of the signed-in user's money movement.

    KYC-gated like the dashboard and ``account.views.account``: the figures are
    the same banking data, so the same gate applies. Read-only throughout.
    """
    blocked = _kyc_required(request)
    if blocked:
        return blocked

    user = request.user
    start, end, label = _statement_scope(request)
    scope = _statement_transactions(user, start, end)

    # One aggregate, three figures: each filter becomes a CASE WHEN.
    totals = scope.aggregate(
        total_in=Sum("amount", filter=_settled_in_q(user)),
        total_out=Sum("amount", filter=_settled_out_q(user)),
        total_count=Count("pk"),
    )
    total_in = _money(totals["total_in"])
    total_out = _money(totals["total_out"])
    total_count = totals["total_count"] or 0

    recent = scope.select_related(
        "sender",
        "sender__kyc",
        "reciever",
        "reciever__kyc",
        "sender_account",
        "reciever_account",
    ).order_by("-date", "-id")[:STATEMENT_ROW_LIMIT]

    context = {
        "account": getattr(user, "account", None),
        "kyc": getattr(user, "kyc", None),
        "range_key": _statement_range_key(request),
        "range_label": label,
        "range_choices": STATEMENT_RANGES,
        "start_date": start,
        "end_date": end,
        "summary": {
            "in": total_in,
            "out": total_out,
            "net": (total_in - total_out).quantize(ZERO),
            "count": total_count,
        },
        "monthly": _monthly_breakdown(user, scope, start, end),
        "transactions": _statement_rows(user, recent),
        "transactions_total": total_count,
    }
    return render(request, "account/statements.html", context)


def _csv_safe(value):
    """Neutralise spreadsheet formula injection.

    Prefix with a single quote when the value begins with ``=``, ``+``, ``-``,
    ``@``, or a control character (tab / CR / LF). Standard OWASP fix; the cell
    still reads exactly as stored in the browser, the quote is only visible to
    the spreadsheet, which treats it as text.

    Only the CSV writer needs this. The counterparty name comes from another
    user's ``KYC.full_name``, so it is attacker-controlled; the HTML page is
    left alone because a browser does not evaluate formulas.
    """
    if value is None:
        return ""
    s = str(value)
    if s and s[0] in ("=", "+", "-", "@", "\t", "\r", "\n"):
        return "'" + s
    return s


@login_required
def export_csv(request):
    """The same filtered statement as a CSV download.

    Login-only, deliberately: the statements page is already behind the KYC
    gate, and refusing the download of figures the user was just shown would be
    surprising -- so a missing Account or KYC row never blocks the export. The
    cap keeps one request from streaming an unbounded file; over it the answer
    is a plain 400 the user can act on, not a 500.
    """
    user = request.user
    start, end, _label = _statement_scope(request)
    scope = _statement_transactions(user, start, end)

    if scope.count() > EXPORT_ROW_CAP:
        return HttpResponse(
            "Too many rows to export. Narrow the range.",
            status=400,
            content_type="text/plain",
        )

    response = HttpResponse(content_type="text/csv")
    filename = f"o-banking-statements-{start}-to-{end}.csv"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'

    writer = csv.writer(response)
    writer.writerow([
        "date", "type", "direction", "counterparty",
        "amount", "status", "reference", "description",
    ])

    rows = scope.select_related(
        "sender",
        "sender__kyc",
        "reciever",
        "reciever__kyc",
        "sender_account",
        "reciever_account",
    ).order_by("-date", "-id")

    for txn in rows:
        when = txn.date
        if timezone.is_aware(when):
            when = timezone.localtime(when)
        writer.writerow([
            when.isoformat(),
            txn.get_transaction_type_display(),
            _statement_direction(user, txn),
            # The two free-text columns are the only ones another user can put
            # arbitrary bytes into. Date, type, direction, amount, status and
            # transaction_id are all generated here or by the model.
            _csv_safe(_statement_name(_statement_counterparty(user, txn))),
            f"{txn.amount:.2f}",
            txn.get_status_display(),
            txn.transaction_id,
            _csv_safe(txn.description or ""),
        ])

    return response


# =====================================================================
# Phase 5f-2  saved payees
# =====================================================================
#: Hard cap per user. A view-level rule, not a model constraint: a constraint
#: cannot express "no more than N", and the view can explain the refusal.
MAX_RECIPIENTS_PER_USER = 50


@login_required
def recipients_view(request):
    """List the current user's saved recipients.

    The add form is rendered inline on the same page, so adding is a POST to
    this view rather than to a separate one. Login-only like ``account:settings``
    -- a saved payee list is the user's own address book, not banking data.
    """
    account = getattr(request.user, "account", None)
    kyc = getattr(request.user, "kyc", None)

    if request.method == "POST":
        if Recipient.objects.filter(user=request.user).count() >= MAX_RECIPIENTS_PER_USER:
            messages.warning(request,
                f"You can save at most {MAX_RECIPIENTS_PER_USER} recipients. "
                "Delete one before adding another.")
            return redirect("account:recipients")

        form = RecipientForm(request.POST)
        if form.is_valid():
            raw = form.cleaned_data["account_number"]
            # accept either account_number or account_id
            target = (Account.objects
                      .filter(account_number=raw).first()
                      or Account.objects.filter(account_id=raw).first())
            if target is None:
                form.add_error("account_number",
                    "No account matches that number or ID on this installation.")
            elif target.user_id == request.user.id:
                form.add_error("account_number",
                    "You cannot save your own account as a recipient.")
            elif Recipient.objects.filter(user=request.user, target_account=target).exists():
                form.add_error("account_number",
                    "You have already saved this account.")
            else:
                recipient = Recipient.objects.create(
                    user=request.user,
                    target_account=target,
                    nickname=form.cleaned_data.get("nickname", "").strip(),
                )
                audit_log("recipient_added", target=recipient)
                messages.success(request, "Recipient saved.")
                return redirect("account:recipients")
    else:
        form = RecipientForm()

    recipients = (Recipient.objects
                  .filter(user=request.user)
                  .select_related("target_account__user", "target_account__user__kyc"))

    context = {
        "account": account,
        "kyc": kyc,
        "form": form,
        "recipients": recipients,
        "max_recipients": MAX_RECIPIENTS_PER_USER,
    }
    return render(request, "account/recipients.html", context)


@login_required
@require_POST
def recipient_delete(request, pk):
    """Delete one of the current user's recipients. POST only.

    ``login_required`` sits outside ``require_POST`` on purpose: an anonymous
    GET is a sign-in redirect, an authenticated GET is a 405. The queryset is
    scoped to ``request.user``, so another user's pk is simply "not found"
    rather than a permission error that would confirm the row exists.
    """
    recipient = Recipient.objects.filter(pk=pk, user=request.user).first()
    if recipient is None:
        messages.warning(request, "Recipient not found.")
        return redirect("account:recipients")
    label = recipient.display_name
    audit_log("recipient_removed", metadata={"display_name": label})
    recipient.delete()
    messages.success(request, f"Removed {label}.")
    return redirect("account:recipients")


# =====================================================================
# Phase 5g-2  notifications
# =====================================================================
NOTIFICATIONS_PER_PAGE = 20


@login_required
def notifications_view(request):
    """The full notification list, newest first, paginated.

    Login-only, like ``account:recipients`` -- a notification feed is the
    user's own activity, not banking data, so it does not go through
    ``_kyc_required``.
    """
    account = getattr(request.user, "account", None)
    kyc = getattr(request.user, "kyc", None)

    qs = Notification.objects.filter(user=request.user)
    paginator = Paginator(qs, NOTIFICATIONS_PER_PAGE)
    page_obj = paginator.get_page(request.GET.get("page"))

    context = {
        "account": account,
        "kyc": kyc,
        "page_obj": page_obj,
        "unread_count": qs.filter(is_read=False).count(),
    }
    return render(request, "account/notifications.html", context)


def _safe_referer_redirect(request):
    """Send the user back to the page they came from, but only on this host.

    ``HTTP_REFERER`` is attacker-controlled, so it is validated with Django's
    own host/scheme check before use: a missing, malformed or off-host value
    falls back to the notifications list instead of turning this into an open
    redirect.
    """
    referer = request.META.get("HTTP_REFERER") or ""
    if referer and url_has_allowed_host_and_scheme(
        referer,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(referer)
    return redirect("account:notifications")


@login_required
@require_POST
def notification_mark_read(request, pk):
    """Mark one of the current user's notifications as read. POST only.

    ``login_required`` sits outside ``require_POST`` on purpose: an anonymous
    GET is a sign-in redirect, an authenticated GET is a 405. The queryset is
    scoped to ``request.user``, so another user's pk is a no-op rather than a
    permission error that would confirm the row exists.
    """
    updated = Notification.objects.filter(pk=pk, user=request.user).update(is_read=True)
    if updated == 0:
        messages.warning(request, "Notification not found.")
    return _safe_referer_redirect(request)


@login_required
@require_POST
def notification_mark_all_read(request):
    """Mark every unread notification of the current user as read. POST only."""
    Notification.objects.filter(user=request.user, is_read=False).update(is_read=True)
    messages.success(request, "All notifications marked as read.")
    return redirect("account:notifications")


# =====================================================================
# Phase 5h-2  support tickets
# =====================================================================
@login_required
def support_view(request):
    """List the user's tickets and render the create form inline.

    Adding a ticket is a POST to this view. Login-only, like
    ``account:recipients``: a support thread is the user's own activity, not
    banking data, so it does not go through ``_kyc_required``.
    """
    account = getattr(request.user, "account", None)
    kyc = getattr(request.user, "kyc", None)

    if request.method == "POST":
        form = SupportTicketForm(request.POST)
        if form.is_valid():
            ticket = SupportTicket.objects.create(
                user=request.user,
                subject=form.cleaned_data["subject"].strip(),
            )
            SupportReply.objects.create(
                ticket=ticket,
                author=request.user,
                body=form.cleaned_data["message"].strip(),
            )
            audit_log("support_ticket_created", target=ticket)
            # No ticket.save() on purpose: the ticket was just INSERTed with
            # auto_now's now(), and Phase 5h-1 established that creating a
            # reply does not move updated_at, so it is already correct.
            messages.success(request, f"Ticket #{ticket.pk} opened.")
            return redirect("account:support_detail", pk=ticket.pk)
    else:
        form = SupportTicketForm()

    tickets = (SupportTicket.objects
               .filter(user=request.user)
               .prefetch_related("replies"))

    context = {
        "account": account,
        "kyc": kyc,
        "form": form,
        "tickets": tickets,
        "open_count": sum(1 for t in tickets if t.is_open),
    }
    return render(request, "account/support_list.html", context)


@login_required
def support_detail(request, pk):
    """One ticket with its thread, plus a reply form (POST here)."""
    account = getattr(request.user, "account", None)
    kyc = getattr(request.user, "kyc", None)

    ticket = SupportTicket.objects.filter(pk=pk, user=request.user).first()
    if ticket is None:
        # Not-found and not-yours are indistinguishable on purpose, matching
        # the Phase 1.9 guards: another user's pk must not confirm the row.
        messages.warning(request, "Ticket not found.")
        return redirect("account:support")

    if request.method == "POST":
        # The template hides the reply form on a ticket that is not open; this
        # is the server-side half of that rule, so a hand-made POST cannot
        # append to a resolved or closed thread.
        if not ticket.is_open:
            messages.warning(
                request,
                "This ticket is closed. Open a new ticket if you need further help.",
            )
            return redirect("account:support_detail", pk=ticket.pk)

        form = SupportReplyForm(request.POST)
        if form.is_valid():
            reply = SupportReply.objects.create(
                ticket=ticket,
                author=request.user,
                body=form.cleaned_data["body"].strip(),
            )
            audit_log("support_reply_created", target=reply)
            # Carry-over from Phase 5h-1: SupportReply.save() does not touch
            # the parent, so bump it explicitly. Without this the thread would
            # not rise to the top of Meta.ordering = ["-updated_at"].
            ticket.save(update_fields=["updated_at"])
            messages.success(request, "Reply posted.")
            return redirect("account:support_detail", pk=ticket.pk)
    else:
        form = SupportReplyForm()

    replies = ticket.replies.select_related("author", "author__kyc")

    context = {
        "account": account,
        "kyc": kyc,
        "ticket": ticket,
        "replies": replies,
        "form": form,
    }
    return render(request, "account/support_detail.html", context)


# =====================================================================
# Phase E-2a  budget categories
# =====================================================================
@login_required
def categories_view(request):
    """List the user's categories and render the add form inline.

    POST to this view creates a new category. Slug is derived
    from the name; per-user uniqueness is enforced by the model
    constraint, caught here as IntegrityError and surfaced as a
    field error.
    """
    account = getattr(request.user, "account", None)
    kyc = getattr(request.user, "kyc", None)

    if request.method == "POST":
        form = CategoryForm(request.POST)
        if form.is_valid():
            name = form.cleaned_data["name"]
            slug = slugify(name)[:50] or "category"
            try:
                # The savepoint matters: a unique violation aborts the
                # surrounding transaction on PostgreSQL and under
                # ATOMIC_REQUESTS, and the re-render below would then raise
                # TransactionManagementError instead of showing the error.
                with transaction.atomic():
                    category = Category.objects.create(
                        user=request.user,
                        name=name,
                        slug=slug,
                        icon=form.cleaned_data["icon"],
                        color=form.cleaned_data["color"],
                    )
                audit_log("settings_changed", target=category,
                          metadata={"category": "created"})
                messages.success(request, f"Category “{category.name}” added.")
                return redirect("account:categories")
            except IntegrityError:
                form.add_error("name",
                    "You already have a category with this name.")
    else:
        form = CategoryForm()

    # Annotate each category with its usage count in one query.
    cats = (Category.objects
            .filter(user=request.user)
            .annotate(txn_count=Count("transactions")))

    context = {
        "account": account,
        "kyc": kyc,
        "form": form,
        "categories": cats,
    }
    return render(request, "account/categories.html", context)


@login_required
@require_POST
def category_delete(request, pk):
    """Delete one of the user's categories. POST-only.

    SET_NULL on Transaction.category means existing transactions
    are preserved; their category becomes NULL. The view warns
    the user if any transactions were attached.
    """
    category = Category.objects.filter(pk=pk, user=request.user).first()
    if category is None:
        messages.warning(request, "Category not found.")
        return redirect("account:categories")

    txn_count = category.transactions.count()
    name = category.name
    category.delete()
    audit_log("settings_changed", target=request.user,
              metadata={"category": "deleted", "name": name,
                        "detached_transactions": txn_count})

    if txn_count:
        messages.success(request,
            f"Category “{name}” removed. {txn_count} transaction"
            f"{'s' if txn_count != 1 else ''} moved to Uncategorized.")
    else:
        messages.success(request, f"Category “{name}” removed.")
    return redirect("account:categories")


# =====================================================================
# Phase F-2  savings goals
# =====================================================================
@login_required
def goals_view(request):
    """List the user's savings goals; POST creates a new one.

    Login-only, like categories and support: a savings goal is the user's
    own bookkeeping, not banking data, so it does not go through
    ``_kyc_required``. Progress is recorded by the user here; nothing in
    this module touches a balance or the transfer flow.
    """
    account = getattr(request.user, "account", None)
    kyc = getattr(request.user, "kyc", None)

    if request.method == "POST":
        form = SavingsGoalForm(request.POST)
        if form.is_valid():
            goal = form.save(commit=False)
            goal.user = request.user
            goal.save()
            audit_log("settings_changed", target=goal,
                      metadata={"goal": "created", "name": goal.name})
            messages.success(request, f"Goal “{goal.name}” created.")
            return redirect("account:goals")
    else:
        form = SavingsGoalForm()

    active = (SavingsGoal.objects
              .filter(user=request.user, is_completed=False)
              .order_by("-created_at"))
    completed = (SavingsGoal.objects
                 .filter(user=request.user, is_completed=True)
                 .order_by("-updated_at")[:20])

    context = {
        "account": account,
        "kyc": kyc,
        "form": form,
        "active_goals": active,
        "completed_goals": completed,
        "add_form": AddToGoalForm(),
    }
    return render(request, "account/goals.html", context)


@login_required
@require_POST
def goal_add(request, pk):
    """Add to a goal's current_amount. POST-only, scoped to user.

    ``login_required`` sits outside ``require_POST`` on purpose: an anonymous
    GET is a sign-in redirect, an authenticated GET is a 405. The queryset is
    scoped to ``request.user``, so another user's pk is a no-op rather than a
    permission error that would confirm the row exists.
    """
    goal = SavingsGoal.objects.filter(pk=pk, user=request.user).first()
    if goal is None:
        messages.warning(request, "Goal not found.")
        return redirect("account:goals")

    form = AddToGoalForm(request.POST)
    if not form.is_valid():
        messages.warning(request, "Please enter a valid amount.")
        return redirect("account:goals")

    goal.current_amount = (goal.current_amount or Decimal("0.00")) + form.cleaned_data["amount"]
    # Auto-complete when the target is reached.
    if goal.current_amount >= goal.target_amount and not goal.is_completed:
        goal.is_completed = True
    goal.save(update_fields=["current_amount", "is_completed", "updated_at"])

    audit_log("settings_changed", target=goal,
              metadata={"goal": "contrib", "amount": str(form.cleaned_data["amount"])})
    messages.success(request, f"Added to “{goal.name}”.")
    return redirect("account:goals")


@login_required
@require_POST
def goal_complete(request, pk):
    """Toggle is_completed on the goal. POST-only, scoped to user.

    A toggle rather than a one-way "complete": the completed list offers a
    "Reopen" button, and both directions go through this one view so the
    audit entry reads the same either way.
    """
    goal = SavingsGoal.objects.filter(pk=pk, user=request.user).first()
    if goal is None:
        messages.warning(request, "Goal not found.")
        return redirect("account:goals")
    goal.is_completed = not goal.is_completed
    goal.save(update_fields=["is_completed", "updated_at"])
    audit_log("settings_changed", target=goal,
              metadata={"goal": "toggled", "completed": goal.is_completed})
    return redirect("account:goals")


@login_required
@require_POST
def goal_delete(request, pk):
    """Delete one of the user's goals. POST-only, scoped to user.

    The audit entry targets the user, not the goal: the row is gone by the
    time the entry is written, so its pk would point at nothing.
    """
    goal = SavingsGoal.objects.filter(pk=pk, user=request.user).first()
    if goal is None:
        messages.warning(request, "Goal not found.")
        return redirect("account:goals")
    name = goal.name
    goal.delete()
    audit_log("settings_changed", target=request.user,
              metadata={"goal": "deleted", "name": name})
    messages.success(request, f"Goal “{name}” deleted.")
    return redirect("account:goals")
