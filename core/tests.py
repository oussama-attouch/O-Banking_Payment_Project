"""Regression tests for money movement.

Deliberately **view-level**: every assertion drives the HTTP endpoints rather
than ``core.security``'s internals. That keeps the suite runnable against the
pre-Phase-1 tree (where ``core/security.py`` did not exist) so it can be shown
to actually catch the original bugs, and it exercises the decorators, URL
wiring and scoping rather than just the helpers.

``MAX_AMOUNT`` is ``Decimal("1000000.00")`` in ``core/security.py``. The boundary
values below hardcode it on purpose, so the tests do not import the module they
are testing.
"""
from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import get_resolver, reverse
from django.utils import timezone

from account.models import Account, Category, KYC
from audit.models import LogEntry
from core.models import Transaction, TransferLimit
from core.security import (
    _used_in_period,
    check_transfer_limit,
    ensure_transfer_limits,
)
from userauths.models import User

PASSWORD = "pw-Phase-1.5-test"


class MoneyMovementTestBase(TestCase):
    """alice (sender) and bob (recipient) are funded; carol is unrelated."""

    @classmethod
    def setUpClass(cls):
        # Deployment hardening follows DEBUG, so with DJANGO_DEBUG unset the SSL
        # redirect and secure cookies are on. A plain-HTTP test client cannot
        # send a secure cookie, so relax those three for the whole suite.
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
        self.alice, self.alice_acct = self._make_user("alice", "1000.00")
        self.bob, self.bob_acct = self._make_user("bob", "500.00")
        self.carol, self.carol_acct = self._make_user("carol", "250.00")
        self.client.force_login(self.alice)

    def _make_user(self, name, balance):
        user = User.objects.create_user(
            username=name, email="%s@test.invalid" % name, password=PASSWORD
        )
        # Provisioned by the post_save receiver in account.models.
        acct = Account.objects.get(user=user)
        Account.objects.filter(pk=acct.pk).update(account_balance=Decimal(balance))
        acct.refresh_from_db()
        return user, acct

    # ------------------------------------------------------------- helpers
    def balance(self, acct):
        return Account.objects.get(pk=acct.pk).account_balance

    def balances(self):
        return self.balance(self.alice_acct), self.balance(self.bob_acct)

    def transfer_count(self):
        return Transaction.objects.filter(transaction_type="transfer").count()

    def transfer_confirmation_url(self, txn):
        return reverse(
            "core:transfer-confirmation",
            args=[self.bob_acct.account_number, txn.transaction_id],
        )

    def transfer_process_url(self, txn):
        return reverse(
            "core:transfer-process",
            args=[self.bob_acct.account_number, txn.transaction_id],
        )

    # Phase 1.8: the settlement URL names the REQUESTER's account, because that
    # is what the UI renders -- transaction_list.html draws the Settle link for
    # the stored ``reciever`` and passes ``s.sender.account.account_number``.
    # create_request() stores alice as sender (requester) and bob as reciever
    # (the party who owes), so both settlement URLs carry alice's account number
    # and the acting client must be bob. See payer_client().
    def settlement_confirmation_url(self, txn):
        return reverse(
            "core:settlement-confirmation",
            args=[self.alice_acct.account_number, txn.transaction_id],
        )

    def settlement_process_url(self, txn):
        return reverse(
            "core:settlement-processing",
            args=[self.alice_acct.account_number, txn.transaction_id],
        )

    def create_transfer(self, amount="10.00", status="processing"):
        return Transaction.objects.create(
            user=self.alice, sender=self.alice, reciever=self.bob,
            sender_account=self.alice_acct, reciever_account=self.bob_acct,
            amount=Decimal(amount), status=status, transaction_type="transfer",
        )

    def create_request(self, amount="10.00", status="request_sent"):
        return Transaction.objects.create(
            user=self.alice, sender=self.alice, reciever=self.bob,
            sender_account=self.alice_acct, reciever_account=self.bob_acct,
            amount=Decimal(amount), status=status, transaction_type="request",
        )

    def carol_client(self):
        """A client logged in as carol, who is a party to nothing."""
        client = Client()
        client.force_login(self.carol)
        return client

    def payer_client(self):
        """A client logged in as bob, the stored ``reciever`` who pays a request.

        Settlement is paid by the party recorded as ``reciever`` and credited to
        the party recorded as ``sender`` (the requester), so bob is the actor for
        a request created by ``create_request``.
        """
        client = Client()
        client.force_login(self.bob)
        return client

    def csrf_token(self, client):
        """Render a page containing {% csrf_token %} so the cookie is issued."""
        page = client.get(reverse("core:amount-transfer", args=[self.bob_acct.account_number]))
        self.assertEqual(page.status_code, 200)
        return client.cookies["csrftoken"].value


# =====================================================================
# #1  amount validation
# =====================================================================
class AmountValidationTests(MoneyMovementTestBase):

    REJECTED = [
        ("", "Please enter an amount."),
        ("   ", "Please enter an amount."),
        ("abc", "must be a number."),
        ("1,000", "must be a number."),
        ("1.2.3", "must be a number."),
        ("$10", "must be a number."),
        ("nan", "must be a finite number."),
        ("NaN", "must be a finite number."),
        ("inf", "must be a finite number."),
        ("-inf", "must be a finite number."),
        ("0", "must be greater than zero."),
        ("0.00", "must be greater than zero."),
        ("-0", "must be greater than zero."),
        ("-0.01", "must be greater than zero."),
        ("-50", "must be greater than zero."),
        ("1.234", "more than 2 decimal places"),
        ("10.000", "more than 2 decimal places"),
        ("1000000.01", "cannot exceed"),
        ("9999999999.99", "cannot exceed"),
        ("1e100", "cannot exceed"),
    ]

    def test_bad_amounts_are_rejected_with_a_message_not_a_500(self):
        url = reverse("core:amount-transfer-process", args=[self.bob_acct.account_number])
        for raw, expected_message in self.REJECTED:
            with self.subTest(amount=raw):
                before = self.balances()
                resp = self.client.post(
                    url, {"amount-send": raw, "description": "x"}, follow=True
                )
                self.assertEqual(resp.status_code, 200)
                self.assertTrue(resp.redirect_chain, "%r did not redirect" % raw)
                self.assertIn("/amount-transfer/", resp.redirect_chain[-1][0])
                self.assertIn(expected_message, resp.content.decode())
                self.assertEqual(self.transfer_count(), 0)
                self.assertEqual(self.balances(), before)

    def test_missing_amount_field_is_rejected(self):
        url = reverse("core:amount-transfer-process", args=[self.bob_acct.account_number])
        resp = self.client.post(url, {"description": "no amount"}, follow=True)
        self.assertIn("Please enter an amount.", resp.content.decode())
        self.assertEqual(self.transfer_count(), 0)

    def test_valid_amounts_are_accepted(self):
        url = reverse("core:amount-transfer-process", args=[self.bob_acct.account_number])
        for raw, expected in [
            ("1", "1"), ("0.01", "0.01"), ("100", "100"),
            ("100.5", "100.5"), ("100.50", "100.50"),
            (" 42 ", "42"), ("+42", "42"),
        ]:
            with self.subTest(amount=raw):
                resp = self.client.post(url, {"amount-send": raw, "description": "ok"})
                self.assertEqual(resp.status_code, 302)
                self.assertIn("transfer-confirmation", resp["Location"])
                txn = Transaction.objects.filter(transaction_type="transfer").order_by("-id").first()
                self.assertIsNotNone(txn)
                self.assertEqual(txn.amount, Decimal(expected))
                self.assertEqual(txn.status, "processing")
                txn.delete()

    def test_amount_exactly_at_the_cap_is_not_rejected_as_too_large(self):
        """1000000.00 passes validation and is then refused for lack of funds."""
        url = reverse("core:amount-transfer-process", args=[self.bob_acct.account_number])
        resp = self.client.post(url, {"amount-send": "1000000.00"}, follow=True)
        body = resp.content.decode()
        self.assertNotIn("cannot exceed", body)
        self.assertIn("Insufficient Funds", body)
        self.assertEqual(self.transfer_count(), 0)

    def test_amount_request_endpoint_validates_too(self):
        url = reverse("core:amount-request-process", args=[self.bob_acct.account_number])
        for raw in ("-5", "0", "abc", "1.234", "nan", "1000000.01"):
            with self.subTest(amount=raw):
                resp = self.client.post(
                    url, {"amount-request": raw, "description": "x"}, follow=True
                )
                self.assertTrue(resp.redirect_chain, "%r did not redirect" % raw)
                self.assertIn("/amount-request/", resp.redirect_chain[-1][0])
                self.assertEqual(
                    Transaction.objects.filter(transaction_type="request").count(), 0
                )

    def test_amount_request_endpoint_accepts_a_valid_amount(self):
        url = reverse("core:amount-request-process", args=[self.bob_acct.account_number])
        resp = self.client.post(url, {"amount-request": "25.50", "description": "ok"})
        self.assertEqual(resp.status_code, 302)
        self.assertIn("amount-request-confirmation", resp["Location"])
        txn = Transaction.objects.filter(transaction_type="request").order_by("-id").first()
        self.assertEqual(txn.amount, Decimal("25.50"))


