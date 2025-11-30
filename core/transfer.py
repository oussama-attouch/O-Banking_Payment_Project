from django.shortcuts import render, redirect
from account.models import Account
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.contrib import messages
from core.models import Transaction
from decimal import Decimal

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
def AmountTransfer(request, account_number):
    try:
        # Attempt to retrieve an Account object with the given account_number
        account = Account.objects.get(account_number=account_number)
    except:
        # If the account doesn't exist, show a warning message and redirect
        messages.warning(request, "Account does not exist.")
        return redirect("core:search-user-by-account-number")

    # Prepare context data to pass to the template
    context = {
        "account": account,
    }
    # Render the amount-transfer template with the context data
    return render(request, "transfer/amount-transfer.html", context)

# @login_required
def process_amount_transfer(request, account_number):
    account = Account.objects.get(account_number=account_number)
    sender = request.user  # Get the person that is logged in
    receiver = account.user  # Get the person that is going to receive the money

    sender_account = sender.account  # Get the currently logged in user's account that would send the money
    receiver_account = account  # Get the recipient's account that would receive the money

    if request.method == "POST":
        amount = float(request.POST.get("amount-send", 0))
        description = request.POST.get("description", "")

        print(amount)
        print(description)

        if sender_account.account_balance >= Decimal(amount):
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

            # Get the ID of the transaction that was created now
            transaction_id = new_transaction.transaction_id
            return redirect("core:TransferConfirmation", account.account_number, transaction_id)
        else:
            messages.warning(request, "Insufficient Funds.")
            return redirect("core:amount-transfer", account.account_number)
    else:
        messages.warning(request, "An error occurred. Please try again later.")
        return redirect("core:amount-transfer", account.account_number)

# function TransferConfirmation that takes three parameters: request, account_number, and transaction_id
def TransferConfirmation(request, account_number, transaction_id):
    try:
        # Try to retrieve an Account object with the given account_number from the database
        account = Account.objects.get(account_number=account_number)

        # Try to retrieve a Transaction object with the given transaction_id from the database
        transaction = Transaction.objects.get(transaction_id=transaction_id)

    except:
        # If either of the database queries fails (an exception is raised), do the following:

        # Display a warning message using Django's messages framework
        messages.warning(request, "Transaction does not exist.")

        # Redirect the user to the "account:account" URL, which displays account information
        return redirect("account:account")

    # If the try-except block does not raise an exception, create a dictionary context
    context = {
        "account": account,
        "transaction": transaction,
    }

    # Render the "transfer/transfer-confirmation.html" template with the provided context
    return render(request, "transfer/transfer-confirmation.html", context)


def TransferProcess(request, account_number, transaction_id):
    account= Account.objects.get(account_number=account_number)
    transaction = Transaction.objects.get(transaction_id=transaction_id)
    
    sender = request.user   
    receiver = account.user  

    sender_account = sender.account  
    receiver_account = account 

    completed = False
    
    if request.method == "POST":
        pin_number = request.POST.get("pin-number")
        print(pin_number)

        if pin_number == sender_account.account_pin:
            transaction.status = "completed"
            transaction.save()

            #Remove the amount that i am sending from my account balance update my account balance   
            sender_account.account_balance -= transaction.amount
            sender_account.save()

            #Add the amount that was removed from my account to the person that i am sending the money to
            account.account_balance += transaction.amount
            account.save()

            messages.success(request, "Transfer Successfull.")
            return redirect("core:transfer-completed", account.account_number, transaction.transaction_id)
        else:
            messages.warning(request, "Incorrect Pin.")
            return redirect('core:transfer-confirmation', account.account_number, transaction.transaction_id)
    else:
        messages.warning(request, "An error occured, Try again later.")
        return redirect('account:account')
    

def TransferCompleted(request, account_number, transaction_id):
    try:
        account = Account.objects.get(account_number=account_number)
        transaction = Transaction.objects.get(transaction_id=transaction_id)
    except:
        messages.warning(request, "Transfer does not exist.")
        return redirect("account:account")
    context = {
        "account":account,
        "transaction":transaction
    }
    return render(request, "transfer/transfer-completed.html", context)

