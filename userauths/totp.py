"""TOTP helpers for the O-Banking demo.

The TOTP secret is stored as plaintext base32 in the database. That
is a deliberate simplification for a demo project; production would
encrypt the secret with a KMS-managed key. Recorded in README
Future Work.
"""
import base64
import io
import secrets

import pyotp
import qrcode


ISSUER = "O-Banking"
RECOVERY_CODE_COUNT = 10


def generate_secret():
    """Return a new base32 secret compatible with authenticator apps."""
    return pyotp.random_base32()


def provisioning_uri(secret, user, issuer=ISSUER):
    """Return the otpauth:// URI for the QR code.

    `user` may be a User instance or an email string. The label
    must be URL-safe; pyotp handles the encoding.
    """
    label = getattr(user, "email", None) or str(user)
    return pyotp.TOTP(secret).provisioning_uri(name=label, issuer_name=issuer)


def qr_data_url(uri):
    """Return a data:image/png;base64,... URL for the provisioning URI.

    Rendered directly in the template so no file is written to disk
    and no media path is needed.
    """
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def verify_code(secret, code, valid_window=1):
    """Return True if `code` is a valid 6-digit TOTP for `secret`.

    valid_window=1 accepts the current 30-second window plus one
    window on either side, which is what most authenticator apps
    need. Code must be a 6-digit numeric string; anything else
    returns False.
    """
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit() or len(code) != 6:
        return False
    return pyotp.TOTP(secret).verify(code, valid_window=valid_window)


def generate_recovery_codes():
    """Return RECOVERY_CODE_COUNT fresh codes as a list of strings.

    Each code is 10 characters from an unambiguous alphabet
    (no 0/O, no 1/I/L) to reduce transcription errors.
    """
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    codes = []
    for _ in range(RECOVERY_CODE_COUNT):
        codes.append("".join(secrets.choice(alphabet) for _ in range(10)))
    return codes


def hash_recovery_codes(codes):
    """Return a list of (raw_code, hash) pairs. Hash with Django's
    default password hasher (make_password)."""
    from django.contrib.auth.hashers import make_password
    return [(c, make_password(c)) for c in codes]


def check_recovery_code(code, code_hash):
    """Return True if the submitted code matches a stored hash."""
    from django.contrib.auth.hashers import check_password
    return check_password((code or "").strip().upper(), code_hash)