# =====================================================================
# #2  authentication
# =====================================================================
class AuthenticationTests(MoneyMovementTestBase):

    def test_unauthenticated_money_views_redirect_to_login(self):
        core_resolver = get_resolver().namespace_dict["core"][1]
        checked = 0
        for pattern in core_resolver.url_patterns:
            name = pattern.name
            if not name or name == "index":
                continue
            converters = getattr(pattern.pattern, "converters", {}) or {}
            url = reverse("core:" + name, args=["0000000000"] * len(converters))
            with self.subTest(view=name):
                resp = Client().get(url)
                self.assertEqual(resp.status_code, 302, "%s returned %s" % (name, resp.status_code))
                self.assertIn("/user/sign-in/", resp["Location"])
                self.assertIn("next=", resp["Location"])
            checked += 1
        self.assertEqual(checked, 18, "expected every core: route except index to be protected")

    def test_index_is_still_public(self):
        self.assertEqual(Client().get(reverse("core:index")).status_code, 200)


# =====================================================================
# #3  IDOR
# =====================================================================
class OwnershipTests(MoneyMovementTestBase):

    def test_other_user_cannot_read_a_transaction_detail(self):
        txn = self.create_transfer()
        carol = self.carol_client()

        resp = carol.get(reverse("core:transaction-detail", args=[txn.transaction_id]))
        self.assertEqual(resp.status_code, 404)

        # A non-existent id is indistinguishable from someone else's id.
        self.assertEqual(
            carol.get(reverse("core:transaction-detail", args=["TRNdoesnotexist"])).status_code,
            404,
        )
        # A party still sees their own.
        self.assertEqual(
            self.client.get(
                reverse("core:transaction-detail", args=[txn.transaction_id])
            ).status_code,
            200,
        )

    def test_other_user_cannot_confirm_or_execute_a_transfer(self):
        txn = self.create_transfer()
        carol = self.carol_client()
        before = self.balances()

        self.assertEqual(carol.get(self.transfer_confirmation_url(txn)).status_code, 302)

        resp = carol.post(
            self.transfer_process_url(txn), {"password": PASSWORD}
        )
        self.assertEqual(resp.status_code, 302)

        txn.refresh_from_db()
        self.assertEqual(txn.status, "processing")
        self.assertEqual(self.balances(), before)
        self.assertEqual(self.balance(self.carol_acct), Decimal("250.00"))

    def test_other_user_cannot_act_on_payment_request_endpoints(self):
        txn = self.create_request()
        carol = self.carol_client()

        endpoints = [
            "core:amount-request-confirmation",
            "core:amount-request-final-process",
            "core:amount-request-completed",
            "core:settlement-confirmation",
            "core:settlement-processing",
            "core:settlement-completed",
            "core:delete-request",
        ]
        for name in endpoints:
            with self.subTest(endpoint=name):
                url = reverse(name, args=[self.bob_acct.account_number, txn.transaction_id])
                if name == "core:delete-request":
                    resp = carol.post(url)
                else:
                    resp = carol.get(url)
                self.assertEqual(resp.status_code, 302, "%s returned %s" % (name, resp.status_code))

        txn.refresh_from_db()
        self.assertEqual(txn.status, "request_sent")
        self.assertEqual(self.balances(), (Decimal("1000.00"), Decimal("500.00")))


