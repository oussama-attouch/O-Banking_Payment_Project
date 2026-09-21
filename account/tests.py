"""Regression guard for the A1 balance-corruption bug.

``account.models`` connects a ``post_save`` receiver on ``User``. It used to
connect a second one -- ``save_account`` -- that called
``instance.account.save()``. Reading ``.account`` caches the Account on the User
instance, so any later ``User.save()`` (which Django itself performs on every
login via ``update_last_login``) wrote that cached row back verbatim and
silently reverted balance changes made through a different Account instance.
"""
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from account.models import Account
from userauths.models import User

PASSWORD = "pw-Phase-1.5-test"


class AccountProvisioningTests(TestCase):

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
