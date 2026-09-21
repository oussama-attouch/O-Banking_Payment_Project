"""Authentication flow guards.

``test_signup_provisions_an_account`` is the counterpart to the A1 fix: removing
``save_account`` must not have disturbed ``create_account``, because every money
view depends on ``request.user.account`` existing.
"""
from decimal import Decimal

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from account.models import Account
from userauths.models import User

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
