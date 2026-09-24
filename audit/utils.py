from .middleware import get_current_request
from .models import LogEntry


def _client_ip(request):
    if request is None:
        return None
    xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    return request.META.get("REMOTE_ADDR") or None


def log(action, actor=None, target=None, metadata=None, request=None):
    """Append one LogEntry. Safe to call with no request and no actor.

    Never raises: a logging failure must not take down the request
    that triggered it. If the write fails, it is silently dropped.
    """
    try:
        if request is None:
            request = get_current_request()

        if actor is None and request is not None:
            user = getattr(request, "user", None)
            if user is not None and getattr(user, "is_authenticated", False):
                actor = user

        target_type = ""
        target_id = ""
        if target is not None:
            target_type = target.__class__.__name__[:40]
            pk = getattr(target, "pk", None)
            if pk is not None:
                target_id = str(pk)[:64]

        user_agent = ""
        if request is not None:
            user_agent = request.META.get("HTTP_USER_AGENT", "")[:500]

        LogEntry.objects.create(
            actor=actor,
            action=action,
            target_type=target_type,
            target_id=target_id,
            ip=_client_ip(request),
            user_agent=user_agent,
            metadata=metadata or {},
        )
    except Exception:
        # A logging failure must never break the request. In a
        # production system this would go to a fallback logger;
        # for the demo it is dropped.
        return None
