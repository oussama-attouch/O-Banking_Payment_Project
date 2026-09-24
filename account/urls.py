from django.contrib.auth import views as auth_views
from django.urls import path, reverse_lazy

from account import views
from .forms import StyledPasswordChangeForm

app_name = "account"

urlpatterns = [
    path("dashboard/",views.dashboard,name="dashboard"),
    # Aggregates as JSON, for the dashboard's polling refresh (Phase 2a-dash-4).
    path("dashboard-data/", views.dashboard_data, name="dashboard-data"),
    path("",views.account,name="account"),
    path("kyc-reg/", views.kyc_registration, name="kyc-reg"),

    # Phase 5d. Password changing is Django's own PasswordChangeView: it checks
    # the current password, runs the configured validators, and calls
    # update_session_auth_hash so the user stays signed in afterwards. Only the
    # template and the widget styling are ours.
    path("settings/", views.settings_view, name="settings"),
    path(
        "settings/password/",
        auth_views.PasswordChangeView.as_view(
            template_name="account/password_change.html",
            form_class=StyledPasswordChangeForm,
            success_url=reverse_lazy("account:settings"),
        ),
        name="password_change",
    ),

    # Phase 5e. The export is the same filtered set as the page, as CSV.
    path("statements/", views.statements_view, name="statements"),
    path("statements/export.csv", views.export_csv, name="statements_export"),

    # Phase 5f-2. The Recipient pk is the default BigAutoField, hence <int:pk>.
    path("recipients/", views.recipients_view, name="recipients"),
    path("recipients/<int:pk>/delete/", views.recipient_delete, name="recipient_delete"),

    # Phase 5g-2. The bell dropdown and the full list. Both read and write
    # handlers are scoped to request.user.
    path("notifications/", views.notifications_view, name="notifications"),
    path("notifications/<int:pk>/read/", views.notification_mark_read, name="notification_mark_read"),
    path("notifications/read-all/", views.notification_mark_all_read, name="notification_mark_all_read"),

    # Phase 5h-2. Support tickets. The create form is inline on the list page,
    # so adding a ticket is a POST to "support", not a separate route.
    path("support/", views.support_view, name="support"),
    path("support/<int:pk>/", views.support_detail, name="support_detail"),

    # Phase E-2a. Budget categories: list + inline add, and a POST-only delete.
    # Deleting one SET_NULLs the transactions that pointed at it.
    path("categories/", views.categories_view, name="categories"),
    path("categories/<int:pk>/delete/", views.category_delete, name="category_delete"),
]
