from django.contrib import admin
from account.models import Account, Category, KYC, Recipient, Notification, SupportReply, SupportTicket
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

    # Phase 5f-1: RecipientAdmin.autocomplete_fields = ["target_account"] makes
    # the admin load that widget, and autocomplete refuses to serve a model whose
    # admin has no search_fields (system check admin.E040). Purely additive --
    # this only turns on the search box above the Account changelist.
    search_fields = ['user__username', 'user__email', 'account_number', 'account_id']

# Create a custom admin class for the KYC model, inheriting from ImportExportModelAdmin


class KYCAdmin(ImportExportModelAdmin):
    # Enable search functionality based on the "full_name" field
    search_fields = ["full_name"]
    # Define fields to be displayed in the list view
    list_display = ['user', 'full_name', 'gender', 'identity_type', 'date_of_birth'] 

# Saved payees (Phase 5f-1). Registering the model here keeps the admin complete;
# Phase 5f-2 adds the user-facing screens.
#
# autocomplete_fields needs search_fields on *both* referenced admins: User is
# registered with django.contrib.auth.admin.UserAdmin, which already defines
# ('username', 'first_name', 'last_name', 'email'), and AccountAdminModel above
# now defines its own.
class RecipientAdmin(ImportExportModelAdmin):
    list_display = ["user", "display_name", "target_account", "created_at"]
    list_filter = ["created_at"]
    search_fields = [
        "user__username", "user__email",
        "nickname",
        "target_account__account_number",
        "target_account__user__username",
    ]
    readonly_fields = ["created_at"]
    autocomplete_fields = ["user", "target_account"]

# Register the Account model with the custom admin class
admin.site.register(Account, AccountAdminModel)

# Register the KYC model with the custom admin class
admin.site.register(KYC, KYCAdmin)

# Register the Recipient model with the custom admin class
admin.site.register(Recipient, RecipientAdmin)


# Money-event notifications (Phase 5g-1). Rows are written by the signal in
# account/notifications.py; the admin is read-mostly on purpose.
# autocomplete_fields = ["user"] needs search_fields on the referenced admin;
# django.contrib.auth.admin.UserAdmin already defines
# ('username', 'first_name', 'last_name', 'email').
class NotificationAdmin(ImportExportModelAdmin):
    list_display = ["user", "kind", "title", "is_read", "created_at"]
    list_filter = ["kind", "is_read", "created_at"]
    search_fields = ["user__username", "user__email", "title", "body"]
    readonly_fields = ["created_at"]
    autocomplete_fields = ["user"]


# Register the Notification model with the custom admin class
admin.site.register(Notification, NotificationAdmin)


# In-app support (Phase 5h-1). Tickets are opened by users in the app and
# answered by staff here; there is no email path, so the admin is where a
# reply is written. autocomplete_fields needs search_fields on both referenced
# admins: UserAdmin defines ('username', 'first_name', 'last_name', 'email'),
# and SupportTicketAdmin defines its own for SupportReply.ticket.
class SupportReplyInline(admin.TabularInline):
    model = SupportReply
    extra = 1
    fields = ["author", "body", "created_at"]
    readonly_fields = ["created_at"]
    autocomplete_fields = ["author"]


class SupportTicketAdmin(ImportExportModelAdmin):
    list_display = ["id", "user", "subject", "status", "priority",
                    "updated_at"]
    list_filter = ["status", "priority", "created_at"]
    search_fields = [
        "user__username", "user__email",
        "subject",
        "replies__body",
    ]
    readonly_fields = ["created_at", "updated_at"]
    autocomplete_fields = ["user"]
    inlines = [SupportReplyInline]


# Register the SupportTicket model with the custom admin class
admin.site.register(SupportTicket, SupportTicketAdmin)


class SupportReplyAdmin(ImportExportModelAdmin):
    list_display = ["id", "ticket", "author", "created_at"]
    list_filter = ["created_at"]
    search_fields = ["ticket__subject", "author__username",
                     "author__email", "body"]
    readonly_fields = ["created_at"]
    autocomplete_fields = ["ticket", "author"]


# Register the SupportReply model standalone too, so a reply can be edited
# directly without going through its ticket's change form.
admin.site.register(SupportReply, SupportReplyAdmin)


# Budget categories (Phase E-1). autocomplete_fields = ["user"] needs
# search_fields on the referenced admin; UserAdmin already defines
# ('username', 'first_name', 'last_name', 'email').
class CategoryAdmin(ImportExportModelAdmin):
    list_display = ["user", "name", "slug", "icon", "color", "created_at"]
    list_filter = ["icon", "color", "created_at"]
    search_fields = ["user__username", "user__email", "name", "slug"]
    readonly_fields = ["created_at"]
    autocomplete_fields = ["user"]


admin.site.register(Category, CategoryAdmin)
