from django.urls import path
from account import views

app_name = "account"

urlpatterns = [
    path("dashboard/",views.dashboard,name="dashboard"),
    # Aggregates as JSON, for the dashboard's polling refresh (Phase 2a-dash-4).
    path("dashboard-data/", views.dashboard_data, name="dashboard-data"),
    path("",views.account,name="account"),
    path("kyc-reg/", views.kyc_registration, name="kyc-reg"),
]
