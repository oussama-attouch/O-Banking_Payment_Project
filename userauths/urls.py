from django.urls import path
from userauths import views

app_name = "userauths"

urlpatterns = [
    path("sign-up/", views.RegisterView, name="sign-up"),
    path("sign-in/", views.LoginView, name="sign-in"),
    path("sign-out/", views.logoutView, name="sign-out"),
    path("2fa/setup/", views.two_factor_setup, name="two_factor_setup"),
    path("2fa/status/", views.two_factor_status, name="two_factor_status"),
    path("2fa/disable/", views.two_factor_disable_confirm, name="two_factor_disable"),
    path("2fa/disable/confirm/", views.two_factor_disable, name="two_factor_disable_confirm"),
]
