from django.shortcuts import render, redirect
from account.models import Account
from django.contrib.auth.decorators import login_required
from django.db import transaction as db_transaction
from django.db.models import Q
from django.contrib import messages
from core.models import Transaction
from core.security import AmountError, find_party_transaction, parse_amount
from audit.utils import log as audit_log
from core.ratelimit import rate_limit, user_key

# Apply the login_required decorator to the function
@login_required
def search_users_account_number(request):
    # Get all Account objects
    account = Account.objects.all()

    # Get the value of 'account_number' from the POST request
    query = request.POST.get("account_number")

    # Check if a query was provided
    if query:
        # Filter the accounts based on account_number or account_id using OR logic
        account = account.filter(
            Q(account_number=query) |
            Q(account_id=query)
        ).distinct()

    # Prepare context data to pass to the template
    context = {
        "account": account,
        "query": query,
    }
    # Render the search_users_account_number template with the context data
    return render(request, "transfer/search_users_account_number.html", context)

# Define a view function for transferring amounts to a user's account
@login_required
def AmountTransfer(request, account_number):
    try:
        # Attempt to retrieve an Account object with the given account_number
        account = Account.objects.get(account_number=account_number)
    except:
        # If the account doesn't exist, show a warning message and redirect
        messages.warning(request, "Account does not exist.")
        return redirect("core:search-account")

    # Prepare context data to pass to the template
    context = {
        "account": account,
    }
    # Render the amount-transfer template with the context data
    return render(request, "transfer/amount-transfer.html", context)

@login_required
def process_amount_transfer(request, account_number):
    # Phase 1.10: .get() raised Account.DoesNotExist -- a hard 500 -- when the
    # URL named an account that does not exist. Same guard as the payment_request
    # sweep and as TransferProcess: an unknown account and an unknown transaction
    # are reported identically, so this URL is not an account-number oracle.
    account = Account.objects.filter(account_number=account_number).first()
    if account is None:
        messages.warning(request, "Transaction does not exist.")
        return redirect("core:transactions")

    sender = request.user  # Get the person that is logged in
    receiver = account.user  # Get the person that is going to receive the money

    sender_account = sender.account  # Get the currently logged in user's account that would send the money
    receiver_account = account  # Get the recipient's account that would receive the money

    # Refuse to send money to yourself. Without this guard the debit and the
    # credit land on the same account row, so the confirmation step would
    # complete a "transfer" that moves nothing.
    if account.pk == sender_account.pk:
        messages.warning(request, "You cannot transfer money to your own account.")
        return redirect("core:search-account")

    if request.method == "POST":
        try:
            amount = parse_amount(request.POST.get("amount-send"), field_name="Amount to send")
        except AmountError as exc:
            messages.warning(request, str(exc))
            return redirect("core:amount-transfer", account.account_number)

        description = request.POST.get("description", "")

        if sender_account.account_balance >= amount:
            new_transaction = Transaction.objects.create(
                user=request.user,
                amount=amount,
                description=description,
                reciever=receiver,
                sender=sender,
                sender_account=sender_account,
                reciever_account=receiver_account,
                status="processing",
                transaction_type="transfer",
            )
            new_transaction.save()
            audit_log("transfer_created", target=new_transaction)

            # Get the ID of the transaction that was created now
            transaction_id = new_transaction.transaction_id
            return redirect("core:transfer-confirmation", account.account_number, transaction_id)
        else:
            messages.warning(request, "Insufficient Funds.")
            return redirect("core:amount-transfer", account.account_number)
    else:
        messages.warning(request, "An error occurred. Please try again later.")
        return redirect("core:amount-transfer", account.account_number)

# function TransferConfirmation that takes three parameters: request, account_number, and transaction_id
@login_required
def TransferConfirmation(request, account_number, transaction_id):
    # Try to retrieve an Account object with the given account_number from the database
    account = Account.objects.filter(account_number=account_number).first()

    # Scoped to the requesting user's own transactions (see core.security), so
    # another user's transaction_id behaves exactly like one that does not exist.
    transaction = find_party_transaction(request.user, transaction_id)

    if account is None or transaction is None:
        # If either lookup comes up empty, do the following:

        # Display a warning message using Django's messages framework
        messages.warning(request, "Transaction does not exist.")

        # Redirect the user to the "account:account" URL, which displays account information
        return redirect("account:account")

    # Phase 1.6 (F1): this page is the only UI route into TransferProcess, so a
    # finished transfer must not be re-confirmable. Without this guard a browser
    # Back after a completed transfer returned a working password form, and
    # submitting it moved the money a second time.
    if transaction.status != "processing":
        messages.warning(request, "This transfer has already been processed.")
        return redirect("core:transaction-detail", transaction.transaction_id)

    # If the try-except block does not raise an exception, create a dictionary context
    context = {
        "account": account,
        "transaction": transaction,
    }

    # Render the "transfer/transfer-confirmation.html" template with the provided context
    return render(request, "transfer/transfer-confirmation.html", context)


