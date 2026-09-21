from django.shortcuts import get_object_or_404, render, redirect
from core.models import Transaction 
from core.security import party_transaction_filter
from account.models import Account
from django.contrib.auth.decorators import login_required
from django.contrib import messages

@login_required
def transaction_lists(request):
    sender_transaction = Transaction.objects.filter(sender=request.user,transaction_type="transfer").order_by("-id")
    reciever_transaction = Transaction.objects.filter(reciever=request.user,transaction_type="transfer").order_by("-id")

    request_sender_transaction = Transaction.objects.filter(sender=request.user,transaction_type="request")
    request_reciever_transaction = Transaction.objects.filter(reciever=request.user,transaction_type="request")

    context = {
        "sender_transaction":sender_transaction,
        "reciever_transaction":reciever_transaction,

        "request_sender_transaction":request_sender_transaction,
        "request_reciever_transaction":request_reciever_transaction,
    }

    return render(request,"transaction/transaction_list.html",context)

@login_required
def transaction_detail(request, transaction_id):
    # Scoped to the requesting user: someone else's transaction ID yields 404,
    # exactly as a non-existent ID does, so detail pages cannot be enumerated.
    transaction = get_object_or_404(
        Transaction, party_transaction_filter(request.user, transaction_id)
    )

    context = {
        "transaction":transaction,
    }

    return render(request, "transaction/transaction-detail.html", context)