# Import the admin module from the django.contrib package
from django.contrib import admin

# Import the Transaction model from the core.models module
from core.models import Transaction

# Define a custom admin class for the Transaction model
class TransactionAdmin(admin.ModelAdmin):
    # Specify which fields are editable in the list view
    list_editable = ['amount', 'status', 'transaction_type', 'reciever', 'sender']
    
    # Specify which fields are displayed in the list view
    list_display = ['user', 'amount', 'status', 'transaction_type', 'reciever', 'sender']

# Register the Transaction model with the Django admin site
admin.site.register(Transaction, TransactionAdmin)
