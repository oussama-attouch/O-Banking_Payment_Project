"""Audit signals.

`user_login_failed` fires from Django's `authenticate()` on every failed
credential check. We log it so a brute-force attempt leaves a trail. The
receiver must not raise: a logging failure cannot prevent the login response
from being returned.
"""
from django.contrib.auth.signals import user_login_failed
from django.dispatch import receiver

from .utils import log


@receiver(user_login_failed)
def on_login_failed(sender, credentials, request=None, **kwargs):
    try:
        # `credentials` is the keyword-argument dict Django's authenticate()
        # was called with, with any password-shaped value replaced by a
        # placeholder before the signal is sent. "username" is the default
        # ModelBackend spelling, but this project calls
        # authenticate(request, email=..., password=...) -- see LoginView -- so
        # here the submitted identifier arrives under the "email" key. Both are
        # accepted, in that order, so the identifier is recorded either way.
        submitted = (
            (credentials or {}).get("username")
            or (credentials or {}).get("email")
            or ""
        )
        # Never store the password, even a wrong one.
        log(
            "user_login_failed",
            request=request,
            metadata={"submitted_identifier": str(submitted)[:150]},
        )
    except Exception:
        return
