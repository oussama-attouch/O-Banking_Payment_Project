from decimal import Decimal

from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.utils import timezone

from account import analytics
from account.models import KYC, Account
from account.forms import KYCForm
from django.contrib import messages
from django.contrib.auth.decorators import login_required

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


@login_required
def dashboard(request):
    """The expanded dashboard: KPIs, charts data, and a filterable history."""
    blocked = _kyc_required(request)
    if blocked:
        return blocked

    kyc = KYC.objects.get(user=request.user)
    account = Account.objects.get(user=request.user)

    history_status = (request.GET.get("status") or "").strip()
    history_type = (request.GET.get("type") or "").strip()
    try:
        page = int(request.GET.get("page", 1))
    except (TypeError, ValueError):
        page = 1

    context = {
        "kyc": kyc,
        "account": account,
        "kpis": analytics.get_kpis(request.user, account=account),
        "daily_flow_90d": analytics.get_daily_net_flow(request.user),
        "weekly_volume_12w": analytics.get_weekly_volume(request.user),
        "status_breakdown": analytics.get_status_breakdown(request.user),
        "recent_transactions": analytics.get_recent_transactions(request.user),
        "top_counterparties": analytics.get_top_counterparties(request.user),
        "kyc_status": analytics.get_kyc_status(request.user, account=account, has_kyc=True),
        "transaction_history": analytics.get_transaction_history(
            request.user,
            status=history_status or None,
            ttype=history_type or None,
            page=page,
        ),
        "history_status": history_status,
        "history_type": history_type,
        "history_choices": analytics.STATUS_CHOICES,
        "history_type_choices": analytics.TYPE_CHOICES,
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
    """
    blocked = _kyc_required(request)
    if blocked:
        return blocked

    account = Account.objects.get(user=request.user)

    payload = {
        "generated_at": timezone.now().isoformat(),
        "kpis": _json_ready(analytics.get_kpis(request.user, account=account)),
        "daily_flow_90d": _json_ready(analytics.get_daily_net_flow(request.user)),
        "weekly_volume_12w": _json_ready(analytics.get_weekly_volume(request.user)),
        "status_breakdown": analytics.get_status_breakdown(request.user),
        "top_counterparties": _json_ready(analytics.get_top_counterparties(request.user)),
        "kyc_status": analytics.get_kyc_status(request.user, account=account, has_kyc=True),
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
