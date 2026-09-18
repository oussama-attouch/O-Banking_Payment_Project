from django.db import models
from userauths.models import User
from account.models import Account
from shortuuid.django_fields import ShortUUIDField

# Define choices for different types of transactions
TRANSACTION_TYPE = (
    ("transfer", "Transfer"),
    ("recieved", "Recieved"),
    ("withdraw", "withdraw"),
    ("refund", "Refund"),
    ("request", "Payment Request"),
    ("none", "None")
)

TRANSACTION_STATUS = (
    ("failed", "failed"),
    ("completed", "completed"),
    ("pending", "pending"),
    ("processing", "processing"),
    ("request_sent", "request_sent"),
    ("request_settled", "request settled"),
    ("request_processing", "request processing"),

)

# Create a Django model called Transaction


class Transaction(models.Model):
    # Field for a unique transaction ID using ShortUUIDField
    transaction_id = ShortUUIDField(
        unique=True, length=15, max_length=20, prefix="TRN")

    # ForeignKey to the User model for the user associated with the transaction
    user = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, related_name="user")

    # DecimalField for the transaction amount
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)

    # CharField for the transaction description
    description = models.CharField(max_length=1000, null=True, blank=True)

    # ForeignKey to the User model for the receiver of the transaction
    reciever = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, related_name="reciever")

    # ForeignKey to the User model for the sender of the transaction
    sender = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, related_name="sender")

    # ForeignKey to the Account model for the receiver's account
    reciever_account = models.ForeignKey(
        Account, on_delete=models.SET_NULL, null=True, related_name="reciever_account")

    # ForeignKey to the Account model for the sender's account
    sender_account = models.ForeignKey(
        Account, on_delete=models.SET_NULL, null=True, related_name="sender_account")

    # CharField for the transaction status using predefined choices
    status = models.CharField(
        choices=TRANSACTION_STATUS, max_length=100, default="pending")

    # CharField for the transaction type using predefined choices
    transaction_type = models.CharField(
        choices=TRANSACTION_TYPE, max_length=100, default="none")

    # DateTimeField for the transaction date, auto-generated on creation
    date = models.DateTimeField(auto_now_add=True)

    # DateTimeField for the last update time of the transaction
    updated = models.DateTimeField(auto_now_add=False, null=True, blank=True)

    # Define a string representation of the object
    def __str__(self):
        try:
            return f"{self.user}"
        except:
            return f"Transaction"
