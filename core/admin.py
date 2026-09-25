# Import the admin module from the django.contrib package
from django.contrib import admin

# Import the Transaction model from the core.models module
from core.models import Transaction, TransferLimit

# Define a custom admin class for the Transaction model
class TransactionAdmin(admin.ModelAdmin):
    # Specify which fields are editable in the list view
    list_editable = ['amount', 'status', 'transaction_type', 'reciever', 'sender']
    
    # Specify which fields are displayed in the list view
    list_display = ['user', 'amount', 'status', 'transaction_type', 'reciever', 'sender']

# Register the Transaction model with the Django admin site
admin.site.register(Transaction, TransactionAdmin)


# Phase G-1. Plain ModelAdmin, like TransactionAdmin above: core does not use
# import-export. ``autocomplete_fields`` needs the referenced model to be
# registered with search_fields, which UserAdmin (userauths/admin.py) is.
class TransferLimitAdmin(admin.ModelAdmin):
    list_display = ["user", "period", "amount", "updated_at"]
    list_filter = ["period"]
    search_fields = ["user__username", "user__email"]
    readonly_fields = ["created_at", "updated_at"]
    autocomplete_fields = ["user"]


admin.site.register(TransferLimit, TransferLimitAdmin)
