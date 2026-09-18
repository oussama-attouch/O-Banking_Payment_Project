from django.shortcuts import render, redirect
from account.models import Account  # Import the Account model from the 'account' app
from django.contrib.auth.decorators import login_required  # Decorator for authentication
from django.db.models import Q  # Import the Q object for complex queries
from django.contrib import messages  # Import messages module for user notifications
from core.models import Transaction  # Import the Transaction model from the 'core' app
from decimal import Decimal  # Import the Decimal class for precise decimal arithmetic

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
def AmountRequest(request, account_number):
    # Retrieve the account associated with the provided account_number
    account = Account.objects.get(account_number=account_number)
    
    # Create a context dictionary to pass the account data to the template
    context = {
        "account": account,  # Pass the account object
    }

    # Render the 'amount-request.html' template with the context data
    return render(request, "payment_request/amount-request.html", context)

def AmountRequestProcess(request, account_number):
    account = Account.objects.get(account_number=account_number)

    sender = request.user
    reciever = account.user

    sender_account = request.user.account
    reciever_account = account.user.account

    if request.method == "POST":
        amount = request.POST.get("amount-request")
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

def AmountRequestConfirmation(request,account_number,transaction_id):
    account = Account.objects.get(account_number=account_number)
    transaction = Transaction.objects.get(transaction_id=transaction_id)

    context = {
        "account": account,  # Pass the account object
        "transaction": transaction,
    }
    return render(request,"payment_request/amount-request-confirmation.html",context)


def AmountRequestFinalProcess(request, account_number,transaction_id):
    account = Account.objects.get(account_number=account_number)
    transaction = Transaction.objects.get(transaction_id=transaction_id)

    if request.method == "POST":
        pin_number = request.POST.get("pin-number")
        if pin_number == request.user.account.account_pin:
            transaction.status = "request_sent"
            transaction.save()

            messages.success(request,"Your Payment request have been sent successfully.")
            return redirect("core:amount-request-completed",account.account_number,transaction.transaction_id)
        else:
            messages.warning(request,"An Error Occured, Try again later.")
            return redirect("account:dashboard")
        
def RequestCompleted(request,transaction_id,account_number):
    transaction = Transaction.objects.get(transaction_id=transaction_id)
    account = Account.objects.get(account_number=account_number)

    context = {
            "account": account,  # Pass the account object
            "transaction": transaction,
        }
    return render(request,"payment_request/amount-request-completed.html",context)


##### Settled ####

def Settlement_confirmation(request,account_number,transaction_id):
    transaction = Transaction.objects.get(transaction_id=transaction_id)
    account = Account.objects.get(account_number=account_number)

    context = {
            "account": account,  # Pass the account object
            "transaction": transaction,
        }
    return render(request,"payment_request/settlement-confirmation.html",context)

def Settlement_processing(request,account_number,transaction_id):
    account = Account.objects.get(account_number=account_number)
    transaction = Transaction.objects.get(transaction_id=transaction_id)

    sender = request.user 
    sender_account = request.user.account 

    if request.method == "POST":
        pin_number = request.POST.get("pin-number")
        if pin_number == sender_account.account_pin:
            if sender_account.account_balance <= 0 or sender_account.account_balance < transaction.amount:
                messages.warning(request,"Insufficient Funds, Fund your account and try again.")
            else:
                sender_account.account_balance -= transaction.amount 
                sender_account.save() 

                account.account_balance += transaction.amount
                account.save()

                transaction.status = "request_settled"
                transaction.save()

                messages.success(request,f"Settled to {account.user.kyc.full_name} was successfull.")
                return redirect("core:settlement-completed")
        else:
            messages.warning(request,"Incorrect Pin")
            return redirect("core:settlement-confirmation",account.account_number,transaction.transaction_id)
    else:
        messages.warning(request,"Error Occured")
        return redirect("core:settlement-completed",account.account_number,transaction.transaction_id)
    
    
def SettlementCompleted(request,transaction_id,account_number):
    transaction = Transaction.objects.get(transaction_id=transaction_id)
    account = Account.objects.get(account_number=account_number)

    context = {
            "account": account,  # Pass the account object
            "transaction": transaction,
        }
    return render(request,"payment_request/settlement-completed.html",context)


def DeletePaymentRequest(request,account_number,transaction_id):
    transaction = Transaction.objects.get(transaction_id=transaction_id)
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








