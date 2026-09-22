"""Regression guard for the A1 balance-corruption bug.

``account.models`` connects a ``post_save`` receiver on ``User``. It used to
connect a second one -- ``save_account`` -- that called
``instance.account.save()``. Reading ``.account`` caches the Account on the User
instance, so any later ``User.save()`` (which Django itself performs on every
login via ``update_last_login``) wrote that cached row back verbatim and
silently reverted balance changes made through a different Account instance.
"""
from decimal import Decimal
from io import BytesIO

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from account import analytics
from account.models import Account, KYC
from core.models import Transaction
from userauths.models import User

PASSWORD = "pw-Phase-1.5-test"


def png_bytes():
    """A real 1x1 PNG.

    ``KYCForm.image`` and ``.signature`` are ImageFields, so Pillow validates
    whatever is uploaded -- arbitrary bytes would be rejected by the form rather
    than reaching the view.
    """
    buffer = BytesIO()
    Image.new("RGB", (1, 1), "white").save(buffer, format="PNG")
    return buffer.getvalue()


class AccountProvisioningTests(TestCase):

    @classmethod
    def setUpClass(cls):
        # See core.tests: hardening follows DEBUG, so the SSL redirect and secure
        # cookies are on and would break a plain-HTTP test client.
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

    def test_new_user_gets_exactly_one_account(self):
        """The surviving receiver: Account.user is a required one-to-one."""
        user = User.objects.create_user(
            username="solo", email="solo@test.invalid", password=PASSWORD
        )
        self.assertEqual(Account.objects.filter(user=user).count(), 1)
        self.assertTrue(user.account.account_number)

    def test_account_model_has_no_pin_field(self):
        """Phase 1b: the 4-digit account PIN was removed entirely.

        Money movement now re-enters the account password and verifies it with
        django.contrib.auth.check_password, so no PIN column should remain.
        """
        user = User.objects.create_user(
            username="nopin", email="nopin@test.invalid", password=PASSWORD
        )
        self.assertFalse(hasattr(user.account, "account_pin"))

    def test_user_save_does_not_revert_a_balance_changed_elsewhere(self):
        user = User.objects.create_user(
            username="guard", email="guard@test.invalid", password=PASSWORD
        )
        # Read .account so it is cached on this User instance -- the exact
        # condition that made the removed receiver destructive.
        cached = user.account
        self.assertEqual(cached.account_balance, Decimal("0.00"))

        # Change the balance through a different Account instance.
        Account.objects.filter(pk=cached.pk).update(account_balance=Decimal("1000.00"))
        self.assertEqual(Account.objects.get(pk=cached.pk).account_balance, Decimal("1000.00"))
        self.assertEqual(user.account.account_balance, Decimal("0.00"))  # stale, as expected

        # Exactly what django.contrib.auth.login -> update_last_login does.
        user.last_login = timezone.now()
        user.save(update_fields=["last_login"])

        self.assertEqual(
            Account.objects.get(pk=cached.pk).account_balance,
            Decimal("1000.00"),
            "User.save() reverted the balance - the save_account receiver is back",
        )


# =====================================================================
# Phase 2a-dash-1  dashboard analytics
# =====================================================================
class DashboardAnalyticsTestBase(TestCase):
    """alice owns the dashboard. bob has KYC, carol has none, dave is a stranger."""

    @classmethod
    def setUpClass(cls):
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
        self.alice, self.alice_acct = self.make_user("alice", "1000.00", kyc=True)
        self.bob, self.bob_acct = self.make_user("bob", "0.00", kyc=True)
        self.carol, self.carol_acct = self.make_user("carol", "0.00", kyc=False)
        self.dave, self.dave_acct = self.make_user("dave", "0.00", kyc=False)
        self.client.force_login(self.alice)

    def make_user(self, name, balance, kyc):
        user = User.objects.create_user(
            username=name, email="%s@test.invalid" % name, password=PASSWORD
        )
        acct = Account.objects.get(user=user)
        Account.objects.filter(pk=acct.pk).update(account_balance=Decimal(balance))
        acct.refresh_from_db()
        if kyc:
            KYC.objects.create(
                user=user,
                account=acct,
                full_name="%s Person" % name.title(),
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
            )
        user.refresh_from_db()
        return user, acct

    def make_txn(self, sender, reciever, amount, status="completed",
                 ttype="transfer", description="", when=None):
        txn = Transaction.objects.create(
            user=sender,
            sender=sender,
            reciever=reciever,
            sender_account=Account.objects.get(user=sender),
            reciever_account=Account.objects.get(user=reciever),
            amount=Decimal(amount),
            status=status,
            transaction_type=ttype,
            description=description,
        )
        if when is not None:
            # date is auto_now_add, so it can only be moved with an UPDATE.
            Transaction.objects.filter(pk=txn.pk).update(date=when)
            txn.refresh_from_db()
        return txn