@rate_limit(user_key("transfer"), limit=10, window=3600, label="transfer")
@login_required
def TransferProcess(request, account_number, transaction_id):
    account = Account.objects.filter(account_number=account_number).first()

    # Scoped to the requesting user's own transactions (see core.security).
    transaction = find_party_transaction(request.user, transaction_id)

    if account is None or transaction is None:
        messages.warning(request, "Transaction does not exist.")
        return redirect("account:account")
    
    sender = request.user   
    receiver = account.user  

    sender_account = sender.account  
    receiver_account = account 

    if request.method == "POST":
        submitted_password = request.POST.get("password")

        if not submitted_password:
            messages.warning(request, "Please enter your password.")
            return redirect("core:transfer-confirmation", account.account_number, transaction.transaction_id)

        if request.user.check_password(submitted_password):
            insufficient = False
            already_processed = False
            mismatch = False
            with db_transaction.atomic():
                # Lock both account rows in a deterministic (primary key) order
                # so two concurrent transfers in opposite directions cannot
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
                # Its status used to be written but never checked, so a second
                # POST of this URL re-applied the debit and the credit -- reachable
                # by accident (browser Back, then Submit again) as well as on
                # purpose. Only "processing" is settleable, which is the status
                # process_amount_transfer creates.
                locked_txn = (
                    Transaction.objects.select_for_update()
                    .filter(pk=transaction.pk)
                    .first()
                )

                # Phase 1.8: URL/transaction mismatch guard.
                # The account_number in the URL must be the transaction's
                # intended receiver account. Without this, either party could
                # POST the URL with a different account_number and redirect the
                # credit to a third party while the transaction row kept
                # recording the original counterparty.
                if locked_txn is None:
                    already_processed = True
                elif (
                    locked_txn.reciever_account_id != account.pk
                    or locked_txn.sender_account_id != sender_account.pk
                    or locked_txn.sender_id != request.user.id
                ):
                    mismatch = True
                elif locked_txn.status != "processing":
                    already_processed = True
                # The balance was only checked when the transaction was created.
                # Re-check it against the LOCKED row, because the sender may have
                # spent the money in the meantime. Rejecting here leaves the
                # transaction in "processing" so the user can fund the account
                # and retry the same transfer.
                elif sender_row.account_balance < locked_txn.amount:
                    insufficient = True
                else:
                    locked_txn.status = "completed"
                    locked_txn.save()

                    # Remove the amount from the sender, add it to the receiver.
                    sender_row.account_balance -= locked_txn.amount
                    sender_row.save()

                    receiver_row.account_balance += locked_txn.amount
                    receiver_row.save()

                    audit_log("transfer_confirmed", target=locked_txn)  # in-atomic: rolls back with the money
            if mismatch:
                messages.warning(request, "This transaction does not match the account in the link.")
                return redirect("core:transactions")

            if already_processed:
                messages.warning(request, "This transfer has already been processed.")
                return redirect("core:transaction-detail", transaction.transaction_id)

            if insufficient:
                messages.warning(request, "Insufficient Funds.")
                return redirect("core:transfer-confirmation", account.account_number, transaction.transaction_id)

            messages.success(request, "Transfer Successfull.")
            return redirect("core:transfer-completed", account.account_number, transaction.transaction_id)
        else:
            messages.warning(request, "Incorrect password.")
            return redirect('core:transfer-confirmation', account.account_number, transaction.transaction_id)
    else:
        messages.warning(request, "An error occured, Try again later.")
        return redirect('account:account')
    

@login_required
def TransferCompleted(request, account_number, transaction_id):
    account = Account.objects.filter(account_number=account_number).first()

    # Scoped to the requesting user's own transactions (see core.security).
    transaction = find_party_transaction(request.user, transaction_id)

    if account is None or transaction is None:
        messages.warning(request, "Transfer does not exist.")
        return redirect("account:account")
    context = {
        "account":account,
        "transaction":transaction
    }
    return render(request, "transfer/transfer-completed.html", context)