# =====================================================================
# happy path + confirmation-step guards
# =====================================================================
class TransferFlowTests(MoneyMovementTestBase):

    def test_valid_transfer_moves_both_balances_and_completes(self):
        resp = self.client.post(
            reverse("core:amount-transfer-process", args=[self.bob_acct.account_number]),
            {"amount-send": "100.00", "description": "rent"},
        )
        self.assertEqual(resp.status_code, 302)
        txn = Transaction.objects.filter(transaction_type="transfer").order_by("-id").first()
        self.assertEqual(txn.amount, Decimal("100.00"))
        self.assertEqual(txn.status, "processing")
        # Creating the transfer must not move money yet.
        self.assertEqual(self.balances(), (Decimal("1000.00"), Decimal("500.00")))

        self.assertEqual(self.client.get(self.transfer_confirmation_url(txn)).status_code, 200)

        done = self.client.post(
            self.transfer_process_url(txn), {"password": PASSWORD}
        )
        self.assertEqual(done.status_code, 302)
        self.assertIn("transfer-completed", done["Location"])
        self.assertEqual(self.client.get(done["Location"]).status_code, 200)

        txn.refresh_from_db()
        self.assertEqual(txn.status, "completed")
        self.assertEqual(self.balances(), (Decimal("900.00"), Decimal("600.00")))

    def test_wrong_password_does_not_move_money(self):
        """Phase 1b: the account PIN is gone; the password is re-entered instead."""
        txn = self.create_transfer(amount="100.00")
        before = self.balances()

        resp = self.client.post(
            self.transfer_process_url(txn), {"password": "definitely-not-the-password"}
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn("transfer-confirmation", resp["Location"])

        txn.refresh_from_db()
        self.assertEqual(txn.status, "processing")
        self.assertEqual(self.balances(), before)

    def test_empty_password_is_rejected(self):
        """An empty submission must re-prompt with a message, not 500 or execute."""
        txn = self.create_transfer(amount="100.00")
        before = self.balances()

        resp = self.client.post(
            self.transfer_process_url(txn), {"password": ""}, follow=True
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.redirect_chain)
        self.assertIn("/transfer-confirmation/", resp.redirect_chain[-1][0])
        self.assertIn("Please enter your password.", resp.content.decode())

        txn.refresh_from_db()
        self.assertEqual(txn.status, "processing")
        self.assertEqual(self.balances(), before)

    def test_transfer_is_rejected_if_the_balance_drains_before_confirmation(self):
        """Phase 1.5 (B): the creation-time balance check was never re-run."""
        resp = self.client.post(
            reverse("core:amount-transfer-process", args=[self.bob_acct.account_number]),
            {"amount-send": "900.00", "description": "big"},
        )
        self.assertEqual(resp.status_code, 302)
        txn = Transaction.objects.filter(transaction_type="transfer").order_by("-id").first()

        # Spend the money after the transfer was created but before confirming.
        Account.objects.filter(pk=self.alice_acct.pk).update(account_balance=Decimal("10.00"))

        failed = self.client.post(
            self.transfer_process_url(txn),
            {"password": PASSWORD},
            follow=True,
        )
        self.assertEqual(failed.status_code, 200)
        self.assertTrue(failed.redirect_chain)
        self.assertIn("/transfer-confirmation/", failed.redirect_chain[-1][0])
        self.assertIn("Insufficient Funds", failed.content.decode())

        txn.refresh_from_db()
        self.assertEqual(txn.status, "processing", "must stay retryable")
        self.assertEqual(self.balance(self.alice_acct), Decimal("10.00"))
        self.assertEqual(self.balance(self.bob_acct), Decimal("500.00"))

    def test_self_transfer_is_rejected(self):
        """Phase 1.5 (C): debit and credit would land on the same row."""
        resp = self.client.post(
            reverse("core:amount-transfer-process", args=[self.alice_acct.account_number]),
            {"amount-send": "10.00", "description": "to myself"},
            follow=True,
        )
        self.assertTrue(resp.redirect_chain)
        self.assertIn("/search-account/", resp.redirect_chain[-1][0])
        self.assertIn("own account", resp.content.decode())
        self.assertEqual(self.transfer_count(), 0)
        self.assertEqual(self.balance(self.alice_acct), Decimal("1000.00"))


# =====================================================================
# Phase 1.5 (D)  delete-request is a POST action
# =====================================================================
class DeleteRequestTests(MoneyMovementTestBase):

    def delete_url(self, txn):
        return reverse(
            "core:delete-request",
            args=[self.alice_acct.account_number, txn.transaction_id],
        )

    def test_get_is_rejected(self):
        txn = self.create_request()
        resp = self.client.get(self.delete_url(txn))
        self.assertEqual(resp.status_code, 405)
        self.assertTrue(Transaction.objects.filter(pk=txn.pk).exists())

    def test_post_without_csrf_is_rejected(self):
        txn = self.create_request()
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.alice)

        resp = strict.post(self.delete_url(txn))
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(Transaction.objects.filter(pk=txn.pk).exists())

    def test_post_with_csrf_deletes(self):
        txn = self.create_request()
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.alice)
        token = self.csrf_token(strict)

        resp = strict.post(self.delete_url(txn), {"csrfmiddlewaretoken": token})
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/transactions/", resp["Location"])
        self.assertFalse(Transaction.objects.filter(pk=txn.pk).exists())

    def test_route_no_longer_ends_in_html(self):
        txn = self.create_request()
        self.assertNotIn(".html", self.delete_url(txn))


# =====================================================================
# Phase 1.6 (F1)  replay of an already-completed money movement
# =====================================================================
class ReplayGuardTests(MoneyMovementTestBase):
    """A second POST of the confirmation URL must not move money again.

    Neither ``TransferProcess`` nor ``Settlement_processing`` used to look at
    ``transaction.status``. The status was only ever *written*, never checked, so
    re-submitting the same confirmation form re-applied the debit and the credit.
    That is reachable by accident (browser Back after a successful transfer, then
    Submit again) as well as deliberately.
    """

    def give_kyc(self, acct, full_name):
        """Seed a KYC row.

        Not optional scaffolding: ``Settlement_processing``'s success branch reads
        ``account.user.kyc.full_name``, so settling to a user without KYC raises
        ``RelatedObjectDoesNotExist`` inside the view. Reaching the authenticated
        UI at all requires KYC (``account.views.account`` redirects to kyc-reg), so
        a party with KYC is the realistic precondition. See finding F3.
        """
        KYC.objects.update_or_create(
            user=acct.user,
            defaults=dict(
                account=acct,
                full_name=full_name,
                nationality="MA",
                marrital_status="single",
                gender="male",
                identity_type="passport",
                date_of_birth=timezone.now(),
                signature="kyc/test.png",
                country="MA",
                city="Casablanca",
                state="Casablanca",
                mobile="0600000000",
                fax="",
            ),
        )

    def test_transfer_process_rejects_already_completed_transaction(self):
        txn = self.create_transfer(amount="100.00")

        first = self.client.post(self.transfer_process_url(txn), {"password": PASSWORD})
        self.assertEqual(first.status_code, 302)
        self.assertIn("transfer-completed", first["Location"])

        txn.refresh_from_db()
        self.assertEqual(txn.status, "completed")
        completed_balances = self.balances()
        self.assertEqual(completed_balances, (Decimal("900.00"), Decimal("600.00")))

        # Replay the exact same POST, with the correct password.
        replay = self.client.post(self.transfer_process_url(txn), {"password": PASSWORD})
        self.assertNotEqual(replay.status_code, 500, "a replayed POST must not 500")
        self.assertEqual(replay.status_code, 302)

        txn.refresh_from_db()
        self.assertEqual(txn.status, "completed", "replay changed the status")
        self.assertEqual(
            self.balances(), completed_balances, "replay moved money a second time"
        )

    def test_settlement_processing_rejects_already_completed_transaction(self):
        txn = self.create_request(amount="100.00", status="request_sent")
        self.give_kyc(self.alice_acct, "Alice")  # alice is credited, so her KYC is read
        payer = self.payer_client()

        first = payer.post(self.settlement_process_url(txn), {"password": PASSWORD})
        self.assertEqual(first.status_code, 302)
        self.assertIn("settlement-completed", first["Location"])

        txn.refresh_from_db()
        self.assertEqual(txn.status, "request_settled")
        settled_balances = self.balances()
        # bob (reciever) pays, alice (sender/requester) is credited.
        self.assertEqual(settled_balances, (Decimal("1100.00"), Decimal("400.00")))

        replay = payer.post(self.settlement_process_url(txn), {"password": PASSWORD})
        self.assertNotEqual(replay.status_code, 500, "a replayed settlement must not 500")
        self.assertEqual(replay.status_code, 302)

        txn.refresh_from_db()
        self.assertEqual(txn.status, "request_settled", "replay changed the status")
        self.assertEqual(
            self.balances(), settled_balances, "replay moved money a second time"
        )

    def test_settlement_confirmation_get_is_refused_once_completed(self):
        """F4: mirror of the TransferConfirmation guard from Phase 1.6."""
        txn = self.create_request(amount="100.00", status="request_sent")
        self.give_kyc(self.alice_acct, "Alice")
        payer = self.payer_client()

        self.assertEqual(
            payer.get(self.settlement_confirmation_url(txn)).status_code, 200
        )

        payer.post(self.settlement_process_url(txn), {"password": PASSWORD})
        txn.refresh_from_db()
        self.assertEqual(txn.status, "request_settled")

        resp = payer.get(self.settlement_confirmation_url(txn), follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.redirect_chain, "a settled request must not re-confirm")
        self.assertIn("already been processed", resp.content.decode())
        self.assertNotIn('name="password"', resp.content.decode())

    def test_transfer_confirmation_get_is_refused_once_completed(self):
        """Browser Back after a successful transfer used to show a live form."""
        txn = self.create_transfer(amount="100.00")
        self.assertEqual(
            self.client.get(self.transfer_confirmation_url(txn)).status_code, 200
        )

        self.client.post(self.transfer_process_url(txn), {"password": PASSWORD})
        txn.refresh_from_db()
        self.assertEqual(txn.status, "completed")

        resp = self.client.get(self.transfer_confirmation_url(txn), follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.redirect_chain, "the completed confirmation must redirect")
        self.assertIn("already been processed", resp.content.decode())
        # And the password form must be gone from the response.
        self.assertNotIn('name="password"', resp.content.decode())


