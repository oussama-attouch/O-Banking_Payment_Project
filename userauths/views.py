from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login, logout
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.views.decorators.http import require_POST

from userauths.models import TOTPDevice, TOTPRecoveryCode, User
from userauths.forms import UserRegisterForm
from userauths import totp
from audit.utils import log as audit_log
from core.ratelimit import rate_limit, login_key, reset_login_limit

def RegisterView(request):
    if request.method == "POST":
        form = UserRegisterForm(request.POST)
        if form.is_valid():
            # form.save()
            new_user = form.save() # new_user.email
            username = form.cleaned_data.get("username")
            # username = request.POST.get("username")
            messages.success(request, f"Hey {username}, your account was created successfully.")
            # new_user = authenticate(username=form.cleaned_data.get('email'))
            new_user = authenticate(username=form.cleaned_data['email'],
                                    password=form.cleaned_data['password1'])
            login(request, new_user)
            audit_log("user_register", target=new_user)
            return redirect("account:account")
    
    if request.user.is_authenticated:
        messages.warning(request, f"You are already logged in.")
        return redirect("account:account")


    else:
        form = UserRegisterForm()
    context = {
        "form": form
    }
    return render(request, "userauths/sign-up.html", context)


@rate_limit(login_key, limit=5, window=15 * 60, label="login")
def LoginView(request):
    if request.method == "POST":
        email = request.POST.get("email")
        password = request.POST.get("password")

        try:
            user = User.objects.get(email=email)
            user = authenticate(request, email=email, password=password)

            if user is not None: # if there is a user
                # Phase C-2: if the user has a confirmed TOTP device, do not
                # authenticate the session yet. Park the pending user in the
                # session and send them to the challenge.
                _device = TOTPDevice.objects.filter(user=user, is_confirmed=True).first()
                if _device is not None:
                    request.session["2fa_pending_user_id"] = user.pk
                    request.session["2fa_pending_at"] = timezone.now().isoformat()
                    request.session["2fa_pending_email"] = request.POST.get("email")
                    return redirect("userauths:two_factor_challenge")

                login(request, user)
                audit_log("user_login", target=user)
                reset_login_limit(request.POST.get("email"))
                messages.success(request, "You are logged.")
                return redirect("account:account")
            else:
                messages.warning(request, "Username or password does not exist")
                return redirect("userauths:sign-in")
        except:
            messages.warning(request, "User does not exist")

    if request.user.is_authenticated:
        messages.warning(request, "You are already logged In")
        return redirect("account:account")
        
    return render(request, "userauths/sign-in.html")


# =====================================================================
# Phase C-2  TOTP challenge on login
# =====================================================================
@rate_limit(lambda r, *a, **k: f"2fa_challenge:{r.session.get('2fa_pending_user_id') or 'anon'}",
            limit=10, window=15 * 60, label="2fa_challenge")
def two_factor_challenge(request):
    """Second-factor challenge during login. NOT decorated with
    @login_required: the session is not authenticated yet.

    Reads the pending user id from the session, checks the
    submitted TOTP code first, then falls back to unused recovery
    codes. On success calls login() and clears the pending state.
    On failure logs 2fa_challenge_failed and re-renders.
    """
    from datetime import datetime, timedelta

    PENDING_TIMEOUT_SECONDS = 5 * 60

    pending_id = request.session.get("2fa_pending_user_id")
    pending_at = request.session.get("2fa_pending_at")

    # No pending challenge → nothing to do, back to sign-in.
    if pending_id is None or pending_at is None:
        # A half-written pending state (an id with no timestamp) is inert --
        # every later request takes this same branch, so it can never reach
        # login() -- but it should not be left behind either.
        request.session.pop("2fa_pending_user_id", None)
        request.session.pop("2fa_pending_at", None)
        return redirect("userauths:sign-in")

    # Expired pending challenge → drop it and go back to sign-in.
    try:
        started = datetime.fromisoformat(pending_at)
    except (TypeError, ValueError):
        request.session.pop("2fa_pending_user_id", None)
        request.session.pop("2fa_pending_at", None)
        return redirect("userauths:sign-in")

    if timezone.now() - started > timedelta(seconds=PENDING_TIMEOUT_SECONDS):
        request.session.pop("2fa_pending_user_id", None)
        request.session.pop("2fa_pending_at", None)
        messages.warning(request, "The two-factor session expired. Sign in again.")
        return redirect("userauths:sign-in")

    user = User.objects.filter(pk=pending_id).first()
    if user is None:
        request.session.pop("2fa_pending_user_id", None)
        request.session.pop("2fa_pending_at", None)
        return redirect("userauths:sign-in")

    device = TOTPDevice.objects.filter(user=user, is_confirmed=True).first()
    if device is None:
        # The device was removed after the pending state was set.
        # Proceed without 2FA (it is no longer enabled) but do not
        # leave the pending state behind.
        request.session.pop("2fa_pending_user_id", None)
        request.session.pop("2fa_pending_at", None)
        login(request, user)
        audit_log("user_login", target=user, metadata={"2fa": "device_removed_mid_flow"})
        return redirect("account:dashboard")

    if request.method == "POST":
        submitted = (request.POST.get("code") or "").strip().upper()

        # 1) TOTP
        if totp.verify_code(device.secret, submitted):
            device.last_used_at = timezone.now()
            device.save(update_fields=["last_used_at"])
            request.session.pop("2fa_pending_user_id", None)
            request.session.pop("2fa_pending_at", None)
            login(request, user)
            audit_log("2fa_challenge_passed", target=user, metadata={"method": "totp"})
            audit_log("user_login", target=user, metadata={"2fa": "totp"})
            reset_login_limit(request.session.get("2fa_pending_email") or user.email)
            return redirect("account:dashboard")

        # 2) Recovery code (any unused one)
        for rc in device.recovery_codes.filter(is_used=False):
            if totp.check_recovery_code(submitted, rc.code_hash):
                rc.is_used = True
                rc.used_at = timezone.now()
                rc.save(update_fields=["is_used", "used_at"])
                request.session.pop("2fa_pending_user_id", None)
                request.session.pop("2fa_pending_at", None)
                login(request, user)
                audit_log("2fa_challenge_passed", target=user, metadata={"method": "recovery"})
                audit_log("user_login", target=user, metadata={"2fa": "recovery"})
                reset_login_limit(request.session.get("2fa_pending_email") or user.email)
                return redirect("account:dashboard")

        audit_log("2fa_challenge_failed", target=user,
                  metadata={"code_length": len(submitted)})
        return render(request, "userauths/2fa_challenge.html",
                      {"error": "That code did not match. Try again."})

    return render(request, "userauths/2fa_challenge.html", {})