class DashboardAnalyticsTests(DashboardAnalyticsTestBase):

    # ------------------------------------------------------------- KPIs
    def test_kpis_zero_for_fresh_user(self):
        kpis = analytics.get_kpis(self.dave)
        for key in ("balance", "received", "sent", "net", "average_amount", "largest_amount"):
            with self.subTest(key=key):
                self.assertIsInstance(kpis[key], Decimal)
                self.assertEqual(kpis[key], Decimal("0.00"))
        self.assertEqual(kpis["pending_count"], 0)
        self.assertEqual(kpis["transaction_count"], 0)
        self.assertEqual(kpis["largest_counterparty"], "")

    def test_kpis_counts_only_completed(self):
        self.make_txn(self.bob, self.alice, "250.00", status="processing")
        kpis = analytics.get_kpis(self.alice, account=self.alice_acct)
        self.assertEqual(kpis["received"], Decimal("0.00"))
        self.assertEqual(kpis["sent"], Decimal("0.00"))
        self.assertEqual(kpis["net"], Decimal("0.00"))
        self.assertEqual(kpis["transaction_count"], 1)
        self.assertEqual(kpis["pending_count"], 1)

    def test_kpis_pending_count_includes_request_states(self):
        for status in ("processing", "request_sent", "request_processing"):
            self.make_txn(self.bob, self.alice, "10.00", status=status)
        self.make_txn(self.bob, self.alice, "10.00", status="completed")
        self.make_txn(self.bob, self.alice, "10.00", status="failed")
        kpis = analytics.get_kpis(self.alice, account=self.alice_acct)
        self.assertEqual(kpis["pending_count"], 3)

    def test_kpis_average_and_largest(self):
        for amount in ("10.00", "20.00", "30.00"):
            self.make_txn(self.bob, self.alice, amount)
        kpis = analytics.get_kpis(self.alice, account=self.alice_acct)
        self.assertEqual(kpis["average_amount"], Decimal("20.00"))
        self.assertEqual(kpis["largest_amount"], Decimal("30.00"))
        self.assertEqual(kpis["received"], Decimal("60.00"))

    def test_kpis_largest_counterparty_name(self):
        """KYC full name when there is one, otherwise the username."""
        self.make_txn(self.bob, self.alice, "40.00")     # bob has KYC
        self.make_txn(self.carol, self.alice, "10.00")   # carol does not
        kpis = analytics.get_kpis(self.alice, account=self.alice_acct)
        self.assertEqual(kpis["largest_amount"], Decimal("40.00"))
        self.assertEqual(kpis["largest_counterparty"], "Bob Person")

        # Make carol's the largest and re-check the fallback.
        self.make_txn(self.carol, self.alice, "99.00")
        kpis = analytics.get_kpis(self.alice, account=self.alice_acct)
        self.assertEqual(kpis["largest_amount"], Decimal("99.00"))
        self.assertEqual(kpis["largest_counterparty"], "carol")

    def test_kpis_settled_request_credits_the_requester(self):
        """Direction is not "sender pays". A settled request flows reciever -> sender."""
        # alice raised the request (she is `sender`); bob settled it.
        self.make_txn(self.alice, self.bob, "70.00", status="request_settled",
                      ttype="request")
        kpis = analytics.get_kpis(self.alice, account=self.alice_acct)
        self.assertEqual(kpis["received"], Decimal("70.00"))
        self.assertEqual(kpis["sent"], Decimal("0.00"))
        self.assertEqual(kpis["net"], Decimal("70.00"))

    # ------------------------------------------------------- time series
    def test_daily_net_flow_length_90(self):
        flow = analytics.get_daily_net_flow(self.alice)
        self.assertEqual(len(flow), 90)
        dates = [row["date"] for row in flow]
        self.assertEqual(dates, sorted(dates), "not ascending")
        self.assertEqual(len(set(dates)), 90, "duplicate buckets")
        for row in flow:
            self.assertIsInstance(row["received"], Decimal)
            self.assertIsInstance(row["sent"], Decimal)
            self.assertIsInstance(row["net"], Decimal)
        self.assertEqual(sum(row["received"] for row in flow), Decimal("0.00"))

    def test_weekly_volume_length_12(self):
        volume = analytics.get_weekly_volume(self.alice)
        self.assertEqual(len(volume), 12)
        starts = [row["week_start"] for row in volume]
        self.assertEqual(starts, sorted(starts), "not ascending")
        from datetime import date as _date
        for value in starts:
            year, month, day = (int(p) for p in value.split("-"))
            self.assertEqual(
                _date(year, month, day).weekday(), 0, "%s is not a Monday" % value
            )
        self.assertEqual(sum(row["received"] for row in volume), Decimal("0.00"))

    def test_daily_flow_buckets_a_completed_transaction(self):
        self.make_txn(self.bob, self.alice, "15.00")
        flow = analytics.get_daily_net_flow(self.alice, days=7)
        self.assertEqual(len(flow), 7)
        today = flow[-1]
        self.assertEqual(today["date"], timezone.localdate().isoformat())
        self.assertEqual(today["received"], Decimal("15.00"))
        self.assertEqual(today["net"], Decimal("15.00"))

    def test_status_breakdown_keys_stable(self):
        self.make_txn(self.bob, self.alice, "10.00", status="completed")
        breakdown = analytics.get_status_breakdown(self.alice)
        self.assertEqual(set(breakdown), set(analytics.STATUS_KEYS))
        self.assertEqual(breakdown["completed"], 1)
        for key, value in breakdown.items():
            with self.subTest(status=key):
                self.assertIsInstance(value, int)
        self.assertEqual(breakdown["request_settled"], 0)

    # ------------------------------------------------------- counterparties
    def test_recent_transactions_excludes_non_parties(self):
        mine = self.make_txn(self.bob, self.alice, "10.00")
        theirs = self.make_txn(self.carol, self.dave, "10.00")
        ids = [t.pk for t in analytics.get_recent_transactions(self.alice)]
        self.assertIn(mine.pk, ids)
        self.assertNotIn(theirs.pk, ids)

    def test_recent_transactions_respects_limit_and_order(self):
        for amount in ("1.00", "2.00", "3.00"):
            self.make_txn(self.bob, self.alice, amount)
        recent = analytics.get_recent_transactions(self.alice, limit=2)
        self.assertEqual(len(recent), 2)
        self.assertGreaterEqual(recent[0].date, recent[1].date)

    def test_top_counterparties_aggregates(self):
        self.make_txn(self.bob, self.alice, "10.00")
        self.make_txn(self.bob, self.alice, "15.00")
        rows = analytics.get_top_counterparties(self.alice)
        self.assertEqual(len(rows), 1, "same counterparty must collapse to one row")
        self.assertEqual(rows[0]["count"], 2)
        self.assertEqual(rows[0]["volume"], Decimal("25.00"))
        self.assertEqual(rows[0]["name"], "Bob Person")

    def test_top_counterparties_sorted_by_volume(self):
        self.make_txn(self.bob, self.alice, "10.00")
        self.make_txn(self.carol, self.alice, "90.00")
        rows = analytics.get_top_counterparties(self.alice)
        self.assertEqual([r["name"] for r in rows], ["carol", "Bob Person"])
        self.assertEqual(rows[0]["volume"], Decimal("90.00"))

    def test_top_counterparties_empty_for_fresh_user(self):
        self.assertEqual(analytics.get_top_counterparties(self.dave), [])

    # --------------------------------------------------------------- KYC
    def test_kyc_status_progress(self):
        """none -> 0, pending -> 50, confirmed -> 100.

        `submitted` also counts the existence of a KYC row: nothing in this
        application ever writes the kyc_submitted flag, so the row is the only
        real signal that anything was filed.
        """
        fresh = analytics.get_kyc_status(self.dave, account=self.dave_acct,
                                         has_kyc=False)
        self.assertEqual((fresh["state"], fresh["progress"]), ("none", 0))
        self.assertFalse(fresh["submitted"])

        pending = analytics.get_kyc_status(self.bob, account=self.bob_acct,
                                           has_kyc=True)
        self.assertEqual((pending["state"], pending["progress"]), ("pending", 50))
        self.assertTrue(pending["submitted"])
        self.assertFalse(pending["confirmed"])

        Account.objects.filter(pk=self.bob_acct.pk).update(kyc_confirmed=True)
        confirmed = analytics.get_kyc_status(
            self.bob, account=Account.objects.get(pk=self.bob_acct.pk), has_kyc=True
        )
        self.assertEqual((confirmed["state"], confirmed["progress"]),
                         ("confirmed", 100))
        self.assertTrue(confirmed["confirmed"])

    # ---------------------------------------------------------- history
    def test_transaction_history_pagination(self):
        for index in range(45):
            self.make_txn(self.bob, self.alice, "1.00", description="t%d" % index)

        first = analytics.get_transaction_history(self.alice, per_page=20)
        self.assertEqual(first["page"], 1)
        self.assertEqual(first["total"], 45)
        self.assertEqual(first["total_pages"], 3)
        self.assertEqual(len(first["items"]), 20)
        self.assertTrue(first["has_next"])
        self.assertFalse(first["has_prev"])

        third = analytics.get_transaction_history(self.alice, page=3, per_page=20)
        self.assertEqual(third["page"], 3)
        self.assertEqual(len(third["items"]), 5)
        self.assertFalse(third["has_next"])
        self.assertTrue(third["has_prev"])

    def test_transaction_history_status_filter(self):
        self.make_txn(self.bob, self.alice, "1.00", status="completed")
        self.make_txn(self.bob, self.alice, "1.00", status="processing")
        self.make_txn(self.bob, self.alice, "1.00", status="failed")

        page = analytics.get_transaction_history(self.alice, status="completed")
        self.assertEqual(page["total"], 1)
        self.assertEqual([t.status for t in page["items"]], ["completed"])

        # An unknown status must not widen the query.
        unfiltered = analytics.get_transaction_history(self.alice, status="nonsense")
        self.assertEqual(unfiltered["total"], 3)

    def test_transaction_history_type_filter(self):
        self.assertIn("request", analytics.TYPE_KEYS)
        self.make_txn(self.bob, self.alice, "1.00", ttype="transfer")
        self.make_txn(self.bob, self.alice, "1.00", ttype="request")
        self.make_txn(self.bob, self.alice, "1.00", ttype="request")

        page = analytics.get_transaction_history(self.alice, ttype="request")
        self.assertEqual(page["total"], 2)
        self.assertEqual({t.transaction_type for t in page["items"]}, {"request"})

    # ------------------------------------------------------------ views
    def test_dashboard_view_requires_kyc(self):
        no_kyc = User.objects.create_user(
            username="nokyc", email="nokyc@test.invalid", password=PASSWORD
        )
        self.client.force_login(no_kyc)
        response = self.client.get(reverse("account:dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("account:kyc-reg"))

    def test_dashboard_view_renders(self):
        response = self.client.get(reverse("account:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "account/dashboard.html")
        self.assertEqual(response.context["account"].pk, self.alice_acct.pk)
        # The 2a-dash-1 stub renders no figures, so the balance is asserted on
        # the context the view supplies rather than on the response body.
        self.assertEqual(response.context["kpis"]["balance"], Decimal("1000.00"))
        self.assertEqual(len(response.context["daily_flow_90d"]), 90)
        self.assertEqual(len(response.context["weekly_volume_12w"]), 12)
        self.assertEqual(response.context["history_status"], "")
        self.assertEqual(response.context["history_type"], "")

    def test_dashboard_data_endpoint_returns_json(self):
        self.make_txn(self.bob, self.alice, "12.34")
        response = self.client.get(reverse("account:dashboard-data"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json")

        payload = response.json()
        for key in ("generated_at", "kpis", "daily_flow_90d", "weekly_volume_12w",
                    "status_breakdown", "top_counterparties", "kyc_status"):
            with self.subTest(key=key):
                self.assertIn(key, payload)

        # Every Decimal must have been rendered as an exact string.
        self.assertEqual(payload["kpis"]["balance"], "1000.00")
        self.assertEqual(payload["kpis"]["received"], "12.34")
        self.assertIsInstance(payload["kpis"]["received"], str)
        self.assertIsInstance(payload["kpis"]["pending_count"], int)
        self.assertIsInstance(payload["daily_flow_90d"], list)
        self.assertEqual(len(payload["daily_flow_90d"]), 90)
        self.assertIsInstance(payload["daily_flow_90d"][0]["net"], str)

    def test_dashboard_data_requires_kyc(self):
        no_kyc = User.objects.create_user(
            username="nokyc2", email="nokyc2@test.invalid", password=PASSWORD
        )
        self.client.force_login(no_kyc)
        response = self.client.get(reverse("account:dashboard-data"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("account:kyc-reg"))

    # ---------------------------------------------------- query budget
    def test_helper_query_counts_stay_within_budget(self):
        """Every helper must stay at or under 2 queries.

        Two helpers are deliberately at the top of that budget:
        ``get_kpis`` (one aggregate + one row fetch for the largest
        counterparty's name) and ``get_top_counterparties`` (one grouped
        aggregate + one name lookup).
        """
        self.make_txn(self.bob, self.alice, "10.00")
        self.make_txn(self.carol, self.alice, "20.00")

        cases = [
            ("get_balance", 0, lambda: analytics.get_balance(self.alice_acct)),
            ("get_kpis", 2,
             lambda: analytics.get_kpis(self.alice, account=self.alice_acct)),
            ("get_daily_net_flow", 1, lambda: analytics.get_daily_net_flow(self.alice)),
            ("get_weekly_volume", 1, lambda: analytics.get_weekly_volume(self.alice)),
            ("get_status_breakdown", 1,
             lambda: analytics.get_status_breakdown(self.alice)),
            ("get_recent_transactions", 1,
             lambda: analytics.get_recent_transactions(self.alice)),
            ("get_top_counterparties", 2,
             lambda: analytics.get_top_counterparties(self.alice)),
            ("get_kyc_status", 0,
             lambda: analytics.get_kyc_status(self.alice, account=self.alice_acct,
                                              has_kyc=True)),
            ("get_transaction_history", 2,
             lambda: analytics.get_transaction_history(self.alice)),
        ]
        for name, expected, call in cases:
            with self.subTest(helper=name):
                with CaptureQueriesContext(connection) as captured:
                    call()
                self.assertEqual(
                    len(captured), expected,
                    "%s used %d queries, expected %d" % (name, len(captured), expected),
                )


# =====================================================================
# Phase 3a  the kyc_submitted flag must become truthful
# =====================================================================
class KYCSubmissionFlagTests(DashboardAnalyticsTestBase):
    """``Account.kyc_submitted`` used to be declared but never written.

    Nothing in the application set it, so it read False for a user with a
    complete KYC row, and only ``analytics.get_kyc_status`` compensated (by
    treating row existence as the signal). Submitting the form now sets it.
    """

    def kyc_payload(self):
        return {
            "full_name": "Alice Person",
            "nationality": "MA",
            "marrital_status": "single",
            "gender": "male",
            "identity_type": "passport",
            "date_of_birth": "1990-01-01",
            "country": "Morocco",
            "state": "Casablanca",
            "city": "Casablanca",
            "mobile": "0600000000",
            "fax": "0522000000",
            "image": SimpleUploadedFile("id.png", png_bytes(), content_type="image/png"),
            "signature": SimpleUploadedFile("sig.png", png_bytes(), content_type="image/png"),
        }

    def test_kyc_submission_sets_submitted_flag(self):
        # alice is created by the base fixture WITHOUT a KYC row, so the flag
        # starts false and the account is genuinely un-submitted.
        self.assertFalse(Account.objects.get(pk=self.alice_acct.pk).kyc_submitted)

        response = self.client.post(
            reverse("account:kyc-reg"), self.kyc_payload(), follow=True
        )
        self.assertEqual(response.status_code, 200)

        account = Account.objects.get(pk=self.alice_acct.pk)
        self.assertTrue(
            account.kyc_submitted,
            "submitting the KYC form must set Account.kyc_submitted",
        )
        self.assertFalse(
            account.kyc_confirmed,
            "kyc_confirmed is an admin/compliance action and must stay false",
        )
        self.assertTrue(KYC.objects.filter(user=self.alice).exists())
