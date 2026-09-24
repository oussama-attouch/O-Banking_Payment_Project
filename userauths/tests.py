"""Authentication flow guards.

``test_signup_provisions_an_account`` is the counterpart to the A1 fix: removing
``save_account`` must not have disturbed ``create_account``, because every money
view depends on ``request.user.account`` existing.

``TwoFactorSetupTests`` (Phase C-1) drives the enrolment and disable flows. It
does not test the login challenge: that arrives in Phase C-2.
"""
import re
from decimal import Decimal

import pyotp
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from account.models import Account
from userauths import totp
from userauths.models import TOTPDevice, TOTPRecoveryCode, User

PASSWORD = "pw-Phase-1.5-test"


class AuthFlowTests(TestCase):

    @classmethod
    def setUpClass(cls):
        # See core.tests: hardening follows DEBUG, and a secure session cookie is
        # never sent back over plain HTTP, which would log every test out.
        cls._hardening = override_settings(
            SECURE_SSL_REDIRECT=False,
            SESSION_COOKIE_SECURE=False,
            CSRF_COOKIE_SECURE=False,
        )
        cls._hardening.enable()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls._hardening.disable()

    def test_signup_creates_user_and_provisions_an_account(self):
        resp = self.client.post(reverse("userauths:sign-up"), {
            "username": "newbie", "email": "newbie@test.invalid",
            "password1": PASSWORD, "password2": PASSWORD,
        })
        self.assertEqual(resp.status_code, 302)
        user = User.objects.get(email="newbie@test.invalid")
        self.assertEqual(Account.objects.filter(user=user).count(), 1)

    def test_signin_with_email_works_and_signout_ends_the_session(self):
        User.objects.create_user(
            username="login", email="login@test.invalid", password=PASSWORD
        )
        client = Client()
        resp = client.post(
            reverse("userauths:sign-in"),
            {"email": "login@test.invalid", "password": PASSWORD},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/account/", resp["Location"])
        self.assertIn("_auth_user_id", client.session)

        client.get(reverse("userauths:sign-out"))
        self.assertNotIn("_auth_user_id", client.session)

    def test_login_does_not_reset_the_balance(self):
        """Control: documents that a normal login was never the trigger.

        Each request loads the User fresh, so its ``.account`` is never stale --
        which is why the A1 bug only fired for long-lived User instances.
        """
        user = User.objects.create_user(
            username="rich", email="rich@test.invalid", password=PASSWORD
        )
        acct = Account.objects.get(user=user)
        Account.objects.filter(pk=acct.pk).update(account_balance=Decimal("1000.00"))

        client = Client()
        client.post(
            reverse("userauths:sign-in"),
            {"email": "rich@test.invalid", "password": PASSWORD},
        )

        self.assertEqual(Account.objects.get(pk=acct.pk).account_balance, Decimal("1000.00"))


# =====================================================================
# Phase C-1  TOTP two-factor authentication: enrolment
# =====================================================================
class TwoFactorSetupTests(TestCase):
    """Enrol, verify, and disable a TOTP device through the real views.

    The codes are computed the way an authenticator app would, from the secret
    the enrolment page displays, so nothing here re-implements the algorithm
    under test.
    """

    @classmethod
    def setUpClass(cls):
        # See core.tests: hardening follows DEBUG, and a secure session cookie is
        # never sent back over plain HTTP, which would log every test out.
        cls._hardening = override_settings(
            SECURE_SSL_REDIRECT=False,
            SESSION_COOKIE_SECURE=False,
            CSRF_COOKIE_SECURE=False,
        )
        cls._hardening.enable()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls._hardening.disable()

    def setUp(self):
        self.alice = User.objects.create_user(
            username="alice", email="alice@test.invalid", password=PASSWORD
        )
        self.client.force_login(self.alice)

    # ------------------------------------------------------------- helpers
    def urls(self):
        return {
            "setup": reverse("userauths:two_factor_setup"),
            "status": reverse("userauths:two_factor_status"),
            "disable": reverse("userauths:two_factor_disable"),
            "disable_post": reverse("userauths:two_factor_disable_confirm"),
        }

    def device(self, confirmed=False):
        return TOTPDevice.objects.create(
            user=self.alice,
            secret=totp.generate_secret(),
            is_confirmed=confirmed,
            confirmed_at=timezone.now() if confirmed else None,
        )

    def current_code(self, device):
        """The code an authenticator app would show right now."""
        return pyotp.TOTP(device.secret).now()

    def confirm(self, device):
        """Walk the real POST so the recovery codes are created by the view."""
        return self.client.post(self.urls()["setup"],
                                {"code": self.current_code(device)})

    # ------------------------------------------------------------- setup
    def test_setup_requires_login(self):
        resp = Client().get(reverse("userauths:two_factor_setup"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/user/sign-in/", resp["Location"])

    def test_setup_creates_unconfirmed_device(self):
        resp = self.client.get(self.urls()["setup"])
        self.assertEqual(resp.status_code, 200)

        devices = TOTPDevice.objects.filter(user=self.alice)
        self.assertEqual(devices.count(), 1)
        self.assertFalse(devices.get().is_confirmed)

    def test_setup_renders_qr_and_secret(self):
        resp = self.client.get(self.urls()["setup"])
        body = resp.content.decode()
        device = TOTPDevice.objects.get(user=self.alice)

        self.assertIn("data:image/png;base64,", body)
        self.assertIn(device.secret, body)
        # The QR encodes this exact otpauth URI. The page shows the QR and the
        # secret but not the URI itself, so it is checked on the context.
        self.assertEqual(
            resp.context["uri"], totp.provisioning_uri(device.secret, self.alice)
        )

    def test_setup_post_valid_code_confirms(self):
        self.client.get(self.urls()["setup"])
        device = TOTPDevice.objects.get(user=self.alice)

        resp = self.confirm(device)
        self.assertEqual(resp.status_code, 200)

        device.refresh_from_db()
        self.assertTrue(device.is_confirmed)
        self.assertIsNotNone(device.confirmed_at)

    def test_setup_post_invalid_code_does_not_confirm(self):
        self.client.get(self.urls()["setup"])
        device = TOTPDevice.objects.get(user=self.alice)
        # "000000" is a valid guess roughly once in a million; if this run is
        # that run, ask for a different wrong code instead of flaking.
        wrong = "000000" if self.current_code(device) != "000000" else "111111"

        resp = self.client.post(self.urls()["setup"], {"code": wrong})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "did not match")

        device.refresh_from_db()
        self.assertFalse(device.is_confirmed)
        self.assertIsNone(device.confirmed_at)

    def test_setup_generates_ten_recovery_codes(self):
        device = self.device()
        self.confirm(device)
        self.assertEqual(TOTPRecoveryCode.objects.filter(device=device).count(), 10)

    def test_recovery_codes_are_hashed(self):
        device = self.device()
        resp = self.confirm(device)

        shown = re.findall(r"<code>([A-Z0-9]{10})</code>", resp.content.decode())
        self.assertEqual(len(shown), 10, "the page must show ten codes once")

        hashes = [c.code_hash for c in TOTPRecoveryCode.objects.filter(device=device)]
        self.assertEqual(len(hashes), 10)
        for code in shown:
            # Never stored in the clear ...
            self.assertNotIn(code, hashes)
            # ... but each displayed code still matches exactly one stored hash.
            matches = [h for h in hashes if totp.check_recovery_code(code, h)]
            self.assertEqual(len(matches), 1, "%s matched %d hashes" % (code, len(matches)))
        for code_hash in hashes:
            self.assertTrue(
                code_hash.startswith("pbkdf2_") or code_hash.startswith("md5$"),
                "not a Django password hash: %s" % code_hash,
            )

    def test_setup_when_already_confirmed_redirects(self):
        device = self.device()
        self.confirm(device)

        resp = self.client.get(self.urls()["setup"])
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], self.urls()["disable"])

    # ------------------------------------------------------------- disable
    def test_disable_confirm_requires_confirmed_device(self):
        resp = self.client.get(self.urls()["disable"])
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], reverse("account:settings"))

    def test_disable_requires_correct_password(self):
        device = self.device(confirmed=True)

        resp = self.client.post(self.urls()["disable_post"],
                                {"password": "not-the-password"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Incorrect password.")
        self.assertTrue(TOTPDevice.objects.filter(pk=device.pk).exists())

    def test_disable_removes_device_and_codes(self):
        device = self.device()
        self.confirm(device)
        self.assertEqual(TOTPRecoveryCode.objects.filter(device=device).count(), 10)

        resp = self.client.post(self.urls()["disable_post"], {"password": PASSWORD})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], reverse("account:settings"))

        self.assertEqual(TOTPDevice.objects.filter(user=self.alice).count(), 0)
        self.assertEqual(
            TOTPRecoveryCode.objects.filter(device__user=self.alice).count(), 0
        )

    def test_status_redirects_to_setup_or_disable(self):
        resp = self.client.get(self.urls()["status"])
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], self.urls()["setup"])

        device = self.device(confirmed=True)
        resp = self.client.get(self.urls()["status"])
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], self.urls()["disable"])
        self.assertTrue(TOTPDevice.objects.filter(pk=device.pk).exists())
