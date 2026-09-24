from django.shortcuts import render, redirect
from account.models import Account  # Import the Account model from the 'account' app
from django.contrib.auth.decorators import login_required  # Decorator for authentication
from django.db import transaction as db_transaction
from django.db.models import Q  # Import the Q object for complex queries
from django.contrib import messages  # Import messages module for user notifications
from core.models import Transaction  # Import the Transaction model from the 'core' app
from decimal import Decimal  # Import the Decimal class for precise decimal arithmetic
from core.security import AmountError, find_party_transaction, parse_amount
from django.views.decorators.http import require_POST
from audit.utils import log as audit_log

# Require authentication for this view using the @login_required decorator
@login_required
def searchUsersRequest(request):
    # Retrieve all accounts
    account = Account.objects.all()
    
    # Get the 'account_number' from the POST data
    query = request.POST.get("account_number")

    if query:
        # Filter accounts based on the 'account_number' using the Q object and make them distinct
        account = account.filter(Q(account_number=query)).distinct()

    # Create a context dictionary to pass data to the template
    context = {
        "account": account,  # Pass the filtered or unfiltered account data
        "query": query,  # Pass the query for display in the template
    }

    # Render the 'search-users.html' template with the context data
    return render(request, "payment_request/search-users.html", context)

# Define a view to handle payment requests with a specific account number
@login_required
def AmountRequest(request, account_number):
    # Phase 1.10: an unknown account_number used to raise Account.DoesNotExist
    # -- a hard 500 -- before anything could reject it. Same guard as
    # Settlement_processing (Phase 1.9): an unknown account and an unknown
    # transaction are reported identically, so this URL cannot be used to test
    # which account numbers exist.
    account = Account.objects.filter(account_number=account_number).first()
    if account is None:
        messages.warning(request, "Transaction does not exist.")
        return redirect("core:transactions")

    # Create a context dictionary to pass the account data to the template
    context = {
        "account": account,  # Pass the account object
    }

    # Render the 'amount-request.html' template with the context data
    return render(request, "payment_request/amount-request.html", context)

@login_required
def AmountRequestProcess(request, account_number):
    account = Account.objects.filter(account_number=account_number).first()
    if account is None:
        messages.warning(request, "Transaction does not exist.")
        return redirect("core:transactions")

    sender = request.user
    reciever = account.user

    sender_account = request.user.account
    reciever_account = account.user.account

    if request.method == "POST":
        try:
            amount = parse_amount(request.POST.get("amount-request"), field_name="Amount to request")
        except AmountError as exc:
            messages.warning(request, str(exc))
            return redirect("core:amount-request", account.account_number)

        description = request.POST.get("description")

        new_request = Transaction.objects.create(
            user=request.user,
            amount=amount,
            description=description,
            sender=sender,
            reciever=reciever,
            sender_account=sender_account,
            reciever_account=reciever_account,
            status="request_processing",
            transaction_type="request"
        )
        new_request.save()
        audit_log("request_created", target=new_request)
        transaction_id = new_request.transaction_id
        return redirect("core:amount-request-confirmation", account.account_number, transaction_id)
    else:
        messages.warning(request, "Error Occurred, Try again later.")
        return redirect("account:dashboard")

@login_required
def AmountRequestConfirmation(request,account_number,transaction_id):
    # Scoped to the requesting user's own transactions (see core.security).
    transaction = find_party_transaction(request.user, transaction_id)
    if transaction is None:
        messages.warning(request, "Transaction does not exist.")
        return redirect("core:transactions")
    account = Account.objects.filter(account_number=account_number).first()
    if account is None:
        messages.warning(request, "Transaction does not exist.")
        return redirect("core:transactions")

    context = {
        "account": account,  # Pass the account object
        "transaction": transaction,
    }
    return render(request,"payment_request/amount-request-confirmation.html",context)


