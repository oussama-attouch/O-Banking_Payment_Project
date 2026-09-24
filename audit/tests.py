"""Phase A: the immutable audit log.

The tests live here rather than in ``account/tests.py`` because every assertion
is about ``audit.models``, ``audit.utils`` or ``audit.admin``. The two flow tests
drive the existing money endpoints only to produce the rows under test; what the
money itself does is ``core/tests.py``'s contract, not this module's.

One deliberate deviation from the Phase A test list, recorded here because the
task asked for it: item 11 is worded "no row created if actor is None and no
request", but ``audit.utils.log`` as specified (and as implemented) always
INSERTs -- what it drops is only the *request* context. The gate list confirms
this ("ip ... None on a direct call with no request"), so the test asserts the
implemented behaviour: no exception, one row, ``actor`` and ``ip`` both None.
"""
import json
from decimal import Decimal

from django.contrib import admin
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse

from account.models import Account
from audit.admin import LogEntryAdmin
from audit.models import LogEntry
from audit.utils import log
from core.models import Transaction
from userauths.models import User

PASSWORD = "pw-Phase-1.5-test"


class AuditLogTests(TestCase):
    """alice is the actor; bob is the counterparty for the money flows."""

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

    def setUp(self):
        self.alice, self.alice_acct = self._make_user("alice", "1000.00")
        self.bob, self.bob_acct = self._make_user("bob", "500.00")
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

    def _entry(self, action):
        """The single LogEntry for ``action``, asserting there is exactly one."""
        entries = LogEntry.objects.filter(action=action)
        self.assertEqual(entries.count(), 1, "expected exactly one %r row" % action)
        return entries.get()

    # ------------------------------------------------- 1-6  immutability
    def test_log_entry_can_be_created(self):
        entry = LogEntry.objects.create(
            action="user_login", actor=self.alice, ip="127.0.0.1",
            user_agent="probe/1.0", metadata={"note": "created directly"},
        )
        self.assertTrue(LogEntry.objects.filter(pk=entry.pk).exists())
        stored = LogEntry.objects.get(pk=entry.pk)
        self.assertEqual(stored.action, "user_login")
        self.assertEqual(stored.actor, self.alice)
        self.assertEqual(stored.metadata, {"note": "created directly"})
        self.assertEqual(stored.target_type, "")

    def test_log_entry_save_on_existing_raises(self):
        entry = LogEntry.objects.create(action="user_login", actor=self.alice)
        entry.action = "user_logout"
        with self.assertRaises(PermissionError):
            entry.save()
        # The row is untouched: the refusal happens before any SQL is issued.
        self.assertEqual(LogEntry.objects.get(pk=entry.pk).action, "user_login")

    def test_log_entry_delete_raises(self):
        entry = LogEntry.objects.create(action="user_login", actor=self.alice)
        with self.assertRaises(PermissionError):
            entry.delete()
        self.assertTrue(LogEntry.objects.filter(pk=entry.pk).exists())

    def test_log_entry_queryset_update_raises(self):
        entry = LogEntry.objects.create(action="user_login", actor=self.alice)
        with self.assertRaises(PermissionError):
            LogEntry.objects.filter(pk=entry.pk).update(action="user_logout")
        self.assertEqual(LogEntry.objects.get(pk=entry.pk).action, "user_login")

    def test_log_entry_queryset_delete_raises(self):
        entry = LogEntry.objects.create(action="user_login", actor=self.alice)
        with self.assertRaises(PermissionError):
            LogEntry.objects.filter(pk=entry.pk).delete()
        self.assertTrue(LogEntry.objects.filter(pk=entry.pk).exists())

    def test_log_entry_bulk_create_raises(self):
        with self.assertRaises(PermissionError):
            LogEntry.objects.bulk_create(
                [LogEntry(action="user_login", actor=self.alice)]
            )
        self.assertEqual(LogEntry.objects.count(), 0)

    # ------------------------------------------------- 7-10  hooked views
    def test_audit_log_helper_records_login(self):
        """The hook in LoginView runs after login(), so the actor is known."""
        self.assertEqual(LogEntry.objects.count(), 0)

        resp = Client().post(
            reverse("userauths:sign-in"),
            {"email": self.alice.email, "password": PASSWORD},
            HTTP_USER_AGENT="audit-probe/1.0",
        )
        self.assertEqual(resp.status_code, 302)

        entry = self._entry("user_login")
        self.assertEqual(entry.actor, self.alice)
        self.assertEqual(entry.target_type, "User")
        self.assertEqual(entry.target_id, str(self.alice.pk))
        self.assertEqual(entry.ip, "127.0.0.1")
        self.assertEqual(entry.user_agent, "audit-probe/1.0")

    def test_audit_log_helper_records_register(self):
        resp = Client().post(reverse("userauths:sign-up"), {
            "username": "newbie", "email": "newbie@test.invalid",
            "password1": PASSWORD, "password2": PASSWORD,
        })
        self.assertEqual(resp.status_code, 302)

        new_user = User.objects.get(email="newbie@test.invalid")
        entry = self._entry("user_register")
        self.assertEqual(entry.actor, new_user)
        self.assertEqual(entry.target_id, str(new_user.pk))

    def test_audit_log_helper_records_transfer(self):
        created = self.client.post(
            reverse("core:amount-transfer-process", args=[self.bob_acct.account_number]),
            {"amount-send": "100.00", "description": "rent"},
        )
        self.assertEqual(created.status_code, 302)
        txn = (Transaction.objects.filter(transaction_type="transfer")
               .order_by("-id").first())

        confirmed = self.client.post(
            reverse("core:transfer-process",
                    args=[self.bob_acct.account_number, txn.transaction_id]),
            {"password": PASSWORD},
        )
        self.assertEqual(confirmed.status_code, 302)
        txn.refresh_from_db()
        self.assertEqual(txn.status, "completed")

        self.assertEqual(self._entry("transfer_created").target_id, str(txn.pk))
        entry = self._entry("transfer_confirmed")
        self.assertEqual(entry.target_id, str(txn.pk))
        self.assertEqual(entry.actor, self.alice)

    def test_audit_log_helper_records_settlement(self):
        txn = Transaction.objects.create(
            user=self.alice, sender=self.alice, reciever=self.bob,
            sender_account=self.alice_acct, reciever_account=self.bob_acct,
            amount=Decimal("10.00"), status="request_sent",
            transaction_type="request",
        )
        # Settlement is paid by the stored reciever, so bob is the actor and the
        # URL names the requester's account (see core.tests).
        payer = Client()
        payer.force_login(self.bob)
        resp = payer.post(
            reverse("core:settlement-processing",
                    args=[self.alice_acct.account_number, txn.transaction_id]),
            {"password": PASSWORD},
        )
        self.assertEqual(resp.status_code, 302)
        txn.refresh_from_db()
        self.assertEqual(txn.status, "request_settled")

        entry = self._entry("settlement_confirmed")
        self.assertEqual(entry.target_id, str(txn.pk))
        self.assertEqual(entry.actor, self.bob)

    # ------------------------------------------------- 11-12  no request, admin
    def test_audit_log_without_request_does_not_raise(self):
        """No thread-local request: the row is still written, with no ip.

        See the module docstring on why this asserts a row rather than no row.
        """
        self.assertIsNone(log("user_login"))

        entry = LogEntry.objects.get()
        self.assertEqual(entry.action, "user_login")
        self.assertIsNone(entry.actor)
        self.assertIsNone(entry.ip)
        self.assertEqual(entry.target_type, "")
        self.assertEqual(entry.target_id, "")
        self.assertEqual(entry.user_agent, "")

    def test_audit_admin_is_read_only(self):
        root = User.objects.create_superuser(
            username="root", email="root@test.invalid", password=PASSWORD
        )
        LogEntry.objects.create(action="user_login", actor=self.alice)

        staff = Client()
        staff.force_login(root)
        self.assertEqual(
            staff.get(reverse("admin:audit_logentry_changelist")).status_code, 200
        )
        self.assertEqual(
            staff.get(reverse("admin:audit_logentry_add")).status_code, 403
        )

        model_admin = admin.site._registry[LogEntry]
        self.assertIsInstance(model_admin, LogEntryAdmin)
        request = RequestFactory().get("/admin/audit/logentry/")
        request.user = root
        self.assertFalse(model_admin.has_add_permission(request))
        self.assertFalse(model_admin.has_change_permission(request))
        self.assertFalse(model_admin.has_delete_permission(request))

    # ------------------------------------- 13-14  login failure, password change
    def test_login_failed_is_logged(self):
        """A wrong password leaves a trail, with no actor and no password.

        The email has to belong to a real user: LoginView looks the address up
        first, and its bare ``except`` swallows the miss, so an *unknown* email
        never reaches ``authenticate()`` and therefore never fires the signal.
        """
        wrong = "definitely-not-the-password"
        resp = Client().post(
            reverse("userauths:sign-in"),
            {"email": self.alice.email, "password": wrong},
        )
        self.assertEqual(resp.status_code, 302)

        entry = self._entry("user_login_failed")
        self.assertIsNone(entry.actor)
        self.assertEqual(entry.metadata.get("submitted_identifier"), self.alice.email)
        self.assertEqual(entry.ip, "127.0.0.1")
        # Django cleanses the password value before sending the signal; the
        # helper must not put it back.
        self.assertNotIn(wrong, json.dumps(entry.metadata))
        self.assertEqual(LogEntry.objects.filter(action="user_login").count(), 0)

    def test_password_change_is_logged(self):
        new_password = "pw-Phase-A2-new-pass"
        resp = self.client.post(reverse("account:password_change"), {
            "old_password": PASSWORD,
            "new_password1": new_password,
            "new_password2": new_password,
        })
        self.assertEqual(resp.status_code, 302)

        entry = self._entry("password_changed")
        self.assertEqual(entry.actor, self.alice)
        self.assertEqual(entry.target_type, "User")
        self.assertEqual(entry.target_id, str(self.alice.pk))
        # Nothing about the credential is recorded.
        self.assertEqual(entry.metadata, {})

        self.alice.refresh_from_db()
        self.assertTrue(self.alice.check_password(new_password))
