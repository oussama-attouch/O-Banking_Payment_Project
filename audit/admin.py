from django.contrib import admin
from import_export.admin import ImportExportModelAdmin

from .models import LogEntry


class LogEntryAdmin(ImportExportModelAdmin):
    list_display = ["created_at", "actor", "action", "target_type",
                    "target_id", "ip"]
    list_filter = ["action", "created_at", "target_type"]
    search_fields = ["actor__username", "actor__email",
                     "target_id", "ip"]
    date_hierarchy = "created_at"
    readonly_fields = [f.name for f in LogEntry._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


admin.site.register(LogEntry, LogEntryAdmin)