@login_required
def AmountRequestFinalProcess(request, account_number,transaction_id):
    # Scoped to the requesting user's own transactions (see core.security).
    transaction = find_party_transaction(request.user, transaction_id)
    if transaction is None:
        messages.warning(request, "Transaction does not exist.")
        return redirect("core:transactions")
    account = Account.objects.filter(account_number=account_number).first()
    if account is None:
        messages.warning(request, "Transaction does not exist.")
        return redirect("core:transactions")

    if request.method == "POST":
        submitted_password = request.POST.get("password")
        if not submitted_password:
            messages.warning(request, "Please enter your password.")
            return redirect("core:amount-request-confirmation", account.account_number, transaction.transaction_id)
        if request.user.check_password(submitted_password):
            transaction.status = "request_sent"
            transaction.save()
            audit_log("request_sent", target=transaction)

            messages.success(request,"Your Payment request have been sent successfully.")
            return redirect("core:amount-request-completed",account.account_number,transaction.transaction_id)
        else:
            messages.warning(request,"Incorrect password.")
            return redirect("core:amount-request-confirmation",account.account_number,transaction.transaction_id)
        
@login_required
def RequestCompleted(request,transaction_id,account_number):
    # Scoped to the requesting user's own transactions (see core.security).
    transaction = find_party_transaction(request.user, transaction_id)
    if transaction is None:
        messages.warning(request, "Transaction does not exist.")
        return redirect("core:transactions")
    account = Account.objects.filter(account_number=account_number).first()
    if account is None:
        messages.warning(request, "Transaction does not exist.")
        return redirect("core:transactions")

    context = {
            "account": account,  # Pass the account object
            "transaction": transaction,
        }
    return render(request,"payment_request/amount-request-completed.html",context)


##### Settled ####

@login_required
def Settlement_confirmation(request,account_number,transaction_id):
    # Scoped to the requesting user's own transactions (see core.security).
    transaction = find_party_transaction(request.user, transaction_id)
    if transaction is None:
        messages.warning(request, "Transaction does not exist.")
        return redirect("core:transactions")
    account = Account.objects.filter(account_number=account_number).first()
    if account is None:
        messages.warning(request, "Transaction does not exist.")
        return redirect("core:transactions")

    # Phase 1.7 (F4): mirror the TransferConfirmation guard from Phase 1.6. Only a
    # request that has actually been sent is settleable, so a finished one must not
    # offer the password form again. transaction_list.html only renders the Settle
    # button for "request_sent", so this closes the direct-URL path behind that.
    if transaction.status != "request_sent":
        messages.warning(request, "This settlement has already been processed.")
        return redirect("core:transaction-detail", transaction.transaction_id)

    context = {
            "account": account,  # Pass the account object
            "transaction": transaction,
        }
    return render(request,"payment_request/settlement-confirmation.html",context)

