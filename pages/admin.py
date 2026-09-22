from django.contrib import admin
from import_export.admin import ImportExportModelAdmin

from pages.models import BlogPost, ContactMessage


# Registered as ImportExportModelAdmin to match the precedent set by
# account/admin.py's AccountAdminModel and KYCAdmin.
class BlogPostAdmin(ImportExportModelAdmin):
    list_display = ["title", "category", "author_name", "is_featured", "is_published", "published_at"]
    list_filter = ["category", "is_featured", "is_published"]
    search_fields = ["title", "excerpt", "body", "tags"]
    prepopulated_fields = {"slug": ("title",)}
    date_hierarchy = "published_at"


class ContactMessageAdmin(ImportExportModelAdmin):
    list_display = ["name", "subject", "email", "created_at", "is_read"]
    list_filter = ["is_read"]
    search_fields = ["name", "email", "subject", "message"]


admin.site.register(BlogPost, BlogPostAdmin)
admin.site.register(ContactMessage, ContactMessageAdmin)