# =====================================================================
# Phase 1.8  URL/transaction mismatch must not redirect the credit
# =====================================================================
class TransferDirectionGuardTests(MoneyMovementTestBase):
    """The account_number in the confirmation URL must be the transaction's.

    Both ``TransferProcess`` and ``Settlement_processing`` took the credited
    account straight from the URL, so a party could POST the confirmation link
    with any account number and send the money somewhere the transaction row
    does not record. A URL naming an account that does not exist at all is the
    same family of bug, and used to be a 500 rather than a rejection. These
    tests pin both the rejection and the happy path, so the guard cannot be
    tightened into over-strictness unnoticed.
    """

    MISMATCH_MESSAGE = "This transaction does not match the account in the link."

    def test_transfer_rejects_url_account_mismatch(self):
        """alice's own transfer, with carol's account number in the URL."""
        txn = self.create_transfer(amount="100.00")
        url = reverse(
            "core:transfer-process",
            args=[self.carol_acct.account_number, txn.transaction_id],
        )

        resp = self.client.post(url, {"password": PASSWORD}, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(self.MISMATCH_MESSAGE, resp.content.decode())

        txn.refresh_from_db()
        self.assertEqual(txn.status, "processing", "a mismatch must not complete the transfer")
        self.assertEqual(
            (
                self.balance(self.alice_acct),
                self.balance(self.bob_acct),
                self.balance(self.carol_acct),
            ),
            (Decimal("1000.00"), Decimal("500.00"), Decimal("250.00")),
            "a mismatch must not move money anywhere",
        )

    def test_settlement_rejects_url_account_mismatch(self):
        """The payer settles, but the URL names a third party's account."""
        txn = self.create_request(amount="100.00", status="request_sent")
        payer = self.payer_client()
        url = reverse(
            "core:settlement-processing",
            args=[self.carol_acct.account_number, txn.transaction_id],
        )

        resp = payer.post(url, {"password": PASSWORD}, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(self.MISMATCH_MESSAGE, resp.content.decode())

        txn.refresh_from_db()
        self.assertEqual(txn.status, "request_sent", "a mismatch must not settle the request")
        self.assertEqual(
            (
                self.balance(self.alice_acct),
                self.balance(self.bob_acct),
                self.balance(self.carol_acct),
            ),
            (Decimal("1000.00"), Decimal("500.00"), Decimal("250.00")),
            "a mismatch must not move money anywhere",
        )

    def test_transfer_still_completes_when_accounts_match(self):
        """The happy path: alice confirms her own transfer to bob's account."""
        txn = self.create_transfer(amount="100.00")

        resp = self.client.post(self.transfer_process_url(txn), {"password": PASSWORD})
        self.assertEqual(resp.status_code, 302)
        self.assertIn("transfer-completed", resp["Location"])

        txn.refresh_from_db()
        self.assertEqual(txn.status, "completed")
        self.assertEqual(
            self.balances(), (Decimal("900.00"), Decimal("600.00"))
        )
        self.assertEqual(self.balance(self.carol_acct), Decimal("250.00"))

    def test_settlement_still_completes_when_accounts_match(self):
        """The happy path: bob pays, alice (the requester) is credited."""
        txn = self.create_request(amount="100.00", status="request_sent")
        payer = self.payer_client()

        resp = payer.post(self.settlement_process_url(txn), {"password": PASSWORD})
        self.assertEqual(resp.status_code, 302)
        self.assertIn("settlement-completed", resp["Location"])

        txn.refresh_from_db()
        self.assertEqual(txn.status, "request_settled")
        self.assertEqual(
            self.balances(), (Decimal("1100.00"), Decimal("400.00"))
        )
        self.assertEqual(self.balance(self.carol_acct), Decimal("250.00"))

    # An account_number carrying a digit outside the generator's alphabet
    # ("1234567890") can never be issued, so it is guaranteed not to exist.
    MISSING_ACCOUNT = "2170000000000"

    def test_settlement_with_unknown_account_number_does_not_500(self):
        """Phase 1.9: .get() raised Account.DoesNotExist -> 500 before the guards.

        The URL was resolved before the transaction checks, so an unknown
        account_number crashed the view instead of rejecting it.
        """
        txn = self.create_request(amount="100.00", status="request_sent")
        payer = self.payer_client()
        self.assertFalse(
            Account.objects.filter(account_number=self.MISSING_ACCOUNT).exists(),
            "precondition: the account number must not exist",
        )
        url = reverse(
            "core:settlement-processing", args=[self.MISSING_ACCOUNT, txn.transaction_id]
        )

        resp = payer.post(url, {"password": PASSWORD})
        self.assertNotEqual(resp.status_code, 500, "an unknown account must not 500")
        self.assertEqual(resp.status_code, 302)

        txn.refresh_from_db()
        self.assertEqual(txn.status, "request_sent", "the request must be untouched")
        self.assertEqual(
            (
                self.balance(self.alice_acct),
                self.balance(self.bob_acct),
                self.balance(self.carol_acct),
            ),
            (Decimal("1000.00"), Decimal("500.00"), Decimal("250.00")),
            "no money may move for an unknown account",
        )

    def test_transfer_with_unknown_account_number_does_not_500(self):
        """The transfer side already used filter().first(); this pins it."""
        txn = self.create_transfer(amount="100.00")
        url = reverse(
            "core:transfer-process", args=[self.MISSING_ACCOUNT, txn.transaction_id]
        )

        resp = self.client.post(url, {"password": PASSWORD})
        self.assertNotEqual(resp.status_code, 500, "an unknown account must not 500")
        self.assertEqual(resp.status_code, 302)

        txn.refresh_from_db()
        self.assertEqual(txn.status, "processing", "the transfer must be untouched")
        self.assertEqual(
            (
                self.balance(self.alice_acct),
                self.balance(self.bob_acct),
                self.balance(self.carol_acct),
            ),
            (Decimal("1000.00"), Decimal("500.00"), Decimal("250.00")),
            "no money may move for an unknown account",
        )


# =====================================================================
# Phase 1.10  the remaining .get(account_number=...) sites
# =====================================================================
class UnknownAccountSweepTests(MoneyMovementTestBase):
    """Every payment_request view used to 500 on an unknown account number.

    Looking an account up with ``.get()`` raises ``Account.DoesNotExist``
    for an account that was never issued, so these URLs crashed instead of
    rejecting. Phase 1.9 fixed ``Settlement_processing``; this covers the other
    eight. The number below carries ``0``, which is outside the generator's
    alphabet ("1234567890"), so it can never be issued.
    """

    MISSING = "2170000000000"

    def setUp(self):
        super().setUp()
        self.assertFalse(
            Account.objects.filter(account_number=self.MISSING).exists(),
            "precondition: the account number must not exist",
        )

    def _assert_rejected(self, resp, label):
        self.assertNotEqual(resp.status_code, 500, "%s returned 500" % label)
        self.assertEqual(resp.status_code, 302, "%s returned %s" % (label, resp.status_code))

    def test_amount_transfer_process_unknown_account_does_not_500(self):
        """The transfer-side twin: core/transfer.py::process_amount_transfer.

        Added with the same authorisation as the payment_request sweep, because
        this view's unguarded .get() was the last reachable 500 of the family.
        """
        before = self.transfer_count()
        resp = self.client.post(
            reverse("core:amount-transfer-process", args=[self.MISSING]),
            {"amount-send": "10.00", "description": "x"},
        )
        self._assert_rejected(resp, "amount-transfer-process")
        self.assertEqual(
            self.transfer_count(), before,
            "no transfer may be created for an unknown account",
        )

    def test_amount_request_unknown_account_does_not_500(self):
        resp = self.client.get(reverse("core:amount-request", args=[self.MISSING]))
        self._assert_rejected(resp, "amount-request")
        self.assertEqual(resp["Location"], reverse("core:transactions"))

    def test_amount_request_process_unknown_account_does_not_500(self):
        before = self.transfer_count()
        resp = self.client.post(
            reverse("core:amount-request-process", args=[self.MISSING]),
            {"amount-request": "10.00", "description": "x"},
        )
        self._assert_rejected(resp, "amount-request-process")
        self.assertEqual(
            Transaction.objects.filter(transaction_type="request").count(), 0,
            "no request may be created for an unknown account",
        )
        self.assertEqual(self.transfer_count(), before)

    def test_amount_request_confirmation_unknown_account_does_not_500(self):
        txn = self.create_request()
        resp = self.client.get(
            reverse("core:amount-request-confirmation", args=[self.MISSING, txn.transaction_id])
        )
        self._assert_rejected(resp, "amount-request-confirmation")

    def test_amount_request_final_process_unknown_account_does_not_500(self):
        txn = self.create_request(status="request_processing")
        resp = self.client.post(
            reverse("core:amount-request-final-process", args=[self.MISSING, txn.transaction_id]),
            {"password": PASSWORD},
        )
        self._assert_rejected(resp, "amount-request-final-process")
        txn.refresh_from_db()
        self.assertEqual(txn.status, "request_processing", "the request must not be sent")

    def test_request_completed_unknown_account_does_not_500(self):
        txn = self.create_request()
        resp = self.client.get(
            reverse("core:amount-request-completed", args=[self.MISSING, txn.transaction_id])
        )
        self._assert_rejected(resp, "amount-request-completed")

    def test_settlement_confirmation_unknown_account_does_not_500(self):
        txn = self.create_request(status="request_sent")
        resp = self.client.get(
            reverse("core:settlement-confirmation", args=[self.MISSING, txn.transaction_id])
        )
        self._assert_rejected(resp, "settlement-confirmation")

    def test_settlement_completed_unknown_account_does_not_500(self):
        txn = self.create_request(status="request_settled")
        resp = self.client.get(
            reverse("core:settlement-completed", args=[self.MISSING, txn.transaction_id])
        )
        self._assert_rejected(resp, "settlement-completed")

    def test_delete_request_unknown_account_does_not_500(self):
        """The view is @require_POST, so only a POST can reach the guard.

        The GET half of this test pins that the sweep did not weaken
        ``@require_POST``: a GET must still be refused with 405 before the body
        runs, so it never reaches the new account guard at all.
        """
        txn = self.create_request()

        get = self.client.get(
            reverse("core:delete-request", args=[self.alice_acct.account_number, txn.transaction_id])
        )
        self.assertEqual(get.status_code, 405, "@require_POST must still refuse GET")

        resp = self.client.post(
            reverse("core:delete-request", args=[self.MISSING, txn.transaction_id])
        )
        self.assertNotEqual(resp.status_code, 500, "delete-request returned 500")
        self.assertIn(resp.status_code, (302, 405), "unexpected %s" % resp.status_code)
        self.assertTrue(
            Transaction.objects.filter(pk=txn.pk).exists(),
            "the request must not be deleted for an unknown account",
        )


# =====================================================================
# Phase 1.7 (F3)  settlement must not 500 on a payee with no KYC
# =====================================================================
class SettlementMessageTests(MoneyMovementTestBase):

    def test_settlement_succeeds_when_payee_has_no_kyc(self):
        """F3: the success message read account.user.kyc.full_name directly.

        Template lookups silence RelatedObjectDoesNotExist; direct Python
        attribute access does not, so a payee without a KYC row turned a
        completed settlement into a 500 *after* the money had already moved.
        The payee -- the party credited -- is the requester, alice.
        """
        txn = self.create_request(amount="100.00", status="request_sent")
        self.assertFalse(hasattr(self.alice, "kyc"), "precondition: alice has no KYC row")
        payer = self.payer_client()

        resp = payer.post(self.settlement_process_url(txn), {"password": PASSWORD})
        self.assertNotEqual(resp.status_code, 500, "settlement 500'd on a KYC-less payee")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("settlement-completed", resp["Location"])

        txn.refresh_from_db()
        self.assertEqual(txn.status, "request_settled")
        self.assertEqual(self.balances(), (Decimal("1100.00"), Decimal("400.00")))

        # And the completion page still renders with the username fallback.
        done = payer.get(resp["Location"])
        self.assertEqual(done.status_code, 200)


# =====================================================================
# Phase B  rate limiting on the four credential-checking endpoints
# =====================================================================
class RateLimitTests(MoneyMovementTestBase):
    """Brute force is refused with 429 and recorded, and only where it should be.

    ``cache`` is the process-wide LocMemCache, and Django does *not* clear it
    between tests, so counters would otherwise leak from one test -- or one test
    class -- into the next. Two consequences, both load-bearing:

    * ``setUp`` clears it, so each test starts from a zeroed window.
    * ``tearDown`` clears it again. Without that, a test that exhausts
      ``transfer:1`` (alice's pk) leaves an 11-attempt counter behind, and the
      *later* money tests -- TransferFlowTests and friends, which POST to the
      same view as a user with pk 1 -- would answer 429 instead of moving money.
    """

    def setUp(self):
        cache.clear()
        super().setUp()

    def tearDown(self):
        cache.clear()
        super().tearDown()

    # ------------------------------------------------------------- helpers
    def login(self, client, email, password):
        return client.post(reverse("userauths:sign-in"),
                           {"email": email, "password": password})

    def bob_client(self):
        client = Client()
        client.force_login(self.bob)
        return client

    # ------------------------------------------------------------- login
    def test_login_allows_five_attempts(self):
        for attempt in range(1, 6):
            with self.subTest(attempt=attempt):
                resp = self.login(Client(), self.alice.email, "wrong-password")
                self.assertIn(resp.status_code, (200, 302),
                              "attempt %d returned %s" % (attempt, resp.status_code))
                self.assertNotEqual(resp.status_code, 429)

    def test_login_sixth_attempt_is_429(self):
        for _ in range(5):
            self.login(Client(), self.alice.email, "wrong-password")
        resp = self.login(Client(), self.alice.email, "wrong-password")
        self.assertEqual(resp.status_code, 429)
        self.assertContains(resp, "Too many requests", status_code=429)

    def test_login_limit_is_per_email(self):
        for _ in range(5):
            self.login(Client(), self.alice.email, "wrong-password")

        resp = self.login(Client(), self.bob.email, "wrong-password")
        self.assertNotEqual(resp.status_code, 429,
                            "bob's first attempt was refused by alice's counter")

    def test_login_success_resets_counter(self):
        for _ in range(3):
            self.login(Client(), self.alice.email, "wrong-password")

        ok = self.login(Client(), self.alice.email, PASSWORD)
        self.assertEqual(ok.status_code, 302)
        self.assertIn("/account/", ok["Location"])

        for attempt in range(1, 5):
            with self.subTest(after_reset=attempt):
                resp = self.login(Client(), self.alice.email, "wrong-password")
                self.assertNotEqual(resp.status_code, 429,
                                    "counter survived a successful login")

    def test_login_get_does_not_consume_quota(self):
        client = Client()
        for _ in range(10):
            self.assertEqual(client.get(reverse("userauths:sign-in")).status_code, 200)

        resp = self.login(client, self.alice.email, "wrong-password")
        self.assertNotEqual(resp.status_code, 429,
                            "GET requests consumed the quota")

    # ------------------------------------------------------------- money paths
    def test_transfer_rate_limit_triggers(self):
        txn = self.create_transfer()
        url = self.transfer_process_url(txn)

        for attempt in range(1, 11):
            with self.subTest(attempt=attempt):
                resp = self.client.post(url, {"password": "wrong-password"})
                self.assertNotEqual(resp.status_code, 429,
                                    "attempt %d was refused too early" % attempt)

        resp = self.client.post(url, {"password": "wrong-password"})
        self.assertEqual(resp.status_code, 429)
        self.assertContains(resp, "Too many requests", status_code=429)

        # Refused before the view ran, so no money moved and the txn is untouched.
        txn.refresh_from_db()
        self.assertEqual(txn.status, "processing")
        self.assertEqual(self.balances(), (Decimal("1000.00"), Decimal("500.00")))

    def test_rate_limit_writes_audit_entry(self):
        for _ in range(5):
            self.login(Client(), self.alice.email, "wrong-password")
        resp = self.login(Client(), self.alice.email, "wrong-password")
        self.assertEqual(resp.status_code, 429)

        entries = LogEntry.objects.filter(action="rate_limited")
        self.assertEqual(entries.count(), 1, "expected exactly one audit entry")
        entry = entries.get()
        self.assertEqual(entry.metadata.get("key"), "login:%s" % self.alice.email)
        self.assertEqual(entry.metadata.get("limit"), 5)
        self.assertEqual(entry.metadata.get("window_seconds"), 15 * 60)
        self.assertEqual(entry.metadata.get("label"), "login")
        self.assertEqual(entry.metadata.get("count"), 6)
        # The login page is reached anonymously, so there is no actor.
        self.assertIsNone(entry.actor)

    def test_rate_limit_key_depends_on_user_for_transfer(self):
        txn = self.create_transfer()
        url = self.transfer_process_url(txn)

        for _ in range(10):
            self.client.post(url, {"password": "wrong-password"})
        self.assertEqual(
            self.client.post(url, {"password": "wrong-password"}).status_code, 429
        )

        resp = self.bob_client().post(url, {"password": "wrong-password"})
        self.assertNotEqual(resp.status_code, 429,
                            "bob inherited alice's transfer counter")

        # The money path names its actor: the refusal is attributable.
        entry = LogEntry.objects.filter(action="rate_limited").get()
        self.assertEqual(entry.metadata.get("key"), "transfer:%s" % self.alice.pk)
        self.assertEqual(entry.metadata.get("label"), "transfer")
        self.assertEqual(entry.metadata.get("window_seconds"), 3600)
        self.assertEqual(entry.actor, self.alice)


# =====================================================================
# Phase E-1  Transaction.category
# =====================================================================
class TransactionCategoryTests(MoneyMovementTestBase):
    """The new FK is optional, settable, and SET_NULL on category delete.

    Category is *not* part of the money path: the E-1 rule was one nullable
    column, so nothing here changes how a transfer is written.
    """

    def category(self, name="Groceries"):
        # The post_save receiver already gave alice the six defaults.
        return Category.objects.get(user=self.alice, slug=name.lower())

    def test_transaction_category_is_optional(self):
        txn = self.create_transfer()
        self.assertIsNone(txn.category)
        self.assertIsNone(txn.category_id)
        txn.refresh_from_db()
        self.assertIsNone(txn.category_id)

    def test_transaction_category_can_be_set(self):
        category = self.category()
        txn = self.create_transfer()
        txn.category = category
        txn.save()

        txn.refresh_from_db()
        self.assertEqual(txn.category_id, category.pk)
        self.assertEqual(txn.category, category)
        # Same row from the other direction.
        self.assertIn(txn, category.transactions.all())

    def test_category_delete_sets_transaction_category_to_null(self):
        category = self.category("Housing")
        txn = self.create_transfer()
        txn.category = category
        txn.save()

        category.delete()
        txn.refresh_from_db()
        self.assertIsNone(txn.category_id)
        self.assertTrue(Transaction.objects.filter(pk=txn.pk).exists())

    def test_transaction_category_nullable_with_existing_rows(self):
        """Rows written before categories existed read back as NULL.

        The schema migration added the column with no default and the data
        migration only creates categories -- it never touches transactions -- so
        a row created without one stays uncategorised.
        """
        txn = self.create_transfer()
        stored = Transaction.objects.filter(pk=txn.pk).values_list(
            "category_id", flat=True
        ).get()
        self.assertIsNone(stored)
        self.assertIsNone(
            Transaction.objects.filter(pk=txn.pk).values("category").get()["category"]
        )


# =====================================================================
# Phase G-1  per-user transfer limits
# =====================================================================
class TransferLimitTests(MoneyMovementTestBase):
    """``core.security``'s limit helpers.

    Helper-level rather than view-level, unlike the rest of this module: the
    window arithmetic is the thing under test, and ``Transaction.date`` is
    ``auto_now_add``, so a transaction that belongs 40 days in the past can only
    be built with an UPDATE. Nothing here posts to a money view -- Phase G-1
    wires the checker into no call site, and G-2 is what will.

    Every limit-window test lowers the *daily* limit below the fixture's
    transaction amounts on purpose. Left at the 10,000 default, "95.00 of
    failed volume plus 100.00 more fits" would pass whether or not the failure
    rule works at all.
    """

    def set_limit(self, period, amount, user=None):
        limit, _created = TransferLimit.objects.update_or_create(
            user=user or self.alice, period=period,
            defaults={"amount": Decimal(amount)},
        )
        return limit

    def outgoing(self, amount, status="completed", when=None):
        """A transfer alice sent: money out of her account."""
        txn = self.create_transfer(amount=amount, status=status)
        if when is not None:
            # date is auto_now_add, so it can only be moved with an UPDATE.
            Transaction.objects.filter(pk=txn.pk).update(date=when)
            txn.refresh_from_db()
        return txn

    def incoming_transfer(self, amount, status="completed"):
        """A transfer alice received: money into her account, not out of it."""
        return Transaction.objects.create(
            user=self.bob, sender=self.bob, reciever=self.alice,
            sender_account=self.bob_acct, reciever_account=self.alice_acct,
            amount=Decimal(amount), status=status, transaction_type="transfer",
        )

    def incoming_request(self, amount, status="request_settled"):
        """A settled request alice must pay, per the direction rule.

        ``create_request`` stores alice as the requester and bob as the payer;
        this is the other way round, so alice is the *reciever* and the money
        leaves her account when it settles.
        """
        return Transaction.objects.create(
            user=self.bob, sender=self.bob, reciever=self.alice,
            sender_account=self.bob_acct, reciever_account=self.alice_acct,
            amount=Decimal(amount), status=status, transaction_type="request",
        )

    # ------------------------------------------------------ lazy creation
    def test_ensure_creates_three_rows_on_first_call(self):
        self.assertEqual(TransferLimit.objects.filter(user=self.alice).count(), 0)

        limits = ensure_transfer_limits(self.alice)

        self.assertEqual(sorted(limits), ["day", "month", "week"])
        self.assertEqual(TransferLimit.objects.filter(user=self.alice).count(), 3)
        for period, expected in TransferLimit.DEFAULT_AMOUNTS.items():
            with self.subTest(period=period):
                self.assertEqual(limits[period].amount, expected)
        self.assertEqual(limits["day"].amount, Decimal("10000.00"))
        self.assertEqual(limits["week"].amount, Decimal("50000.00"))
        self.assertEqual(limits["month"].amount, Decimal("200000.00"))

    def test_ensure_is_idempotent(self):
        first = ensure_transfer_limits(self.alice)
        second = ensure_transfer_limits(self.alice)

        self.assertEqual(sorted(second), ["day", "month", "week"])
        self.assertEqual(TransferLimit.objects.filter(user=self.alice).count(), 3)
        # Same rows, not a second set.
        self.assertEqual(
            {p: tl.pk for p, tl in first.items()},
            {p: tl.pk for p, tl in second.items()},
        )

    # -------------------------------------------------------- the verdict
    def test_check_passes_when_under_limit(self):
        ok, details = check_transfer_limit(self.alice, 100)
        self.assertTrue(ok)
        self.assertIsNone(details["period"])

    def test_check_fails_daily_first(self):
        self.set_limit("day", "100.00")
        self.outgoing("95.00")

        ok, details = check_transfer_limit(self.alice, Decimal("10.00"))

        self.assertFalse(ok)
        self.assertEqual(details["period"], "day")
        self.assertEqual(details["used"], Decimal("95.00"))
        self.assertEqual(details["limit"], Decimal("100.00"))
        self.assertEqual(details["would_be"], Decimal("105.00"))

    def test_check_ignores_failed_transactions(self):
        self.set_limit("day", "100.00")
        self.outgoing("95.00", status="failed")

        ok, details = check_transfer_limit(self.alice, Decimal("100.00"))

        self.assertTrue(ok, details)
        self.assertIsNone(details["period"])
        # And the failed row really is in the day window -- it is the status
        # filter, not an empty window, that lets this through.
        self.assertEqual(
            Transaction.objects.filter(
                sender=self.alice, transaction_type="transfer"
            ).count(),
            1,
        )

    def test_check_ignores_incoming(self):
        self.set_limit("day", "100.00")
        self.incoming_transfer("95.00")

        ok, details = check_transfer_limit(self.alice, Decimal("100.00"))

        self.assertTrue(ok, details)
        self.assertIsNone(details["period"])

    def test_check_respects_period_boundaries(self):
        self.set_limit("day", "10000.00")
        self.outgoing("9999.00", when=timezone.now() - timedelta(days=40))

        ok, details = check_transfer_limit(self.alice, Decimal("500.00"))

        self.assertTrue(ok, details)
        # The 9,999.00 would have broken the day limit if it were in the
        # window, so the zero is what proves the boundary is applied.
        self.assertEqual(_used_in_period(self.alice, "day"), Decimal("0.00"))
        self.assertEqual(_used_in_period(self.alice, "month"), Decimal("0.00"))

    def test_check_uses_direction_rule_for_requests(self):
        self.set_limit("day", "100.00")
        self.incoming_request("100.00")

        ok, details = check_transfer_limit(self.alice, Decimal("1.00"))

        self.assertFalse(ok)
        self.assertEqual(details["period"], "day")
        self.assertEqual(details["used"], Decimal("100.00"))
        self.assertEqual(details["would_be"], Decimal("101.00"))

    # ------------------------------------------------------ bad arguments
    def test_check_rejects_nonpositive_amount(self):
        for amount in (0, -5):
            with self.subTest(amount=amount):
                ok, details = check_transfer_limit(self.alice, amount)
                self.assertFalse(ok)
                self.assertIsNone(details["period"])
                self.assertIn("reason", details)

    def test_check_rejects_no_user(self):
        ok, details = check_transfer_limit(None, 10)
        self.assertFalse(ok)
        self.assertIsNone(details["period"])
        self.assertIn("reason", details)


# =====================================================================
# Phase G-2  the limit enforced inside the money path
# =====================================================================
class TransferLimitEnforcementTests(MoneyMovementTestBase):
    """TransferProcess and Settlement_processing refuse an over-limit movement.

    View-level, like the rest of this module: every assertion drives the real
    confirmation endpoint, so it is the wiring and the guard ORDER under test,
    not the helper (TransferLimitTests covers that).

    ``cache`` is the process-wide LocMemCache and both endpoints are rate
    limited to 10 POSTs per user per hour, with the counters surviving from one
    test to the next. This class POSTs more often than that as alice (always
    pk 1 after the per-test rollback), so it clears the cache on both sides of
    every test, exactly as RateLimitTests does and for the same reason: the
    classes that run after this one alphabetically must not inherit counters.
    """

    def setUp(self):
        cache.clear()
        super().setUp()
        self.alice_acct.refresh_from_db()
        self.bob_acct.refresh_from_db()

    def tearDown(self):
        cache.clear()
        super().tearDown()

    # ------------------------------------------------------------- helpers
    def set_limit(self, user, amount, period="day"):
        TransferLimit.objects.update_or_create(
            user=user, period=period,
            defaults={"amount": Decimal(amount)},
        )

    def confirm_transfer(self, txn, client=None):
        return (client or self.client).post(
            self.transfer_process_url(txn), {"password": PASSWORD}
        )

    def prior_outgoing(self, amount, status="completed"):
        """A transfer alice sent earlier today, already in its final status.

        Written straight to the table: these tests are about what the sum
        already contains, not about how it got there.
        """
        return self.create_transfer(amount=amount, status=status)

    def confirm_settlement(self, txn, client=None):
        return (client or self.payer_client()).post(
            self.settlement_process_url(txn), {"password": PASSWORD}
        )

    def rate_limited_entries(self, kind="transfer_limit"):
        return LogEntry.objects.filter(
            action="rate_limited", metadata__kind=kind
        )

    # ------------------------------------------------------------ transfers
    def test_transfer_under_limit_completes(self):
        txn = self.create_transfer(amount="50.00")

        resp = self.confirm_transfer(txn)

        self.assertEqual(resp.status_code, 302)
        self.assertIn("transfer-completed", resp["Location"])
        txn.refresh_from_db()
        self.assertEqual(txn.status, "completed")
        self.assertEqual(self.balances(), (Decimal("950.00"), Decimal("550.00")))
        self.assertFalse(self.rate_limited_entries().exists())

    def test_transfer_over_limit_refused(self):
        self.set_limit(self.alice, "100.00")
        txn = self.create_transfer(amount="150.00")
        before = self.balances()

        resp = self.confirm_transfer(txn)

        self.assertEqual(resp.status_code, 302)
        self.assertIn("transfer-confirmation", resp["Location"])
        self.assertNotIn("transfer-completed", resp["Location"])

        txn.refresh_from_db()
        self.assertEqual(txn.status, "processing")
        self.assertEqual(self.balances(), before)

        entries = self.rate_limited_entries()
        self.assertEqual(entries.count(), 1)
        self.assertEqual(entries.get().metadata["kind"], "transfer_limit")
        self.assertEqual(entries.get().metadata["period"], "day")
        # Compared as Decimals: the audit stores str() of the SUM's result, and
        # SQLite hands that back without trailing zeros ("0", not "0.00").
        self.assertEqual(Decimal(entries.get().metadata["used"]), Decimal("0.00"))
        self.assertEqual(Decimal(entries.get().metadata["limit"]), Decimal("100.00"))
        self.assertEqual(Decimal(entries.get().metadata["amount"]), Decimal("150.00"))

    def test_transfer_exactly_at_limit_passes(self):
        self.set_limit(self.alice, "100.00")
        txn = self.create_transfer(amount="100.00")

        # The in-flight row is still "processing", and only settled money counts
        # towards the window. If this row counted, the confirmation below would
        # be measuring the transfer against itself (100 in the window + 100 more
        # = 200 > 100) and would refuse a transfer that exactly meets the limit.
        self.assertEqual(_used_in_period(self.alice, "day"), Decimal("0.00"))

        resp = self.confirm_transfer(txn)

        self.assertIn("transfer-completed", resp["Location"])
        txn.refresh_from_db()
        self.assertEqual(txn.status, "completed")
        # 100.00 is not "more than" the limit, so the full amount moves.
        self.assertEqual(self.balances(), (Decimal("900.00"), Decimal("600.00")))
        # Now that it has settled, it does count -- for the next transfer.
        self.assertEqual(_used_in_period(self.alice, "day"), Decimal("100.00"))

    def test_transfer_over_limit_after_prior_transfer(self):
        self.set_limit(self.alice, "100.00")
        self.prior_outgoing("40.00")
        self.prior_outgoing("40.00")
        txn = self.create_transfer(amount="30.00")
        before = self.balances()

        resp = self.confirm_transfer(txn)

        self.assertIn("transfer-confirmation", resp["Location"])
        txn.refresh_from_db()
        self.assertEqual(txn.status, "processing")
        self.assertEqual(self.balances(), before)

        entry = self.rate_limited_entries().get()
        self.assertEqual(Decimal(entry.metadata["used"]), Decimal("80.00"))
        self.assertEqual(Decimal(entry.metadata["limit"]), Decimal("100.00"))

    def test_transfer_failed_prior_does_not_count(self):
        self.set_limit(self.alice, "100.00")
        self.prior_outgoing("500.00", status="failed")
        txn = self.create_transfer(amount="50.00")

        resp = self.confirm_transfer(txn)

        self.assertIn("transfer-completed", resp["Location"])
        txn.refresh_from_db()
        self.assertEqual(txn.status, "completed")
        self.assertEqual(self.balances(), (Decimal("950.00"), Decimal("550.00")))
        self.assertFalse(self.rate_limited_entries().exists())

    # ---------------------------------------------------------- settlements
    def test_settlement_over_limit_refused(self):
        self.set_limit(self.bob, "100.00")  # bob is the payer
        txn = self.create_request(amount="150.00", status="request_sent")
        before = self.balances()

        resp = self.confirm_settlement(txn)

        self.assertEqual(resp.status_code, 302)
        self.assertIn("settlement-confirmation", resp["Location"])
        self.assertNotIn("settlement-completed", resp["Location"])

        txn.refresh_from_db()
        self.assertEqual(txn.status, "request_sent")
        self.assertEqual(self.balances(), before)

        entries = self.rate_limited_entries()
        self.assertEqual(entries.count(), 1)
        self.assertEqual(entries.get().metadata["kind"], "transfer_limit")
        self.assertEqual(entries.get().metadata["period"], "day")
        self.assertEqual(entries.get().metadata["limit"], "100.00")

    def test_settlement_under_limit_completes(self):
        self.set_limit(self.bob, "1000.00")
        txn = self.create_request(amount="150.00", status="request_sent")

        resp = self.confirm_settlement(txn)

        self.assertEqual(resp.status_code, 302)
        self.assertIn("settlement-completed", resp["Location"])

        txn.refresh_from_db()
        self.assertEqual(txn.status, "request_settled")
        # bob (the stored reciever) pays; alice (the requester) is credited.
        self.assertEqual(self.balances(), (Decimal("1150.00"), Decimal("350.00")))
        self.assertFalse(self.rate_limited_entries().exists())

    # ------------------------------------------------------ the other party
    def test_limit_does_not_apply_to_receiver(self):
        self.set_limit(self.alice, "1.00")  # the receiver's limit is tiny
        txn = Transaction.objects.create(
            user=self.bob, sender=self.bob, reciever=self.alice,
            sender_account=self.bob_acct, reciever_account=self.alice_acct,
            amount=Decimal("200.00"), status="processing",
            transaction_type="transfer",
        )
        before = self.balances()

        bob = Client()
        bob.force_login(self.bob)
        resp = bob.post(
            reverse("core:transfer-process",
                    args=[self.alice_acct.account_number, txn.transaction_id]),
            {"password": PASSWORD},
        )

        self.assertEqual(resp.status_code, 302)
        self.assertIn("transfer-completed", resp["Location"])
        txn.refresh_from_db()
        self.assertEqual(txn.status, "completed")
        # bob's default limit (10,000.00) is what applied, not alice's 1.00.
        self.assertEqual(
            self.balances(),
            (before[0] + Decimal("200.00"), before[1] - Decimal("200.00")),
        )
        self.assertFalse(self.rate_limited_entries().exists())