@login_required
def Settlement_processing(request,account_number,transaction_id):
    # Scoped to the requesting user's own transactions (see core.security).
    transaction = find_party_transaction(request.user, transaction_id)
    if transaction is None:
        messages.warning(request, "Transaction does not exist.")
        return redirect("core:transactions")
    # Phase 1.9: .get() raised Account.DoesNotExist -- a hard 500 -- when the
    # URL named an account that does not exist. Use the same shape as
    # TransferProcess (filter().first()), and the same message its sibling
    # guard two lines above uses, so an unknown account and an unknown
    # transaction are indistinguishable to the caller: a distinct "account
    # does not exist" reply would turn this URL into an account-number oracle
    # (see core/security.py on keeping existence and authorization merged).
    account = Account.objects.filter(account_number=account_number).first()
    if account is None:
        messages.warning(request, "Transaction does not exist.")
        return redirect("core:transactions")

    sender = request.user 
    sender_account = request.user.account 

    if request.method == "POST":
        submitted_password = request.POST.get("password")
        if not submitted_password:
            messages.warning(request, "Please enter your password.")
            return redirect("core:settlement-confirmation", account.account_number, transaction.transaction_id)
        if request.user.check_password(submitted_password):
            insufficient = False
            already_processed = False
            mismatch = False
            with db_transaction.atomic():
                # Lock both account rows in a deterministic (primary key) order
                # so two concurrent settlements in opposite directions cannot
                # deadlock on each other.
                locked = {
                    str(row.pk): row
                    for row in Account.objects.select_for_update()
                    .filter(pk__in=[sender_account.pk, account.pk])
                    .order_by("pk")
                }
                sender_row = locked[str(sender_account.pk)]
                receiver_row = locked[str(account.pk)]

                # Phase 1.6 (F1): re-read the transaction under the same lock.
                # As in TransferProcess, the status was written but never checked,
                # so re-POSTing this URL settled the same request repeatedly. Only
                # "request_sent" is settleable -- that is what
                # AmountRequestFinalProcess sets once the request has been sent.
                locked_txn = (
                    Transaction.objects.select_for_update()
                    .filter(pk=transaction.pk)
                    .first()
                )

                # Phase 1.8: URL/transaction mismatch guard.
                # A settlement is paid by the stored reciever and credited to
                # the stored sender (the requester), so the URL must name the
                # requester's account and the caller must be the payer. Without
                # this, the payer could POST the URL with a third party's
                # account_number and redirect the credit away from the requester.
                if locked_txn is None:
                    already_processed = True
                elif (
                    locked_txn.sender_account_id != account.pk
                    or locked_txn.reciever_account_id != sender_account.pk
                    or locked_txn.reciever_id != request.user.id
                ):
                    mismatch = True
                elif locked_txn.status != "request_sent":
                    already_processed = True
                # Re-checked against the LOCKED row, so the check and the debit
                # cannot be raced by a concurrent balance change.
                elif sender_row.account_balance <= 0 or sender_row.account_balance < locked_txn.amount:
                    insufficient = True
                else:
                    sender_row.account_balance -= locked_txn.amount
                    sender_row.save()

                    receiver_row.account_balance += locked_txn.amount
                    receiver_row.save()

                    locked_txn.status = "request_settled"
                    locked_txn.save()

                    audit_log("settlement_confirmed", target=locked_txn)  # in-atomic: rolls back with the money
            if mismatch:
                messages.warning(request, "This transaction does not match the account in the link.")
                return redirect("core:transactions")

            if already_processed:
                messages.warning(request, "This settlement has already been processed.")
                return redirect("core:transaction-detail", transaction.transaction_id)

            if insufficient:
                messages.warning(request,"Insufficient Funds, Fund your account and try again.")
                return redirect("core:settlement-confirmation", account.account_number, transaction.transaction_id)

            # Phase 1.7 (F3): a payee with no KYC row made this line raise
            # RelatedObjectDoesNotExist *inside the view*, which is a hard 500 --
            # Django silences that exception for template lookups, but this is
            # direct Python attribute access. Fall back to the username.
            holder = (
                getattr(getattr(account.user, "kyc", None), "full_name", None)
                or account.user.username
            )
            messages.success(request, f"Settled to {holder} was successful.")
            return redirect("core:settlement-completed", account.account_number, transaction.transaction_id)
        else:
            messages.warning(request,"Incorrect password.")
            return redirect("core:settlement-confirmation",account.account_number,transaction.transaction_id)
    else:
        messages.warning(request,"Error Occured")
        return redirect("core:settlement-completed",account.account_number,transaction.transaction_id)
    
    
@login_required
def SettlementCompleted(request,transaction_id,account_number):
    # Scoped to the requesting user's own transactions (see core.security).
    transaction = find_party_transaction(request.user, transaction_id)
    if transaction is None:
        messages.warning(request, "Transaction does not exist.")
        return redirect("core:transactions")
    account = Account.objects.filter(account_number=account_number).first()
    if account is None:
        messages.warning(request, "Transaction does not exist.")
        return redirect("core:transactions")

    context = {
            "account": account,  # Pass the account object
            "transaction": transaction,
        }
    return render(request,"payment_request/settlement-completed.html",context)


@login_required
@require_POST
def DeletePaymentRequest(request,account_number,transaction_id):
    # Scoped to the requesting user's own transactions (see core.security).
    # Deletion itself is still restricted to the transaction's owner below.
    transaction = find_party_transaction(request.user, transaction_id)
    if transaction is None:
        messages.warning(request, "Transaction does not exist.")
        return redirect("core:transactions")
    account = Account.objects.filter(account_number=account_number).first()
    if account is None:
        messages.warning(request, "Transaction does not exist.")
        return redirect("core:transactions")

    if request.user == transaction.user:
        transaction.delete()
        messages.success(request,"Paymenet request Deleted successfully.")
        return redirect("core:transactions")
    context = {
            "account": account,  # Pass the account object
            "transaction": transaction,
        }
    return render(request,"payment_request/delete-request.html",context)








