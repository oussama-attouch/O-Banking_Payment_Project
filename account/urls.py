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
]
