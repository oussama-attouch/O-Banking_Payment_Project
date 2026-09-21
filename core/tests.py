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
from decimal import Decimal

from django.test import Client, TestCase
from django.urls import get_resolver, reverse

from account.models import Account
from core.models import Transaction
from userauths.models import User

PASSWORD = "pw-Phase-1.5-test"


class MoneyMovementTestBase(TestCase):
    """alice (sender) and bob (recipient) are funded; carol is unrelated."""

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
        client = Client()
        client.force_login(self.carol)
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
            self.transfer_process_url(txn), {"pin-number": self.carol_acct.account_pin}
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
            self.transfer_process_url(txn), {"pin-number": self.alice_acct.account_pin}
        )
        self.assertEqual(done.status_code, 302)
        self.assertIn("transfer-completed", done["Location"])
        self.assertEqual(self.client.get(done["Location"]).status_code, 200)

        txn.refresh_from_db()
        self.assertEqual(txn.status, "completed")
        self.assertEqual(self.balances(), (Decimal("900.00"), Decimal("600.00")))

    def test_wrong_pin_does_not_move_money(self):
        txn = self.create_transfer(amount="100.00")
        before = self.balances()
        wrong = "0000" if self.alice_acct.account_pin != "0000" else "1111"

        resp = self.client.post(self.transfer_process_url(txn), {"pin-number": wrong})
        self.assertEqual(resp.status_code, 302)
        self.assertIn("transfer-confirmation", resp["Location"])

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
            {"pin-number": self.alice_acct.account_pin},
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
