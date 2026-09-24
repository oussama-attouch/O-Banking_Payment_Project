from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from userauths.models import TOTPDevice, TOTPRecoveryCode, User

# Registered with UserAdmin rather than a bare ModelAdmin so the admin hashes
# passwords on save instead of showing the raw hash in a text input.
admin.site.register(User, UserAdmin)


class TOTPDeviceAdmin(admin.ModelAdmin):
    list_display = ["user", "is_confirmed", "created_at", "confirmed_at", "last_used_at"]
    list_filter = ["is_confirmed"]
    search_fields = ["user__username", "user__email"]
    readonly_fields = ["created_at", "confirmed_at", "last_used_at"]
    autocomplete_fields = ["user"]


admin.site.register(TOTPDevice, TOTPDeviceAdmin)


class TOTPRecoveryCodeAdmin(admin.ModelAdmin):
    list_display = ["device", "is_used", "used_at", "created_at"]
    list_filter = ["is_used"]
    search_fields = ["device__user__username", "device__user__email"]
    readonly_fields = ["created_at", "used_at"]
    autocomplete_fields = ["device"]


admin.site.register(TOTPRecoveryCode, TOTPRecoveryCodeAdmin)