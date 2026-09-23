"""Regression guard for the A1 balance-corruption bug.

``account.models`` connects a ``post_save`` receiver on ``User``. It used to
connect a second one -- ``save_account`` -- that called
``instance.account.save()``. Reading ``.account`` caches the Account on the User
instance, so any later ``User.save()`` (which Django itself performs on every
login via ``update_last_login``) wrote that cached row back verbatim and
silently reverted balance changes made through a different Account instance.
"""
from datetime import timedelta
from decimal import Decimal
from io import BytesIO, StringIO
import csv
import re
import time

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection, models, transaction
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from account import analytics
from account.models import (
    Account,
    KYC,
    Notification,
    Recipient,
    SupportReply,
    SupportTicket,
)
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
        # Phase 7c renamed these payloads and made their length follow the
        # period filter, which defaults to 30d.
        self.assertEqual(len(response.context["daily_flow"]), 30)
        self.assertEqual(len(response.context["weekly_volume"]), 4)
        self.assertEqual(response.context["history_status"], "")
        self.assertEqual(response.context["history_type"], "")

    def test_dashboard_data_endpoint_returns_json(self):
        self.make_txn(self.bob, self.alice, "12.34")
        response = self.client.get(reverse("account:dashboard-data"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json")

        payload = response.json()
        for key in ("generated_at", "kpis", "daily_flow", "weekly_volume",
                    "status_breakdown", "top_counterparties", "kyc_status"):
            with self.subTest(key=key):
                self.assertIn(key, payload)

        # Every Decimal must have been rendered as an exact string.
        self.assertEqual(payload["kpis"]["balance"], "1000.00")
        self.assertEqual(payload["kpis"]["received"], "12.34")
        self.assertIsInstance(payload["kpis"]["received"], str)
        self.assertIsInstance(payload["kpis"]["pending_count"], int)
        self.assertIsInstance(payload["daily_flow"], list)
        self.assertEqual(len(payload["daily_flow"]), 30)
        self.assertIsInstance(payload["daily_flow"][0]["net"], str)

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
# Phase 7c  one filter bar drives every KPI and every chart
# =====================================================================
class FilterTests(DashboardAnalyticsTestBase):
    """``?period=`` and ``?type=`` on the dashboard and its JSON twin.

    alice is the logged-in owner (see the base fixture). The seed below gives her
    one transfer in and one transfer out, a settled request she raised, and a
    failed transfer, so every filter has something to include and something to
    exclude. Amounts are distinct so a wrong filter shows up as a wrong total.
    """

    def setUp(self):
        super().setUp()

        # Transfer in: alice received 100.
        self.make_txn(self.bob, self.alice, "100.00")
        # Transfer out: alice sent 40.
        self.make_txn(self.alice, self.bob, "40.00")
        # Request alice raised and bob settled: money IN for alice (direction rule).
        self.make_txn(self.alice, self.bob, "25.00", status="request_settled",
                      ttype="request")
        # A request that never completed: excluded from every flow figure.
        self.make_txn(self.alice, self.bob, "999.00", status="request_sent",
                      ttype="request")
        # A failed transfer: counted by neither filter's completed totals.
        self.make_txn(self.alice, self.bob, "777.00", status="failed")

    # --------------------------------------------------------------- period
    def test_dashboard_default_period_is_30d(self):
        response = self.client.get(reverse("account:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["active_period"], "30d")
        self.assertEqual(response.context["active_period_short"], "30d")
        self.assertEqual(response.context["compare_label"], "30 days")

    def test_dashboard_7d_period(self):
        response = self.client.get(reverse("account:dashboard"), {"period": "7d"})
        self.assertEqual(response.context["active_period"], "7d")
        self.assertEqual(response.context["active_period_short"], "7d")
        self.assertEqual(response.context["active_period_label"], "7 days")
        # The window really is 7 days, in both time series.
        self.assertEqual(len(response.context["daily_flow"]), 7)
        self.assertEqual(len(response.context["weekly_volume"]), 2)

    def test_dashboard_invalid_period_falls_back(self):
        response = self.client.get(reverse("account:dashboard"), {"period": "garbage"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["active_period"], "30d")
        self.assertEqual(len(response.context["daily_flow"]), 30)

    # ----------------------------------------------------------------- type
    def test_dashboard_type_filter_transfers(self):
        response = self.client.get(reverse("account:dashboard"), {"type": "transfer"})
        self.assertEqual(response.context["active_type"], "transfer")
        self.assertEqual(response.context["active_type_label"], "Transfers")

        kpis = response.context["kpis"]
        # 100 in and 40 out are both transfers; the settled request (25) is not.
        self.assertEqual(kpis["received"], Decimal("100.00"))
        self.assertEqual(kpis["sent"], Decimal("40.00"))
        self.assertEqual(response.context["kpis"]["transaction_count"], 3)
        # Every recent row is a transfer, and the request rows are gone.
        types = {t.transaction_type for t in response.context["recent_transactions"]}
        self.assertEqual(types, {"transfer"})

    def test_dashboard_invalid_type_falls_back(self):
        response = self.client.get(reverse("account:dashboard"), {"type": "garbage"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["active_type"], "all")
        self.assertEqual(response.context["active_type_label"], "All types")

    def test_dashboard_combined_filters(self):
        response = self.client.get(
            reverse("account:dashboard"), {"period": "7d", "type": "transfer"}
        )
        self.assertEqual(response.context["active_period"], "7d")
        self.assertEqual(response.context["active_type"], "transfer")
        self.assertEqual(len(response.context["daily_flow"]), 7)
        kpis = response.context["kpis"]
        self.assertEqual(kpis["received"], Decimal("100.00"),
                         "the settled request must be excluded by ?type=transfer")
        self.assertEqual(kpis["transaction_count"], 3)

    # --------------------------------------------------------------- labels
    def test_dashboard_kpi_label_uses_period(self):
        response = self.client.get(reverse("account:dashboard"), {"period": "90d"})
        body = response.content.decode()
        self.assertIn("Received (90d)", body)
        self.assertIn("Net flow (90d)", body)
        self.assertNotIn("Received (30d)", body)
        # The filter bar reports the same window the labels do.
        self.assertIn("Showing:", body)
        self.assertIn("90 days", body)

    def test_dashboard_all_period_has_no_comparison(self):
        response = self.client.get(reverse("account:dashboard"), {"period": "all"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["active_period"], "all")
        self.assertIsNone(
            response.context["compare_label"],
            "all time has no window before it to compare against",
        )
        for key, entry in response.context["kpi_deltas"].items():
            with self.subTest(kpi=key):
                self.assertIsNone(entry["delta_pct"])
        body = response.content.decode()
        self.assertIn("No comparison", body)
        # "All time" is capped rather than unbounded.
        self.assertLessEqual(len(response.context["daily_flow"]), 730)
        self.assertEqual(len(response.context["weekly_volume"]), 104)

    # ------------------------------------------------------------- JSON twin
    def test_dashboard_data_endpoint_respects_filters(self):
        response = self.client.get(
            reverse("account:dashboard-data"), {"period": "7d", "type": "transfer"}
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()

        for key in ("generated_at", "kpis", "daily_flow", "weekly_volume",
                    "status_breakdown", "top_counterparties", "kyc_status"):
            with self.subTest(key=key):
                self.assertIn(key, payload)

        # Same shape as before the filters existed...
        self.assertIsInstance(payload["daily_flow"], list)
        self.assertIsInstance(payload["weekly_volume"], list)
        self.assertIsInstance(payload["daily_flow"][0]["net"], str)
        # ...and the filters actually reached the helpers.
        self.assertEqual(len(payload["daily_flow"]), 7)
        self.assertEqual(payload["kpis"]["received"], "100.00")
        self.assertEqual(payload["kpis"]["sent"], "40.00")

        # An invalid pair falls back to the documented defaults, not to an error.
        fallback = self.client.get(
            reverse("account:dashboard-data"), {"period": "x", "type": "y"}
        ).json()
        self.assertEqual(len(fallback["daily_flow"]), 30)


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


# =====================================================================
# Phase 2b-1  KPI deltas and sparklines
# =====================================================================
FLAT_SPARKLINE = "0,10 10,10 20,10 30,10 40,10 50,10 60,10"


class KPIDeltaTests(DashboardAnalyticsTestBase):

    def parse_points(self, text):
        """'x,y x,y ...' -> [(x, y), ...] as floats."""
        return [
            (float(pair.split(",")[0]), float(pair.split(",")[1]))
            for pair in text.split()
        ]

    # ------------------------------------------------------------ shape
    def test_kpi_deltas_zero_for_fresh_user(self):
        deltas = analytics.get_kpi_deltas(self.dave)

        self.assertEqual(
            set(deltas),
            {"balance", "received", "sent", "net", "transaction_count",
             "average_amount", "pending_count", "largest_amount"},
        )
        for key, entry in deltas.items():
            with self.subTest(kpi=key):
                self.assertIsNone(
                    entry["delta_pct"],
                    "%s: nothing to compare against, so delta_pct must be None" % key,
                )
        for key in ("balance", "received", "sent", "net", "transaction_count",
                    "average_amount", "pending_count"):
            with self.subTest(kpi=key):
                self.assertEqual(deltas[key]["sparkline_points"], FLAT_SPARKLINE)
        self.assertEqual(deltas["largest_amount"]["sparkline_points"], "")

    # -------------------------------------------------------------- math
    def test_kpi_deltas_pct_math(self):
        # Previous window: 45 days ago. Current window: now.
        self.make_txn(self.bob, self.alice, "50.00",
                      when=timezone.now() - timedelta(days=45))
        self.make_txn(self.bob, self.alice, "100.00")

        deltas = analytics.get_kpi_deltas(self.alice)
        received = deltas["received"]
        self.assertEqual(received["current"], Decimal("100.00"))
        self.assertEqual(received["previous"], Decimal("50.00"))
        # (100 - 50) / 50 * 100 = 100.0
        self.assertAlmostEqual(received["delta_pct"], 100.0, delta=0.1)

        # A brand-new metric has nothing to compare against.
        self.assertIsNone(deltas["sent"]["delta_pct"])

        # balance is point-in-time: "previous" subtracts the *days* window's net
        # flow from today's balance. The 10-day-old movement is inside that
        # window but outside the 7-point sparkline window, so it distinguishes
        # the two anchors.
        Account.objects.filter(pk=self.alice_acct.pk).update(
            account_balance=Decimal("500.00")
        )
        self.make_txn(self.bob, self.alice, "30.00",
                      when=timezone.now() - timedelta(days=10))
        balance = analytics.get_kpi_deltas(self.alice)["balance"]
        self.assertEqual(balance["current"], Decimal("500.00"))
        self.assertEqual(
            balance["previous"], Decimal("370.00"),
            "balance.previous must span the full 30-day window (130.00 in), "
            "not just the 7-day sparkline window (100.00 in)",
        )
        self.assertAlmostEqual(balance["delta_pct"], 35.1, delta=0.1)

    # -------------------------------------------------------- sparkline
    def test_kpi_deltas_sparkline_normalization(self):
        """The largest value maps to y=2 and the smallest to y=18."""
        amounts = ["10.00", "20.00", "30.00", "40.00", "50.00", "60.00", "70.00"]
        for offset, amount in enumerate(amounts):
            self.make_txn(self.bob, self.alice, amount,
                          when=timezone.now() - timedelta(days=6 - offset))

        points = self.parse_points(
            analytics.get_kpi_deltas(self.alice)["received"]["sparkline_points"]
        )
        self.assertEqual(len(points), 7)
        self.assertEqual([x for x, _ in points], [0.0, 10.0, 20.0, 30.0, 40.0, 50.0, 60.0])

        ys = [y for _, y in points]
        self.assertEqual(ys[0], 18.0, "smallest value (10.00) must sit at y=18")
        self.assertEqual(ys[-1], 2.0, "largest value (70.00) must sit at y=2")
        self.assertEqual(min(ys), 2.0)
        self.assertEqual(max(ys), 18.0)
        # Values increase, so y must decrease monotonically.
        self.assertEqual(ys, sorted(ys, reverse=True))

    def test_kpi_deltas_sparkline_pads_missing_days(self):
        """Only 3 days of data still yields 7 points, padded at the FRONT.

        Documented behaviour: padding uses the value 0, and that zero becomes
        the series minimum -- so the padded points render along the bottom edge
        (y=18), not the centre line. The centre line (y=10) is only reached when
        every point in the series is equal.
        """
        for offset, amount in enumerate(["5.00", "10.00", "15.00"]):
            self.make_txn(self.bob, self.alice, amount,
                          when=timezone.now() - timedelta(days=2 - offset))

        points = self.parse_points(
            analytics.get_kpi_deltas(self.alice)["received"]["sparkline_points"]
        )
        self.assertEqual(len(points), 7)
        ys = [y for _, y in points]
        self.assertEqual(ys[:4], [18.0, 18.0, 18.0, 18.0],
                         "the four padded days are value 0 -> the series minimum")
        # The final three reflect the real 5 / 10 / 15 series.
        self.assertGreater(ys[4], ys[5])
        self.assertGreater(ys[5], ys[6])
        self.assertEqual(ys[6], 2.0)

    def test_kpi_deltas_largest_has_no_sparkline(self):
        self.make_txn(self.bob, self.alice, "123.00")
        largest = analytics.get_kpi_deltas(self.alice)["largest_amount"]
        self.assertEqual(largest["sparkline_points"], "")
        self.assertIsNone(largest["delta_pct"])
        self.assertEqual(largest["current"], Decimal("123.00"))

    # ---------------------------------------------------- query budget
    def test_kpi_deltas_query_budget(self):
        """Three queries today; the contract is at most four."""
        self.make_txn(self.bob, self.alice, "10.00")
        with CaptureQueriesContext(connection) as captured:
            analytics.get_kpi_deltas(self.alice)
        self.assertLessEqual(
            len(captured), 4,
            "get_kpi_deltas used %d queries, budget is 4" % len(captured),
        )
        self.assertEqual(len(captured), 3, "implementation detail moved")


# =====================================================================
# Phase 5d  user settings
# =====================================================================
class SettingsTests(TestCase):
    """The self-service settings screen: profile edit and password change.

    Settings is deliberately *not* KYC-gated -- only ``login_required`` -- so
    the user without a KYC row below is expected to get a 200, not a redirect.
    """

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
        # No KYC row on purpose: settings must stay reachable without one.
        self.user = User.objects.create_user(
            username="setter", email="setter@test.invalid", password=PASSWORD
        )
        self.account = Account.objects.get(user=self.user)
        self.client.force_login(self.user)

    # ------------------------------------------------------------- page
    def test_settings_requires_login(self):
        self.client.logout()
        resp = self.client.get(reverse("account:settings"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/user/sign-in/", resp["Location"])
        self.assertIn("next=", resp["Location"])

    def test_settings_renders_for_authenticated(self):
        """A KYC-less user still gets the page, and it shows both cards."""
        resp = self.client.get(reverse("account:settings"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Profile")
        self.assertContains(resp, "Account overview")
        self.assertContains(resp, "KYC status")
        self.assertContains(resp, self.account.account_number)

    # ---------------------------------------------------------- profile
    def test_settings_profile_update(self):
        resp = self.client.post(
            reverse("account:settings"),
            {"first_name": "Test", "last_name": "User", "email": "test@example.com"},
        )
        self.assertRedirects(resp, reverse("account:settings"))
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "Test")
        self.assertEqual(self.user.last_name, "User")
        self.assertEqual(self.user.email, "test@example.com")

    def test_settings_email_collision_rejected(self):
        """email is USERNAME_FIELD, so it must stay unique across accounts."""
        User.objects.create_user(
            username="taken", email="taken@test.invalid", password=PASSWORD
        )
        resp = self.client.post(
            reverse("account:settings"),
            {"first_name": "Test", "last_name": "User", "email": "taken@test.invalid"},
        )
        self.assertEqual(resp.status_code, 200, "a rejected save must re-render the page")
        self.assertContains(resp, "already exists")
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "setter@test.invalid")
        self.assertEqual(self.user.first_name, "", "nothing may be saved on a collision")

    # --------------------------------------------------------- password
    def test_password_change_requires_login(self):
        self.client.logout()
        resp = self.client.get(reverse("account:password_change"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/user/sign-in/", resp["Location"])

    def test_password_change_updates_password(self):
        new_password = "Phase-5d-brand-new-pw"
        resp = self.client.post(
            reverse("account:password_change"),
            {
                "old_password": PASSWORD,
                "new_password1": new_password,
                "new_password2": new_password,
            },
        )
        self.assertRedirects(resp, reverse("account:settings"))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(new_password))

    def test_password_change_rejects_wrong_old(self):
        new_password = "Phase-5d-brand-new-pw"
        resp = self.client.post(
            reverse("account:password_change"),
            {
                "old_password": "not-the-current-password",
                "new_password1": new_password,
                "new_password2": new_password,
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(PASSWORD))
        self.assertFalse(self.user.check_password(new_password))


# =====================================================================
# Phase 5e  statements
# =====================================================================
class StatementsTests(TestCase):
    """The statements page and its CSV export.

    The page carries the dashboard's KYC gate, so both users below have a KYC
    row; the export is login-only by design.
    """

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
        self.alice = self.make_user("stmt_alice")
        self.bob = self.make_user("stmt_bob")
        self.client.force_login(self.alice)

    def make_user(self, name):
        user = User.objects.create_user(
            username=name, email="%s@test.invalid" % name, password=PASSWORD
        )
        acct = Account.objects.get(user=user)
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
        return user

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

    def range_button(self, resp, key):
        """The opening tag of the range selector button for ``key``.

        Anchored on ``statements/?range=`` so the CSV link, which carries the
        same query string, cannot be mistaken for a selector button.
        """
        match = re.search(
            r'<a[^>]+href="[^"]*statements/\?range=%s"[^>]*>' % re.escape(key),
            resp.content.decode(),
        )
        self.assertIsNotNone(match, "no range button rendered for %r" % key)
        return match.group(0)

    # -------------------------------------------------------------- page
    def test_statements_requires_login(self):
        self.client.logout()
        resp = self.client.get(reverse("account:statements"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/user/sign-in/", resp["Location"])

    def test_statements_renders_for_authenticated(self):
        resp = self.client.get(reverse("account:statements"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Statements")

    def test_statements_default_range_is_this_month(self):
        resp = self.client.get(reverse("account:statements"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("btn-primary", self.range_button(resp, "this_month"))
        for other in ("last_3_months", "this_year", "last_12_months"):
            with self.subTest(range=other):
                self.assertIn("btn-outline-secondary", self.range_button(resp, other))

    def test_statements_unknown_range_falls_back(self):
        default = self.client.get(reverse("account:statements"))
        garbage = self.client.get(reverse("account:statements"), {"range": "garbage"})
        self.assertEqual(garbage.status_code, 200)
        self.assertEqual(
            garbage.context["range_key"], default.context["range_key"],
            "an unknown range must normalise to the default",
        )
        self.assertIn("btn-primary", self.range_button(garbage, "this_month"))

    # ----------------------------------------------------------- summary
    def test_statements_summary_respects_direction(self):
        # Money in: a completed transfer where alice is the reciever.
        self.make_txn(self.bob, self.alice, "100.00")
        # Money in: a settled request where alice is the requester (sender).
        self.make_txn(self.alice, self.bob, "50.00", status="request_settled",
                      ttype="request")
        summary = self.client.get(reverse("account:statements")).context["summary"]
        self.assertEqual(summary["in"], Decimal("150.00"))
        self.assertEqual(summary["out"], Decimal("0.00"))

        # Money out: a completed transfer where alice is the sender.
        self.make_txn(self.alice, self.bob, "30.00")
        summary = self.client.get(reverse("account:statements")).context["summary"]
        self.assertEqual(summary["out"], Decimal("30.00"))
        self.assertEqual(summary["net"], Decimal("120.00"))
        self.assertEqual(summary["count"], 3)

        # A movement still in flight is counted as a row but moves no money.
        self.make_txn(self.bob, self.alice, "999.00", status="processing")
        context = self.client.get(reverse("account:statements")).context
        self.assertEqual(context["summary"]["in"], Decimal("150.00"))
        self.assertEqual(context["summary"]["out"], Decimal("30.00"))
        self.assertEqual(context["summary"]["count"], 4)
        self.assertIn("", [row["direction"] for row in context["transactions"]],
                      "an in-flight row must render unsigned")

    # ----------------------------------------------------------- monthly
    def test_statements_monthly_breakdown(self):
        when = timezone.now()
        self.make_txn(self.bob, self.alice, "40.00", when=when)
        self.make_txn(self.alice, self.bob, "10.00", when=when)
        monthly = self.client.get(reverse("account:statements")).context["monthly"]

        # The default range is a single month, so the breakdown has one row.
        self.assertEqual(len(monthly), 1)
        self.assertEqual(monthly[0]["count"], 2)
        self.assertEqual(monthly[0]["in"], Decimal("40.00"))
        self.assertEqual(monthly[0]["out"], Decimal("10.00"))
        self.assertEqual(monthly[0]["net"], Decimal("30.00"))
        self.assertEqual(monthly[0]["month_label"], when.strftime("%b %Y"))

    # -------------------------------------------------------------- csv
    def test_csv_export_requires_login(self):
        self.client.logout()
        resp = self.client.get(reverse("account:statements_export"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/user/sign-in/", resp["Location"])

    def test_csv_export_content(self):
        self.make_txn(self.bob, self.alice, "12.50", description="for lunch")
        self.make_txn(self.alice, self.bob, "7.25")

        resp = self.client.get(reverse("account:statements_export"))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp["Content-Type"].startswith("text/csv"),
                        resp["Content-Type"])
        self.assertTrue(resp["Content-Disposition"].startswith("attachment"),
                        resp["Content-Disposition"])

        rows = list(csv.reader(StringIO(resp.content.decode("utf-8"))))
        self.assertEqual(len(rows), 3, "one header row plus two data rows: %s" % rows)
        self.assertEqual(rows[0], ["date", "type", "direction", "counterparty",
                                   "amount", "status", "reference", "description"])
        self.assertEqual({row[2] for row in rows[1:]}, {"in", "out"})
        self.assertEqual({row[4] for row in rows[1:]}, {"12.50", "7.25"})
        self.assertEqual({row[3] for row in rows[1:]}, {"Stmt_Bob Person"})

    def test_csv_export_neutralises_formula_injection(self):
        """A counterparty name is attacker-controlled; a formula must not run.

        KYC.full_name is set by the other party, and the description is free
        text, so either can begin with a spreadsheet formula trigger.
        """
        evil_name = '=HYPERLINK("http://evil","x")'
        KYC.objects.filter(user=self.bob).update(full_name=evil_name)
        self.make_txn(self.bob, self.alice, "5.00", description="+CMD|test")

        # Companion: an ordinary name must come through untouched.
        clean = self.make_user("stmt_carol")
        KYC.objects.filter(user=clean).update(full_name="Alice Smith")
        self.make_txn(clean, self.alice, "2.00")

        body = self.client.get(reverse("account:statements_export")).content.decode("utf-8")

        self.assertIn("'=HYPERLINK", body, "the name cell must be quoted for the spreadsheet")
        self.assertIn("'+CMD|test", body, "the description cell must be quoted")
        for line in body.splitlines():
            self.assertFalse(line.startswith("=HYPERLINK"), line)
            self.assertFalse(line.startswith("+CMD"), line)

        rows = list(csv.reader(StringIO(body)))
        counterparties = {row[3] for row in rows[1:]}
        descriptions = {row[7] for row in rows[1:]}
        self.assertIn("'" + evil_name, counterparties)
        self.assertIn("'+CMD|test", descriptions)
        self.assertIn("Alice Smith", counterparties)
        self.assertNotIn("'Alice Smith", counterparties,
                         "an ordinary name must not gain an apostrophe")
        for row in rows[1:]:
            for cell in (row[3], row[7]):
                self.assertFalse(cell.startswith(("=", "+", "-", "@")),
                                 "unescaped formula cell: %r" % cell)


# =====================================================================
# Phase 5f-1  saved payees
# =====================================================================
class RecipientTests(TestCase):
    """account.Recipient: the saved-payee list.

    Model-only coverage -- Phase 5f-2 adds the views that use it. Everything
    here runs against the test database, never db.sqlite3.
    """

    def setUp(self):
        self.alice, self.alice_acct = self.make_user("rec_alice", kyc=True)
        self.bob, self.bob_acct = self.make_user("rec_bob", kyc=True)
        self.carol, self.carol_acct = self.make_user("rec_carol", kyc=False)

    def make_user(self, name, kyc):
        user = User.objects.create_user(
            username=name, email="%s@test.invalid" % name, password=PASSWORD
        )
        acct = Account.objects.get(user=user)
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
        return user, acct

    # ---------------------------------------------------------- creation
    def test_recipient_can_be_created(self):
        recipient = Recipient.objects.create(user=self.bob, target_account=self.alice_acct)
        self.assertTrue(Recipient.objects.filter(pk=recipient.pk).exists())
        self.assertEqual(recipient.user_id, self.bob.pk)
        self.assertEqual(recipient.target_account_id, self.alice_acct.pk)
        self.assertEqual(recipient.nickname, "")
        self.assertEqual(recipient.display_name, "Rec_Alice Person")
        # The reverse accessors are the ones the 5f-2 list view will use.
        self.assertEqual(list(self.bob.recipients.all()), [recipient])
        self.assertEqual(list(self.alice_acct.saved_by.all()), [recipient])

    def test_recipient_nickname_overrides_display_name(self):
        recipient = Recipient.objects.create(
            user=self.bob, target_account=self.alice_acct, nickname="Alice (work)"
        )
        self.assertEqual(recipient.display_name, "Alice (work)")
        self.assertIn("Alice (work)", str(recipient))

    def test_recipient_display_name_falls_back_to_username(self):
        """carol has no KYC row, so the username is all there is to show."""
        self.assertFalse(KYC.objects.filter(user=self.carol).exists())
        recipient = Recipient.objects.create(user=self.bob, target_account=self.carol_acct)
        self.assertEqual(recipient.display_name, "rec_carol")

    # ------------------------------------------------------------ guards
    def test_recipient_cannot_save_self(self):
        recipient = Recipient(user=self.alice, target_account=self.alice_acct)
        with self.assertRaises(ValidationError):
            recipient.save()
        # The form/admin path goes through clean() instead, which attaches the
        # message to the target_account field.
        with self.assertRaises(ValidationError) as caught:
            Recipient(user=self.alice, target_account=self.alice_acct).full_clean()
        self.assertIn("target_account", caught.exception.message_dict)
        self.assertFalse(Recipient.objects.filter(user=self.alice).exists())

    def test_recipient_unique_per_user(self):
        Recipient.objects.create(user=self.bob, target_account=self.alice_acct)
        with self.assertRaises(IntegrityError):
            # Inner atomic block: a caught IntegrityError would otherwise leave
            # the test's own transaction unusable.
            with transaction.atomic():
                Recipient.objects.create(user=self.bob, target_account=self.alice_acct)
        self.assertEqual(
            Recipient.objects.filter(user=self.bob, target_account=self.alice_acct).count(), 1
        )
        constraint = next(
            c for c in Recipient._meta.constraints
            if isinstance(c, models.UniqueConstraint)
        )
        self.assertEqual(constraint.name, "unique_recipient_per_user")
        self.assertEqual(set(constraint.fields), {"user", "target_account"})

    def test_recipient_different_users_can_save_same_target(self):
        first = Recipient.objects.create(user=self.alice, target_account=self.carol_acct)
        second = Recipient.objects.create(user=self.bob, target_account=self.carol_acct)
        self.assertEqual(Recipient.objects.filter(target_account=self.carol_acct).count(), 2)
        self.assertNotEqual(first.pk, second.pk)

    # ----------------------------------------------------------- cascades
    def test_recipient_cascade_on_target_account_delete(self):
        recipient = Recipient.objects.create(user=self.bob, target_account=self.alice_acct)
        self.alice_acct.delete()
        self.assertFalse(Recipient.objects.filter(pk=recipient.pk).exists())

    def test_recipient_cascade_on_user_delete(self):
        recipient = Recipient.objects.create(user=self.bob, target_account=self.alice_acct)
        self.bob.delete()
        self.assertFalse(Recipient.objects.filter(pk=recipient.pk).exists())


# =====================================================================
# Phase 5f-2  recipients page
# =====================================================================
class RecipientViewTests(TestCase):
    """The /account/recipients/ page: list, inline add, POST-only delete."""

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
        self.alice, self.alice_acct = self.make_user("pay_alice", kyc=True)
        self.bob, self.bob_acct = self.make_user("pay_bob", kyc=True)
        self.carol, self.carol_acct = self.make_user("pay_carol", kyc=False)
        self.client.force_login(self.bob)

    def make_user(self, name, kyc=True):
        user = User.objects.create_user(
            username=name, email="%s@test.invalid" % name, password=PASSWORD
        )
        acct = Account.objects.get(user=user)
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
        return user, acct

    # -------------------------------------------------------------- page
    def test_recipients_requires_login(self):
        self.client.logout()
        resp = self.client.get(reverse("account:recipients"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/user/sign-in/", resp["Location"])

    def test_recipients_renders_empty_for_new_user(self):
        resp = self.client.get(reverse("account:recipients"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "No recipients saved yet")
        self.assertContains(resp, "Add a recipient")

    # --------------------------------------------------------------- add
    def test_recipients_add_success(self):
        resp = self.client.post(reverse("account:recipients"), {
            "account_number": self.alice_acct.account_number,
            "nickname": "Alice",
        })
        self.assertRedirects(resp, reverse("account:recipients"))
        saved = Recipient.objects.filter(user=self.bob)
        self.assertEqual(saved.count(), 1)
        self.assertEqual(saved.first().target_account_id, self.alice_acct.pk)
        self.assertEqual(saved.first().nickname, "Alice")

    def test_recipients_add_by_account_id(self):
        resp = self.client.post(reverse("account:recipients"), {
            "account_number": self.alice_acct.account_id,
            "nickname": "",
        })
        self.assertRedirects(resp, reverse("account:recipients"))
        self.assertEqual(
            Recipient.objects.filter(user=self.bob, target_account=self.alice_acct).count(), 1
        )

    def test_recipients_add_unknown_account_rejected(self):
        resp = self.client.post(reverse("account:recipients"), {
            "account_number": "9999999999999",
            "nickname": "",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "No account matches that number or ID")
        self.assertEqual(Recipient.objects.filter(user=self.bob).count(), 0)

    def test_recipients_add_self_rejected(self):
        resp = self.client.post(reverse("account:recipients"), {
            "account_number": self.bob_acct.account_number,
            "nickname": "",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "cannot save your own account")
        self.assertEqual(Recipient.objects.filter(user=self.bob).count(), 0)

    def test_recipients_add_duplicate_rejected(self):
        Recipient.objects.create(user=self.bob, target_account=self.alice_acct)
        resp = self.client.post(reverse("account:recipients"), {
            "account_number": self.alice_acct.account_number,
            "nickname": "again",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "already saved this account")
        self.assertEqual(Recipient.objects.filter(user=self.bob).count(), 1)

    def test_recipients_cap_enforced(self):
        """The 50-per-user cap is a view rule, not a model constraint."""
        from account.views import MAX_RECIPIENTS_PER_USER

        targets = []
        for index in range(MAX_RECIPIENTS_PER_USER):
            user = User.objects.create_user(
                username="cap%d" % index,
                email="cap%d@test.invalid" % index,
                password=PASSWORD,
            )
            targets.append(Account.objects.get(user=user))
        Recipient.objects.bulk_create(
            [Recipient(user=self.bob, target_account=acct) for acct in targets]
        )
        self.assertEqual(
            Recipient.objects.filter(user=self.bob).count(), MAX_RECIPIENTS_PER_USER
        )

        resp = self.client.post(
            reverse("account:recipients"),
            {"account_number": self.alice_acct.account_number, "nickname": "one more"},
            follow=True,
        )
        self.assertContains(resp, "at most %d recipients" % MAX_RECIPIENTS_PER_USER)
        self.assertEqual(
            Recipient.objects.filter(user=self.bob).count(), MAX_RECIPIENTS_PER_USER
        )
        self.assertFalse(
            Recipient.objects.filter(user=self.bob, target_account=self.alice_acct).exists()
        )

    # ------------------------------------------------------------ delete
    def test_recipients_delete_requires_post(self):
        recipient = Recipient.objects.create(user=self.bob, target_account=self.alice_acct)
        resp = self.client.get(
            reverse("account:recipient_delete", args=[recipient.pk])
        )
        self.assertEqual(resp.status_code, 405)
        self.assertTrue(Recipient.objects.filter(pk=recipient.pk).exists())

    def test_recipients_delete_own_recipient(self):
        recipient = Recipient.objects.create(user=self.bob, target_account=self.alice_acct)
        resp = self.client.post(
            reverse("account:recipient_delete", args=[recipient.pk])
        )
        self.assertRedirects(resp, reverse("account:recipients"))
        self.assertFalse(Recipient.objects.filter(pk=recipient.pk).exists())

    def test_recipients_delete_other_users_recipient_is_noop(self):
        """alice's saved payee must survive bob posting its delete URL."""
        recipient = Recipient.objects.create(user=self.alice, target_account=self.carol_acct)
        resp = self.client.post(
            reverse("account:recipient_delete", args=[recipient.pk]),
            follow=True,
        )
        self.assertContains(resp, "Recipient not found.")
        self.assertTrue(Recipient.objects.filter(pk=recipient.pk).exists())


# =====================================================================
# Phase 5g-1  money-event notifications
# =====================================================================
class NotificationTests(TestCase):
    """The signal in account/notifications.py observes core.Transaction and
    writes Notification rows when a row newly enters a settled state.

    These are pure-ORM tests: they exercise the receiver, not a view, so
    there is no client and no SECURE_* override.
    """

    def setUp(self):
        self.alice = User.objects.create_user(
            username="notif_alice", email="notif_alice@test.invalid", password=PASSWORD
        )
        self.bob = User.objects.create_user(
            username="notif_bob", email="notif_bob@test.invalid", password=PASSWORD
        )
        # create_account provisions one Account per User on creation.
        self.alice_acct = Account.objects.get(user=self.alice)
        self.bob_acct = Account.objects.get(user=self.bob)

    def make_txn(self, status, ttype="transfer", amount="25.00"):
        return Transaction.objects.create(
            user=self.alice,
            sender=self.alice,
            reciever=self.bob,
            sender_account=self.alice_acct,
            reciever_account=self.bob_acct,
            amount=Decimal(amount),
            status=status,
            transaction_type=ttype,
        )

    @staticmethod
    def link_for(txn):
        return "/transaction-detail/%s/" % txn.transaction_id

    # ------------------------------------------------------- settled: money in/out
    def test_notification_created_on_completed_transfer(self):
        txn = self.make_txn("completed")

        self.assertEqual(Notification.objects.count(), 2)

        received = Notification.objects.filter(user=self.bob)
        sent = Notification.objects.filter(user=self.alice)
        self.assertEqual(received.count(), 1)
        self.assertEqual(sent.count(), 1)

        self.assertEqual(received.first().kind, Notification.KIND_MONEY_IN)
        self.assertEqual(sent.first().kind, Notification.KIND_MONEY_OUT)

        # Both parties get a link to the same transaction.
        self.assertEqual(received.first().link, self.link_for(txn))
        self.assertEqual(sent.first().link, self.link_for(txn))

    def test_notification_created_on_request_settled(self):
        txn = self.make_txn("request_settled", ttype="request")

        rows = Notification.objects.all()
        self.assertEqual(rows.count(), 2)
        self.assertEqual(
            set(rows.values_list("user_id", flat=True)),
            {self.alice.pk, self.bob.pk},
        )
        for row in rows:
            self.assertEqual(row.kind, Notification.KIND_SETTLED)
            self.assertEqual(row.link, self.link_for(txn))

    # -------------------------------------------------------------- no false positives
    def test_notification_not_created_for_pending_transfer(self):
        self.make_txn("processing")
        self.assertEqual(Notification.objects.count(), 0)

    def test_notification_not_created_on_second_save(self):
        txn = self.make_txn("completed")
        self.assertEqual(Notification.objects.count(), 2)

        # Re-saving the same row in the same settled status must not duplicate.
        txn.save()
        self.assertEqual(Notification.objects.count(), 2)

    # ------------------------------------------------------------- pending request
    def test_pending_request_notification(self):
        self.make_txn("request_sent", ttype="request")

        rows = Notification.objects.all()
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().kind, Notification.KIND_REQUEST)
        self.assertEqual(rows.first().user_id, self.bob.pk)

    def test_pending_request_notification_fires_on_transition(self):
        """The live view creates the request as request_processing and only
        later saves it as request_sent, so created is False on the transition
        that matters."""
        txn = self.make_txn("request_processing", ttype="request")
        self.assertEqual(Notification.objects.count(), 0)

        txn.status = "request_sent"
        txn.save()

        rows = Notification.objects.filter(kind=Notification.KIND_REQUEST)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().user_id, self.bob.pk)

        # Re-saving at the same status must not duplicate.
        txn.save()
        self.assertEqual(Notification.objects.count(), 1)

    # ------------------------------------------------------------------- helper
    def test_notify_helper_noop_for_none_user(self):
        from account.notifications import notify

        self.assertIsNone(notify(None, Notification.KIND_KYC, "Nobody"))
        self.assertEqual(Notification.objects.count(), 0)

    # ----------------------------------------------------------------- cascade
    def test_notification_cascade_on_user_delete(self):
        Notification.objects.create(
            user=self.bob, kind=Notification.KIND_KYC, title="Your KYC was approved"
        )
        self.assertEqual(Notification.objects.filter(user=self.bob).count(), 1)

        self.bob.delete()
        self.assertEqual(Notification.objects.count(), 0)

    # ----------------------------------------------------------------- indexes
    def test_notification_indexes_exist(self):
        fields = [tuple(index.fields) for index in Notification._meta.indexes]
        self.assertIn(("user", "-created_at"), fields)
        self.assertIn(("user", "is_read"), fields)


# =====================================================================
# Phase 5g-2  the bell, the dropdown and the notifications page
# =====================================================================
class NotificationViewTests(TestCase):
    """The topbar bell and /account/notifications/.

    KYC is required: the topbar tests read /account/dashboard/, which is
    gated by account.views._kyc_required.
    """

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
        self.alice, self.alice_acct = self.make_user("nv_alice")
        self.bob, self.bob_acct = self.make_user("nv_bob")
        self.client.force_login(self.alice)

    def make_user(self, name):
        user = User.objects.create_user(
            username=name, email="%s@test.invalid" % name, password=PASSWORD
        )
        acct = Account.objects.get(user=user)
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
        return user, acct

    def make_notification(self, user, title="You received 10.00", kind=None, is_read=False):
        return Notification.objects.create(
            user=user,
            kind=kind or Notification.KIND_MONEY_IN,
            title=title,
            body="body",
            link="/transaction-detail/TRNtest000000001/",
            is_read=is_read,
        )

    # ------------------------------------------------------------- the page
    def test_notifications_requires_login(self):
        self.client.logout()
        resp = self.client.get(reverse("account:notifications"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/user/sign-in/", resp["Location"])

    def test_notifications_renders_for_authenticated(self):
        resp = self.client.get(reverse("account:notifications"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Money events on your account")

    def test_notifications_lists_only_own_rows(self):
        self.make_notification(self.alice, title="Alice event")
        self.make_notification(self.bob, title="Bob event")

        resp = self.client.get(reverse("account:notifications"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Alice event")
        self.assertNotContains(resp, "Bob event")

    # --------------------------------------------------------- mark one read
    def test_notification_mark_read_requires_post(self):
        mine = self.make_notification(self.alice)
        resp = self.client.get(
            reverse("account:notification_mark_read", args=[mine.pk])
        )
        self.assertEqual(resp.status_code, 405)
        mine.refresh_from_db()
        self.assertFalse(mine.is_read)

    def test_notification_mark_read_own_row(self):
        mine = self.make_notification(self.alice)
        resp = self.client.post(
            reverse("account:notification_mark_read", args=[mine.pk])
        )
        self.assertEqual(resp.status_code, 302)
        mine.refresh_from_db()
        self.assertTrue(mine.is_read)

    def test_notification_mark_read_other_user_is_noop(self):
        theirs = self.make_notification(self.bob)
        resp = self.client.post(
            reverse("account:notification_mark_read", args=[theirs.pk]), follow=True
        )
        self.assertContains(resp, "Notification not found.")
        theirs.refresh_from_db()
        self.assertFalse(theirs.is_read)

    def test_notification_mark_all_read_marks_only_own(self):
        self.make_notification(self.alice, title="Alice one")
        self.make_notification(self.alice, title="Alice two")
        theirs = self.make_notification(self.bob, title="Bob one")

        resp = self.client.post(reverse("account:notification_mark_all_read"))
        self.assertEqual(resp.status_code, 302)

        self.assertEqual(
            Notification.objects.filter(user=self.alice, is_read=False).count(), 0
        )
        theirs.refresh_from_db()
        self.assertFalse(theirs.is_read)

    # ------------------------------------------------------------- the bell
    def test_topbar_shows_unread_count_badge(self):
        self.make_notification(self.alice, title="One")
        self.make_notification(self.alice, title="Two")

        resp = self.client.get(reverse("account:dashboard"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "ti-bell")
        self.assertRegex(
            resp.content.decode(),
            r'badge-notification[^>]*>\s*2\s*<',
        )

    def test_topbar_bell_hidden_when_zero(self):
        resp = self.client.get(reverse("account:dashboard"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "ti-bell")
        self.assertNotContains(resp, "badge-notification")


# =====================================================================
# Phase 5h-1  support tickets
# =====================================================================
class SupportTests(TestCase):
    """The SupportTicket / SupportReply models.

    Pure-ORM tests: Phase 5h-2 adds the views and the template, so there is
    no client here.
    """

    def setUp(self):
        self.alice = User.objects.create_user(
            username="sup_alice", email="sup_alice@test.invalid", password=PASSWORD
        )
        self.staff = User.objects.create_user(
            username="sup_staff",
            email="sup_staff@test.invalid",
            password=PASSWORD,
            is_staff=True,
        )

    def make_ticket(self, **kwargs):
        kwargs.setdefault("user", self.alice)
        kwargs.setdefault("subject", "Card declined")
        return SupportTicket.objects.create(**kwargs)

    # ------------------------------------------------------------- the ticket
    def test_support_ticket_can_be_created(self):
        ticket = self.make_ticket()
        self.assertTrue(SupportTicket.objects.filter(pk=ticket.pk).exists())
        self.assertEqual(ticket.status, SupportTicket.STATUS_OPEN)
        self.assertEqual(ticket.priority, SupportTicket.PRIORITY_NORMAL)

    def test_support_ticket_str(self):
        ticket = self.make_ticket(subject="Card declined")
        self.assertTrue(str(ticket).startswith("#%d " % ticket.pk), str(ticket))
        self.assertEqual(str(ticket), "#%d Card declined" % ticket.pk)

    def test_support_ticket_is_open_property(self):
        ticket = self.make_ticket()
        for status, expected in [
            (SupportTicket.STATUS_OPEN, True),
            (SupportTicket.STATUS_IN_PROGRESS, True),
            (SupportTicket.STATUS_RESOLVED, False),
            (SupportTicket.STATUS_CLOSED, False),
        ]:
            ticket.status = status
            self.assertIs(ticket.is_open, expected, status)

    # -------------------------------------------------------------- replies
    def test_support_reply_can_be_created(self):
        ticket = self.make_ticket()
        reply = SupportReply.objects.create(
            ticket=ticket, author=self.staff, body="Looking into it."
        )
        self.assertTrue(SupportReply.objects.filter(pk=reply.pk).exists())
        self.assertEqual(ticket.replies.count(), 1)
        self.assertIn(reply, ticket.replies.all())

    def test_support_reply_from_staff(self):
        ticket = self.make_ticket()
        from_staff = SupportReply.objects.create(
            ticket=ticket, author=self.staff, body="We are on it."
        )
        from_user = SupportReply.objects.create(
            ticket=ticket, author=self.alice, body="Thanks!"
        )
        self.assertTrue(from_staff.from_staff)
        self.assertFalse(from_user.from_staff)

    def test_support_ticket_updated_at_changes_on_reply(self):
        """A reply does NOT touch the parent ticket -- documented, then proved.

        ``SupportReply`` has no ``save()`` override and nothing connects a
        signal to it, so ``auto_now`` on ``SupportTicket.updated_at`` only
        fires when the ticket itself is saved. This test pins the real
        behaviour (the reply leaves ``updated_at`` alone) and then shows the
        field does advance on an explicit ``ticket.save()``. Phase 5h-2 has to
        save the ticket itself if it wants a replied-to thread to sort to the
        top of the ``-updated_at`` ordering.
        """
        ticket = self.make_ticket()
        before = ticket.updated_at

        time.sleep(0.01)
        SupportReply.objects.create(ticket=ticket, author=self.staff, body="Any news?")
        ticket.refresh_from_db()
        self.assertEqual(ticket.updated_at, before)

        time.sleep(0.01)
        ticket.save()
        ticket.refresh_from_db()
        self.assertGreater(ticket.updated_at, before)

    # ------------------------------------------------------------- cascades
    def test_support_ticket_cascade_on_user_delete(self):
        ticket = self.make_ticket()
        # Authored by staff on purpose: the reply can then only disappear via
        # the ticket cascade, not via its own author FK.
        SupportReply.objects.create(ticket=ticket, author=self.staff, body="On it.")
        pk = ticket.pk

        self.alice.delete()

        self.assertFalse(SupportTicket.objects.filter(pk=pk).exists())
        self.assertFalse(SupportReply.objects.filter(ticket_id=pk).exists())

    def test_support_reply_cascade_on_ticket_delete(self):
        ticket = self.make_ticket()
        SupportReply.objects.create(ticket=ticket, author=self.alice, body="One")
        SupportReply.objects.create(ticket=ticket, author=self.staff, body="Two")
        pk = ticket.pk

        ticket.delete()

        self.assertFalse(SupportReply.objects.filter(ticket_id=pk).exists())

    # -------------------------------------------------------------- indexes
    def test_support_ticket_indexes_exist(self):
        fields = [tuple(index.fields) for index in SupportTicket._meta.indexes]
        self.assertIn(("user", "-updated_at"), fields)
        self.assertIn(("status", "-updated_at"), fields)

    def test_support_reply_index_exists(self):
        fields = [tuple(index.fields) for index in SupportReply._meta.indexes]
        self.assertIn(("ticket", "created_at"), fields)


# =====================================================================
# Phase 5h-2  support UI
# =====================================================================
class SupportViewTests(TestCase):
    """The /account/support/ pages: list + inline create, detail + reply.

    No KYC row is created: both views are login-only (like
    ``account:recipients``), not ``_kyc_required``-gated.
    """

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
        self.alice = User.objects.create_user(
            username="sv_alice", email="sv_alice@test.invalid", password=PASSWORD
        )
        self.bob = User.objects.create_user(
            username="sv_bob", email="sv_bob@test.invalid", password=PASSWORD
        )
        self.client.force_login(self.alice)

    def make_ticket(self, user=None, subject="Card declined"):
        return SupportTicket.objects.create(user=user or self.alice, subject=subject)

    # --------------------------------------------------------------- access
    def test_support_requires_login(self):
        self.client.logout()
        resp = self.client.get(reverse("account:support"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/user/sign-in/", resp["Location"])

    def test_support_renders_empty(self):
        resp = self.client.get(reverse("account:support"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Open a new ticket")
        self.assertContains(resp, "Your tickets")
        self.assertContains(resp, "No tickets yet")

    # --------------------------------------------------------------- create
    def test_support_create_ticket_and_first_reply(self):
        resp = self.client.post(reverse("account:support"), {
            "subject": "Card declined",
            "message": "My card was declined this morning.",
        })

        ticket = SupportTicket.objects.get()
        self.assertRedirects(
            resp, reverse("account:support_detail", args=[ticket.pk])
        )
        self.assertEqual(SupportTicket.objects.count(), 1)
        self.assertEqual(ticket.user_id, self.alice.pk)
        self.assertEqual(ticket.subject, "Card declined")
        self.assertEqual(ticket.status, SupportTicket.STATUS_OPEN)

        reply = SupportReply.objects.get()
        self.assertEqual(reply.ticket_id, ticket.pk)
        self.assertEqual(reply.author_id, self.alice.pk)
        self.assertEqual(reply.body, "My card was declined this morning.")

    def test_support_create_rejects_blank_subject(self):
        resp = self.client.post(reverse("account:support"), {
            "subject": "",
            "message": "No subject given.",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(SupportTicket.objects.count(), 0)
        self.assertEqual(SupportReply.objects.count(), 0)

    # --------------------------------------------------------------- detail
    def test_support_detail_only_own_ticket(self):
        ticket = self.make_ticket()
        self.client.force_login(self.bob)

        resp = self.client.get(
            reverse("account:support_detail", args=[ticket.pk]), follow=True
        )
        self.assertContains(resp, "Ticket not found.")
        self.assertEqual(resp.redirect_chain[-1][0], reverse("account:support"))
        self.assertTrue(SupportTicket.objects.filter(pk=ticket.pk).exists())

    def test_support_detail_404_for_missing_pk(self):
        resp = self.client.get(
            reverse("account:support_detail", args=[999999])
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], reverse("account:support"))

    # ---------------------------------------------------------------- reply
    def test_support_reply_appends_and_bumps_updated_at(self):
        ticket = self.make_ticket()
        before = ticket.updated_at

        time.sleep(0.01)
        resp = self.client.post(
            reverse("account:support_detail", args=[ticket.pk]),
            {"body": "Any update on this?"},
        )
        self.assertRedirects(
            resp, reverse("account:support_detail", args=[ticket.pk])
        )

        self.assertEqual(ticket.replies.count(), 1)
        self.assertEqual(ticket.replies.get().body, "Any update on this?")

        ticket.refresh_from_db()
        self.assertGreater(ticket.updated_at, before)

    def test_support_reply_rejected_on_closed_ticket(self):
        ticket = self.make_ticket()
        ticket.status = SupportTicket.STATUS_CLOSED
        ticket.save(update_fields=["status"])
        before = SupportReply.objects.count()

        resp = self.client.post(
            reverse("account:support_detail", args=[ticket.pk]),
            {"body": "One more thing."},
            follow=True,
        )

        self.assertEqual(SupportReply.objects.count(), before)
        self.assertContains(resp, "This ticket is closed")
        # The reply form must not be rendered for a closed ticket.
        self.assertNotContains(resp, "Post reply")

    def test_support_detail_reply_requires_ownership(self):
        ticket = self.make_ticket(user=self.alice)
        self.client.force_login(self.bob)

        resp = self.client.post(
            reverse("account:support_detail", args=[ticket.pk]),
            {"body": "I should not be able to post this."},
            follow=True,
        )

        self.assertContains(resp, "Ticket not found.")
        self.assertEqual(SupportReply.objects.count(), 0)
        self.assertEqual(ticket.replies.count(), 0)

    # -------------------------------------------------------------- ordering
    def test_support_list_orders_by_updated_at_desc(self):
        older = self.make_ticket(subject="Older ticket")
        time.sleep(0.01)
        newer = self.make_ticket(subject="Newer ticket")

        # Newest-created first, before any reply.
        self.assertEqual(
            list(SupportTicket.objects.filter(user=self.alice)),
            [newer, older],
        )

        time.sleep(0.01)
        self.client.post(
            reverse("account:support_detail", args=[older.pk]),
            {"body": "Replying to the older ticket."},
        )

        # The reply bumped the older ticket, so it must now sort first. This is
        # what proves the explicit ticket.save(update_fields=["updated_at"]).
        body = self.client.get(reverse("account:support")).content.decode()
        self.assertLess(
            body.index("Older ticket"),
            body.index("Newer ticket"),
            "the replied-to ticket must sort above the newer one",
        )
        self.assertEqual(
            list(SupportTicket.objects.filter(user=self.alice)),
            [older, newer],
        )