def logoutView(request):
    audit_log("user_logout")
    logout(request)
    messages.success(request, "You have been logged out.")
    return redirect("core:index")


# =====================================================================
# Phase C-1  TOTP two-factor authentication: enrolment
# =====================================================================
@login_required
def two_factor_setup(request):
    """Show the enrolment page or handle code verification.

    GET  — if a device exists but is unconfirmed, reuse its secret;
           otherwise create a new unconfirmed device. Render the
           QR code, the base32 secret, and a 6-digit code form.
    POST — verify the submitted code. On success mark the device
           confirmed, generate 10 recovery codes, store hashes,
           and render the codes once with a message that they are
           shown a single time. On failure re-render with an error.
    """
    device = TOTPDevice.objects.filter(user=request.user).first()
    if device and device.is_confirmed:
        return redirect("userauths:two_factor_disable")

    if device is None:
        device = TOTPDevice.objects.create(
            user=request.user,
            secret=totp.generate_secret(),
            is_confirmed=False,
        )

    uri = totp.provisioning_uri(device.secret, request.user)
    qr = totp.qr_data_url(uri)

    if request.method == "POST":
        submitted = request.POST.get("code", "")
        if totp.verify_code(device.secret, submitted):
            device.is_confirmed = True
            device.confirmed_at = timezone.now()
            device.save(update_fields=["is_confirmed", "confirmed_at"])

            raw_codes = totp.generate_recovery_codes()
            pairs = totp.hash_recovery_codes(raw_codes)
            TOTPRecoveryCode.objects.bulk_create([
                TOTPRecoveryCode(device=device, code_hash=h)
                for _, h in pairs
            ])

            audit_log("2fa_enabled", target=request.user,
                      metadata={"2fa": "enabled"})

            return render(request, "userauths/2fa_setup.html", {
                "device": device,
                "qr": qr,
                "uri": uri,
                "recovery_codes": raw_codes,
                "confirmed": True,
            })

        return render(request, "userauths/2fa_setup.html", {
            "device": device,
            "qr": qr,
            "uri": uri,
            "error": "That code did not match. Try again.",
        })

    return render(request, "userauths/2fa_setup.html", {
        "device": device,
        "qr": qr,
        "uri": uri,
    })


@login_required
@require_POST
def two_factor_disable(request):
    """Delete the device and all recovery codes. POST only.

    Requires the account password to be re-entered. That is the
    security check: disabling 2FA is a downgrade and must not be
    one-click. Wrong password re-renders with an error.
    """
    device = TOTPDevice.objects.filter(user=request.user, is_confirmed=True).first()
    if device is None:
        return redirect("account:settings")

    password = request.POST.get("password", "")
    if not request.user.check_password(password):
        return render(request, "userauths/2fa_disable.html", {
            "device": device,
            "error": "Incorrect password.",
        })

    device.delete()
    audit_log("2fa_disabled", target=request.user,
              metadata={"2fa": "disabled"})
    return redirect("account:settings")


@login_required
def two_factor_disable_confirm(request):
    """The confirmation page. Renders a password form.

    If the user has no confirmed device, redirect to settings.
    """
    device = TOTPDevice.objects.filter(user=request.user, is_confirmed=True).first()
    if device is None:
        return redirect("account:settings")
    return render(request, "userauths/2fa_disable.html", {"device": device})


@login_required
def two_factor_status(request):
    """Redirect to setup or disable depending on device state.

    Linked from the Settings page when the user has not enabled
    2FA yet: goes to setup. If they have enabled it: goes to the
    disable confirmation.
    """
    device = TOTPDevice.objects.filter(user=request.user).first()
    if device and device.is_confirmed:
        return redirect("userauths:two_factor_disable")
    return redirect("userauths:two_factor_setup")