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
    # Retrieve the account associated with the provided account_number
    account = Account.objects.get(account_number=account_number)
    
    # Create a context dictionary to pass the account data to the template
    context = {
        "account": account,  # Pass the account object
    }

    # Render the 'amount-request.html' template with the context data
    return render(request, "payment_request/amount-request.html", context)

@login_required
def AmountRequestProcess(request, account_number):
    account = Account.objects.get(account_number=account_number)

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
    account = Account.objects.get(account_number=account_number)

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
    account = Account.objects.get(account_number=account_number)

    if request.method == "POST":
        submitted_password = request.POST.get("password")
        if not submitted_password:
            messages.warning(request, "Please enter your password.")
            return redirect("core:amount-request-confirmation", account.account_number, transaction.transaction_id)
        if request.user.check_password(submitted_password):
            transaction.status = "request_sent"
            transaction.save()

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
    account = Account.objects.get(account_number=account_number)

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
    account = Account.objects.get(account_number=account_number)

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
    account = Account.objects.get(account_number=account_number)

    sender = request.user 
    sender_account = request.user.account 

    if request.method == "POST":
        submitted_password = request.POST.get("password")
        if not submitted_password:
            messages.warning(request, "Please enter your password.")
            return redirect("core:settlement-confirmation", account.account_number, transaction.transaction_id)
        if request.user.check_password(submitted_password):
            insufficient = False
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

                # Re-checked against the LOCKED row, so the check and the debit
                # cannot be raced by a concurrent balance change.
                if sender_row.account_balance <= 0 or sender_row.account_balance < transaction.amount:
                    insufficient = True
                else:
                    sender_row.account_balance -= transaction.amount
                    sender_row.save()

                    receiver_row.account_balance += transaction.amount
                    receiver_row.save()

                    transaction.status = "request_settled"
                    transaction.save()

            if insufficient:
                messages.warning(request,"Insufficient Funds, Fund your account and try again.")
                return redirect("core:settlement-confirmation", account.account_number, transaction.transaction_id)

            messages.success(request,f"Settled to {account.user.kyc.full_name} was successfull.")
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
    account = Account.objects.get(account_number=account_number)

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
    account = Account.objects.get(account_number=account_number)

    if request.user == transaction.user:
        transaction.delete()
        messages.success(request,"Paymenet request Deleted successfully.")
        return redirect("core:transactions")
    context = {
            "account": account,  # Pass the account object
            "transaction": transaction,
        }
    return render(request,"payment_request/delete-request.html",context)








