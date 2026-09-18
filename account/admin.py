from django.contrib import admin
from account.models import Account, KYC
from userauths.models import User
from import_export.admin import ImportExportModelAdmin

# Create a custom admin class for the Account model, inheriting from ImportExportModelAdmin


class AccountAdminModel(ImportExportModelAdmin):
    # Allow editing the specified fields directly in the list view
    list_editable = ['account_status', 'account_balance', 'kyc_submitted', 'kyc_confirmed'] 

    # Define fields to be displayed in the list view
    list_display = ['user', 'account_number' ,'account_status', 'account_balance', 'kyc_submitted', 'kyc_confirmed'] 


    # Add a filter sidebar for filtering by account status
    list_filter = ['account_status']

# Create a custom admin class for the KYC model, inheriting from ImportExportModelAdmin


class KYCAdmin(ImportExportModelAdmin):
    # Enable search functionality based on the "full_name" field
    search_fields = ["full_name"]
    # Define fields to be displayed in the list view
    list_display = ['user', 'full_name', 'gender', 'identity_type', 'date_of_birth'] 

# Register the Account model with the custom admin class
admin.site.register(Account, AccountAdminModel)

# Register the KYC model with the custom admin class
admin.site.register(KYC, KYCAdmin)
