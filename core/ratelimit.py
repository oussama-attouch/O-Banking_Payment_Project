"""Small fixed-window rate limiter built on Django's cache.

Backend is whatever settings.CACHES resolves to. The project does
not declare CACHES, so Django uses LocMemCache: per-process, not
shared across gunicorn workers. That is correct for the demo's
single-worker runserver and is listed in Future Work alongside
a Redis backend for production.

Window semantics: the timeout is fixed at the first request in the
window. cache.add() sets it once; cache.incr() does not reset it,
so a burst of attempts does not extend the lockout indefinitely.
"""
from functools import wraps

from django.core.cache import cache
from django.http import HttpResponse
from django.shortcuts import render
from django.template import TemplateDoesNotExist

from audit.utils import log as audit_log


def _render_429(request, message):
    try:
        return render(request, "429.html",
                      {"message": message}, status=429)
    except TemplateDoesNotExist:
        return HttpResponse(message, status=429,
                            content_type="text/plain")


def rate_limit(key_func, limit, window, methods=("POST",),
               label=None):
    """Decorator.

    key_func(request, *args, **kwargs) -> str | None. None or empty
    disables the check for that request.

    limit   -> maximum number of matching requests in the window
    window  -> seconds; the timeout is fixed at the first request
    methods -> which HTTP methods consume the quota (default POST)
    label   -> short string for the audit entry; defaults to the
               function name
    """
    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            if request.method not in methods:
                return view_func(request, *args, **kwargs)

            key = key_func(request, *args, **kwargs)
            if not key:
                return view_func(request, *args, **kwargs)

            cache_key = f"rl:{key}"

            # cache.add sets the timeout once. cache.incr only
            # increments. The window is therefore fixed from the
            # first attempt and does not slide with each attempt.
            if cache.add(cache_key, 1, timeout=window):
                count = 1
            else:
                try:
                    count = cache.incr(cache_key)
                except ValueError:
                    # Key expired between add and incr.
                    cache.set(cache_key, 1, timeout=window)
                    count = 1

            if count > limit:
                audit_log(
                    "rate_limited",
                    metadata={
                        "key": key,
                        "limit": limit,
                        "window_seconds": window,
                        "label": label or view_func.__name__,
                        "count": count,
                    },
                )
                message = (
                    "Too many attempts. Please wait before trying again."
                )
                return _render_429(request, message)

            return view_func(request, *args, **kwargs)
        return wrapper
    return decorator


# ---- Key functions ----

def login_key(request, *args, **kwargs):
    """Key by submitted email. Empty when no email posted."""
    email = (request.POST.get("email") or "").strip().lower()
    return f"login:{email}" if email else None


def user_key(prefix):
    """Return a key_func for an authenticated view."""
    def _key(request, *args, **kwargs):
        user = getattr(request, "user", None)
        if user is None or not getattr(user, "is_authenticated", False):
            return None
        return f"{prefix}:{user.pk}"
    return _key


def reset_login_limit(email):
    """Clear the login window for one email. Called after a
    successful authentication."""
    if not email:
        return
    cache.delete(f"rl:login:{email.strip().lower()}")
