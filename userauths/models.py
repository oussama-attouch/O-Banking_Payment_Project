from django.db import models
from django.contrib.auth.models import AbstractUser

class User(AbstractUser):
    username = models.CharField(max_length=100)
    email = models.EmailField(unique=True)
    is_staff = models.BooleanField(default=False)
    is_superuser = models.BooleanField(default=False)

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['username']

    def __str__(self):
        return self.username


class TOTPDevice(models.Model):
    """A confirmed TOTP authenticator for one user. One per user.

    The secret is stored as the base32 string pyotp produces. It
    is NOT encrypted at rest in this build; production would need
    a KMS-backed encrypted field. That limitation is recorded in
    README Future Work and in the docstring of userauths/totp.py.
    """

    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="totp_device",
    )
    secret = models.CharField(max_length=64)
    is_confirmed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        state = "confirmed" if self.is_confirmed else "pending"
        return f"TOTPDevice({self.user.username}, {state})"


class TOTPRecoveryCode(models.Model):
    """One single-use recovery code. Ten are generated when a device
    is first confirmed. `code_hash` is Django's make_password output
    so the code is never stored in the clear after generation."""

    device = models.ForeignKey(
        TOTPDevice,
        on_delete=models.CASCADE,
        related_name="recovery_codes",
    )
    code_hash = models.CharField(max_length=128)
    is_used = models.BooleanField(default=False)
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        state = "used" if self.is_used else "active"
        return f"TOTPRecoveryCode({self.device.user.username}, {state})"