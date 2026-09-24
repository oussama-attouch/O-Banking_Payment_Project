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