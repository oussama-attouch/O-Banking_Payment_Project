from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from userauths.models import User

# Registered with UserAdmin rather than a bare ModelAdmin so the admin hashes
# passwords on save instead of showing the raw hash in a text input.
admin.site.register(User, UserAdmin)