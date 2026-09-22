"""Seed a portfolio-scale demo dataset for O-Banking.

    manage.py seed_demo [--users 150] [--transactions 3000] [--months 24]
                        [--seed 42] [--force | --wipe]

The dataset exists so the dashboard shows something in a screenshot: a small
world of users with two years of transaction history, a realistic compliance
ratio, and balances that agree with the history they were generated from.

Three things about this application shape everything below.

**There is no ledger.** ``Account.account_balance`` is a mutable scalar that
nothing derives from ``Transaction`` (see ``account/analytics.py`` and the
module docstring of ``core/transfer.py``). A seeder that writes transactions
without also writing balances produces a dashboard whose "Balance" figure
contradicts its own flow chart, so balances are computed here from the very
rows that were generated, replaying the same mutation the views perform.

**Direction is type-aware.** A transfer moves money sender -> reciever, but a
settled payment request moves it reciever -> sender: the requester is stored as
``sender`` and the party who owes is stored as ``reciever``
(``core/payment_request.py`` creates the row that way; ``Settlement_processing``
debits whoever the settlement URL names, which the transaction list only ever
renders for the debtor). ``account/analytics.py::_received_q`` encodes the same
rule. This command reuses it rather than restating it, so the seeded balance and
the dashboard can never disagree about who gained money.

**Passwords are hashed once.** ``PBKDF2PasswordHasher`` at Django 5.2's
iteration count costs about a second per hash on a laptop, so calling
``create_user(password=...)`` 150 times would spend ~150 s on hashing alone --
nearly twice this command's whole runtime budget. Every seeded user instead
receives the *same* real PBKDF2 hash of ``DemoPass123!``, computed once. The
plaintext password is identical either way and login behaves normally; only the
salt is shared, which is irrelevant for throwaway demo rows and keeps the run
inside budget.

Nothing here is imported by the application: it is a developer tool that writes
rows through the public ORM. No model, view, form, template, migration or
dependency is touched, and no index is added (at this scale the query plans in
the Phase 0 recon make one unnecessary).
"""
import math
import random
import time
from datetime import date, datetime, time as clock_time, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.contrib.auth.hashers import make_password
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction as db_transaction
from django.db.models import Q
from django.utils import timezone

from account.models import (
    GENDER,
    IDENTITY_TYPE,
    MARRTIAL_STATUS,
    Account,
    KYC,
)
from core.models import Transaction
from core.security import MAX_AMOUNT
from userauths.models import User

# --------------------------------------------------------------------- sentinels
#: Every row this command creates is identifiable from these two values alone.
DEMO_PASSWORD = "DemoPass123!"
DEMO_USERNAME_PREFIX = "demo_user_"
DEMO_EMAIL_DOMAIN = "@demo.local"
DEMO_SIGNATURE = "kyc/demo_signature.png"
DEMO_DESCRIPTION = "Demo seed"

#: Composition of the graph, per 150 users. Scaled proportionally for smaller runs.
HUB_COUNT = 20
REGULAR_COUNT = 100
DORMANT_COUNT = 30

#: Fraction of rows that involve a hub, and of users that file KYC.
HUB_TRAFFIC_SHARE = 0.30
KYC_RATIO = 0.80
KYC_CONFIRMED_RATIO = 0.90

#: Type/status mix per 100 rows: 75 settled transfers, 15 settled requests,
#: 10 still in flight (6 transfers mid-flight, 4 requests awaiting settlement --
#: exactly the two shapes the views themselves create). The pending rows are
#: what make the Pending KPI non-zero.
MIX_PER_100 = (
    (("transfer", "completed"),) * 75
    + (("request", "request_settled"),) * 15
    + (("transfer", "processing"),) * 6
    + (("request", "request_sent"),) * 4
)

#: Share of rows that land on the last two business days of a month.
PAYDAY_SHARE = 0.20
#: Share of rows inside 08:00-18:00 rather than the surrounding hours.
BUSINESS_HOURS_SHARE = 0.80

#: Log-normal money: regular-to-regular is small, anything touching a hub is not.
SMALL_MEAN, SMALL_SIGMA, SMALL_RANGE = 150.0, 0.80, (5.0, 2000.0)
LARGE_MEAN, LARGE_SIGMA, LARGE_RANGE = 800.0, 0.90, (100.0, 15000.0)

#: Opening balance added on top of each user's net flow.
OPENING_RANGE = (500, 5000)

FIRST_NAMES = (
    "Youssef", "Amina", "Karim", "Salma", "Omar", "Nadia", "Hicham", "Leila",
    "Mehdi", "Sara", "Anas", "Imane", "Reda", "Hajar", "Bilal", "Meryem",
    "Adil", "Khadija", "Zakaria", "Soukaina", "Rachid", "Fatima", "Yassine",
    "Ghita", "Hamza", "Nour", "Tarik", "Asmae", "Ismail", "Wiam",
)
LAST_NAMES = (
    "El Amrani", "Benali", "Tazi", "Bennani", "Alaoui", "Cherkaoui", "Idrissi",
    "Fassi", "Berrada", "Lahlou", "Sebti", "Kabbaj", "Ouazzani", "Bouzidi",
    "Naciri", "Mansouri", "Hakimi", "Ziani", "Rifai", "Sqalli",
)
COUNTRIES = ("Morocco", "France", "Spain", "Belgium", "Canada")
CITIES = ("Casablanca", "Rabat", "Marrakech", "Fes", "Tangier", "Agadir")
STATES = ("Casablanca-Settat", "Rabat-Sale-Kenitra", "Marrakech-Safi", "Fes-Meknes")


class _Node:
    """One seeded user plus its graph role. Kept plain: no ORM round-trips."""

    __slots__ = ("idx", "pk", "account_pk", "is_hub", "is_dormant", "hub_idx",
                 "clients", "has_kyc")

    def __init__(self, idx, pk, account_pk, is_hub, is_dormant):
        self.idx = idx
        self.pk = pk
        self.account_pk = account_pk
        self.is_hub = is_hub
        self.is_dormant = is_dormant
        self.hub_idx = 0
        self.clients = []
        self.has_kyc = False


class _EdgeQueue:
    """A shuffled edge list consumed in order, reshuffled once exhausted.

    Sampling edges with ``random.choice`` leaves most of them unused: 900 hub
    rows drawn from 776 hub edges would touch only about two thirds of them, so
    a hub's 30-50 partners would mostly never appear in a transaction. Cycling
    a shuffled queue instead gives every edge a turn before any edge repeats.
    """

    __slots__ = ("edges", "pos")

    def __init__(self, edges):
        self.edges = list(edges)
        self.pos = 0
        random.shuffle(self.edges)

    def next(self):
        if not self.edges:
            return None
        if self.pos >= len(self.edges):
            random.shuffle(self.edges)
            self.pos = 0
        edge = self.edges[self.pos]
        self.pos += 1
        return edge


def _pair(a, b):
    """Unordered edge key for two nodes."""
    return (a.idx, b.idx) if a.idx < b.idx else (b.idx, a.idx)


def _lognormal(mean, sigma, low, high):
    """One log-normally distributed amount, 2dp, inside ``low``..``high``."""
    mu = math.log(mean) - (sigma * sigma) / 2.0
    value = min(max(random.lognormvariate(mu, sigma), low), high)
    amount = Decimal(repr(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if amount > MAX_AMOUNT:
        amount = MAX_AMOUNT
    if amount <= 0:
        amount = Decimal("0.01")
    return amount


class Command(BaseCommand):
    help = "Seed 150 users, 3000 transactions over 24 months and ~120 KYC rows."

    def add_arguments(self, parser):
        parser.add_argument("--users", type=int, default=150,
                            help="number of demo users to create (default 150)")
        parser.add_argument("--transactions", type=int, default=3000,
                            help="number of transactions to create (default 3000)")
        parser.add_argument("--months", type=int, default=24,
                            help="months of history to spread them over (default 24)")
        parser.add_argument("--seed", type=int, default=42,
                            help="random seed, for a reproducible dataset (default 42)")
        parser.add_argument("--force", action="store_true",
                            help="delete previously seeded rows, then seed again")
        parser.add_argument("--wipe", action="store_true",
                            help="delete previously seeded rows and exit")

    # ------------------------------------------------------------------ plumbing
    def handle(self, *args, **options):
        self._t0 = time.monotonic()
        self.n_users = options["users"]
        self.n_tx = options["transactions"]
        self.months = options["months"]
        random.seed(options["seed"])

        if self.n_users < 4:
            raise CommandError("--users must be at least 4 (hubs, regulars, dormant)")
        if self.n_tx < 1:
            raise CommandError("--transactions must be at least 1")
        if self.months < 1:
            raise CommandError("--months must be at least 1")

        sentinel = self._sentinel_users()
        if options["wipe"]:
            if not sentinel.exists():
                self.stdout.write("nothing to wipe: no seeded rows found")
                return
            removed = self._wipe(sentinel)
            self.stdout.write("wiped %s in %.1fs" % (removed, self._elapsed()))
            return

        if sentinel.exists():
            if not options["force"]:
                self.stderr.write("seeded data present; use --force or --wipe.")
                raise SystemExit(1)
            removed = self._wipe(sentinel)
            self.stdout.write("--force: removed %s" % removed)

        self.stdout.write(
            "seeding users=%d transactions=%d months=%d seed=%d"
            % (self.n_users, self.n_tx, self.months, options["seed"])
        )

        nodes = self._create_users()
        self._create_kyc(nodes)
        rows = self._build_rows(nodes)
        self._insert_rows(rows, nodes)
        self._set_balances(nodes, rows)

        self.stdout.write(self.style.SUCCESS(
            "seed_demo done in %.1fs -- log in as %s1%s / %s"
            % (self._elapsed(), DEMO_USERNAME_PREFIX, DEMO_EMAIL_DOMAIN, DEMO_PASSWORD)
        ))

    def _elapsed(self):
        return time.monotonic() - self._t0

    def _sentinel_users(self):
        return User.objects.filter(
            Q(email__endswith=DEMO_EMAIL_DOMAIN)
            | Q(username__startswith=DEMO_USERNAME_PREFIX)
        )

    def _wipe(self, sentinel):
        """Delete every seeded user and every transaction that touches one.

        ``Transaction.user``/``sender``/``reciever`` are ``on_delete=SET_NULL``,
        so rows have to be removed explicitly -- deleting the users alone would
        leave orphaned transactions behind pointing at nobody.
        """
        users = list(sentinel)
        pks = [u.pk for u in users]
        with db_transaction.atomic():
            txns = Transaction.objects.filter(
                Q(sender_id__in=pks) | Q(reciever_id__in=pks) | Q(user_id__in=pks)
            )
            n_tx = txns.count()
            txns.delete()
            # Cascades to each user's Account and KYC row.
            n_users = len(users)
            User.objects.filter(pk__in=pks).delete()
        return "%d users and %d transactions" % (n_users, n_tx)

    # -------------------------------------------------------------------- users
    def _create_users(self):
        """Create the users, hub/regular/dormant, and activate their accounts.

        ``create_user`` rather than ``bulk_create`` on purpose: the
        ``post_save`` receiver in ``account/models.py`` provisions exactly one
        Account per new User, and ``bulk_create`` does not dispatch signals --
        bulk-created users would have no Account at all.
        """
        hubs_n, dormant_n = self._split(self.n_users)
        regular_n = self.n_users - hubs_n - dormant_n

        # One real PBKDF2 hash, reused. See the module docstring.
        shared_hash = make_password(DEMO_PASSWORD)

        nodes = []
        with db_transaction.atomic():
            for n in range(1, self.n_users + 1):
                user = User.objects.create_user(
                    username="%s%d" % (DEMO_USERNAME_PREFIX, n),
                    email="user%d%s" % (n, DEMO_EMAIL_DOMAIN),
                )
                user.password = shared_hash
                user.save(update_fields=["password"])

                account = Account.objects.get(user=user)  # created by the signal
                Account.objects.filter(pk=account.pk).update(account_status="active")

                if n <= hubs_n:
                    is_hub, is_dormant = True, False
                elif n <= hubs_n + regular_n:
                    is_hub, is_dormant = False, False
                else:
                    is_hub, is_dormant = False, True

                nodes.append(_Node(n - 1, user.pk, account.pk, is_hub, is_dormant))
                if n % 25 == 0:
                    self.stdout.write("  users %d/%d  (%.1fs)"
                                      % (n, self.n_users, self._elapsed()))

        hubs = [x for x in nodes if x.is_hub]
        regulars = [x for x in nodes if not x.is_hub and not x.is_dormant]
        for regular in regulars:
            regular.hub_idx = regular.idx % len(hubs)
            hubs[regular.hub_idx].clients.append(regular)
        for node in nodes:
            node.hub_idx = node.idx % len(hubs) if not node.is_hub else node.idx

        self.hubs, self.regulars = hubs, regulars
        self.nodes_by_idx = {x.idx: x for x in nodes}
        self.all_nodes = nodes
        self.stdout.write("  users done: %d hubs, %d regulars, %d dormant  (%.1fs)"
                          % (len(hubs), len(regulars), len(nodes) - len(hubs) - len(regulars),
                             self._elapsed()))
        return nodes

    def _split(self, n):
        """Scale the 20/100/30 split for smaller runs."""
        if n >= HUB_COUNT + REGULAR_COUNT + DORMANT_COUNT:
            return HUB_COUNT, DORMANT_COUNT
        hubs = max(1, int(round(n * HUB_COUNT / 150.0)))
        dormant = int(round(n * DORMANT_COUNT / 150.0))
        if hubs + dormant >= n:
            hubs, dormant = 1, 0
        return hubs, dormant

    # ---------------------------------------------------------------------- KYC
    def _create_kyc(self, nodes):
        """File KYC for ``KYC_RATIO`` of the users, hubs first.

        Hubs are always included, which is what guarantees the documented demo
        login has a KYC row -- ``account/views.py::_kyc_required`` redirects
        anyone without one to the KYC form instead of the dashboard. The users
        left without a row can still transact: nothing in ``core/`` gates on KYC
        (Phase 0 recon D.1/D.2).
        """
        target = int(round(len(nodes) * KYC_RATIO))
        hubs = [x for x in nodes if x.is_hub]
        others = [x for x in nodes if not x.is_hub]
        chosen = hubs + random.sample(others, max(0, min(len(others), target - len(hubs))))

        accounts = dict(
            Account.objects.filter(user_id__in=[x.pk for x in chosen])
            .values_list("user_id", "pk")
        )

        kyc_rows = []
        for i, node in enumerate(chosen):
            node.has_kyc = True
            kyc_rows.append(KYC(
                user_id=node.pk,
                account_id=accounts[node.pk],
                full_name="%s %s" % (FIRST_NAMES[node.idx % len(FIRST_NAMES)],
                                     LAST_NAMES[(node.idx // len(FIRST_NAMES)) % len(LAST_NAMES)]),
                nationality=random.choice(COUNTRIES),
                marrital_status=random.choice([k for k, _ in MARRTIAL_STATUS]),
                gender=random.choice([k for k, _ in GENDER]),
                identity_type=random.choice([k for k, _ in IDENTITY_TYPE]),
                date_of_birth=timezone.now() - timedelta(days=random.randint(21 * 365, 65 * 365)),
                signature=DEMO_SIGNATURE,
                country=random.choice(COUNTRIES),
                city=random.choice(CITIES),
                state=random.choice(STATES),
                mobile="+2126%08d" % random.randint(0, 99999999),
                fax="+2125%08d" % random.randint(0, 99999999),
            ))

        with db_transaction.atomic():
            KYC.objects.bulk_create(kyc_rows, batch_size=100)
            # kyc_submitted is otherwise only ever written by the KYC form view;
            # kyc_confirmed is an admin action in the real flow, so it is set for
            # most -- but not all -- of the seeded rows.
            submitted, confirmed = [], []
            for node in chosen:
                if random.random() < KYC_CONFIRMED_RATIO:
                    confirmed.append(node.account_pk)
                else:
                    submitted.append(node.account_pk)
            Account.objects.filter(pk__in=submitted).update(kyc_submitted=True)
            Account.objects.filter(pk__in=confirmed).update(kyc_submitted=True,
                                                           kyc_confirmed=True)
        self.stdout.write("  kyc done: %d rows (%d confirmed)  (%.1fs)"
                          % (len(kyc_rows), len(confirmed), self._elapsed()))

    # --------------------------------------------------------------- the graph
    def _build_graph(self):
        """Small-world edges: dense around hubs, sparse between regulars.

        Hubs draw their partners from regulars only, never from each other. That
        is what makes "each hub transacts with 30-50 others" exact: a hub's
        degree is then precisely the list it chose, because no other hub and no
        regular can add an edge back to it. (With hub-hub edges one hub reached
        53, since twenty hubs each picking ~40 partners must overlap somewhere.)
        Regulars share clients across neighbouring hubs, which is the small-world
        part: a regular's neighbours are its own hub's clients plus those of the
        next two hubs.
        """
        hubs, regulars = self.hubs, self.regulars
        hub_edges, reg_edges = set(), set()

        for hub in hubs:
            own = list(hub.clients)
            own_idx = {c.idx for c in own}
            candidates = []
            for step in (1, 2):  # neighbouring hubs share their clients
                candidates.extend(hubs[(hub.idx + step) % len(hubs)].clients)
            candidates = [c for c in candidates if c.idx not in own_idx]
            candidates.extend(r for r in regulars if r.idx not in own_idx)

            want = max(0, min(random.randint(30, 50) - len(own), len(candidates)))
            for partner in own + random.sample(candidates, want):
                hub_edges.add(_pair(hub, partner))

        for regular in regulars:
            home = hubs[regular.hub_idx]
            pool = [c for c in home.clients if c is not regular]
            for step in (1, 2):
                pool.extend(hubs[(regular.hub_idx + step) % len(hubs)].clients)
            by_idx = {c.idx: c for c in pool if c is not regular}
            candidates = list(by_idx.values())
            if not candidates:
                continue
            for partner in random.sample(candidates, min(len(candidates),
                                                         random.randint(2, 5))):
                reg_edges.add(_pair(regular, partner))

        self.hub_edges = sorted(hub_edges)
        self.reg_edges = sorted(reg_edges)
        self.stdout.write("  graph: %d hub edges, %d regular edges  (%.1fs)"
                          % (len(hub_edges), len(reg_edges), self._elapsed()))

    # -------------------------------------------------------------- the calendar
    def _calendar(self):
        """Candidate days, payday days, and the start of the seeded window."""
        today = timezone.localdate()
        start = today - timedelta(days=self.months * 30)
        days = [start + timedelta(days=i) for i in range((today - start).days + 1)]
        weights = [5.0 if d.weekday() < 5 else 1.0 for d in days]

        paydays = []
        cursor = date(start.year, start.month, 1)
        while cursor <= today:
            last = (date(cursor.year + (cursor.month == 12), (cursor.month % 12) + 1, 1)
                    - timedelta(days=1))
            found = 0
            probe = last
            while found < 2 and probe >= cursor:
                if probe.weekday() < 5 and probe <= today:
                    paydays.append(probe)
                    found += 1
                probe -= timedelta(days=1)
            cursor = last + timedelta(days=1)

        return days, weights, sorted(set(paydays)), start

    def _pick_datetime(self, days, weights, paydays, now):
        """A weekday-biased, business-hours, payday-spiked timestamp."""
        if paydays and random.random() < PAYDAY_SHARE:
            day = random.choice(paydays)
        else:
            day = random.choices(days, weights=weights, k=1)[0]

        if random.random() < BUSINESS_HOURS_SHARE:
            hour = random.randint(8, 17)
        else:
            hour = random.choice(list(range(0, 8)) + list(range(18, 24)))
        stamp = timezone.make_aware(datetime.combine(
            day, clock_time(hour, random.randint(0, 59), random.randint(0, 59))))
        return min(stamp, now - timedelta(minutes=1))

    # ----------------------------------------------------------- the transaction
    def _build_rows(self, nodes):
        """Build every row in memory first: the balances are computed from them."""
        self._build_graph()
        days, weights, paydays, _start = self._calendar()
        now = timezone.now()
        edges = []
        for sender_idx, reciever_idx in self.hub_edges:
            edges.append((sender_idx, reciever_idx, True))
        for sender_idx, reciever_idx in self.reg_edges:
            edges.append((sender_idx, reciever_idx, False))
        if not edges:
            raise CommandError("no graph edges: increase --users")
        self.hub_queue = _EdgeQueue(e for e in edges if e[2])
        self.reg_queue = _EdgeQueue(e for e in edges if not e[2])

        rows = []
        showcase = self._showcase_rows(now)
        if len(showcase) > self.n_tx:
            showcase = showcase[: self.n_tx]
        rows.extend(showcase)
        self.stdout.write("  showcase: %d reserved rows for %s1  (%.1fs)"
                          % (len(showcase), DEMO_USERNAME_PREFIX, self._elapsed()))

        need = max(0, self.n_tx - len(showcase))
        blocks = []
        while len(blocks) < need:
            pool = list(MIX_PER_100)
            random.shuffle(pool)
            blocks.extend(pool)
        mix = blocks[:need]

        for i, (ttype, status) in enumerate(mix):
            a, b = self._next_pair()
            if a.is_hub or b.is_hub:
                amount = _lognormal(LARGE_MEAN, LARGE_SIGMA, *LARGE_RANGE)
            else:
                amount = _lognormal(SMALL_MEAN, SMALL_SIGMA, *SMALL_RANGE)

            rows.append({
                "sender": a,
                "reciever": b,
                "amount": amount,
                "status": status,
                "ttype": ttype,
                "when": self._pick_datetime(days, weights, paydays, now),
            })
            if (i + 1) % 500 == 0:
                self.stdout.write("  transactions built %d/%d  (%.1fs)"
                                  % (i + 1, need, self._elapsed()))

        random.shuffle(rows)
        self.stdout.write("  built %d rows  (%.1fs)" % (len(rows), self._elapsed()))
        return rows

    def _next_pair(self):
        """The next edge, hub-flavoured ``HUB_TRAFFIC_SHARE`` of the time."""
        edge = None
        if random.random() < HUB_TRAFFIC_SHARE:
            edge = self.hub_queue.next()
        if edge is None:
            edge = self.reg_queue.next() or self.hub_queue.next()
        a, b = self.nodes_by_idx[edge[0]], self.nodes_by_idx[edge[1]]
        return (b, a) if random.random() < 0.5 else (a, b)

    def _showcase_rows(self, now):
        """Guarantee the documented demo login has a populated dashboard.

        Eight rows -- three out, three in, one settled request, one in flight --
        all inside the last three weeks, and part of ``--transactions`` rather
        than additional to it. One hub among twenty would otherwise expect well
        under one transaction per month, so ``Received (30d)``/``Sent (30d)``
        could legitimately render zero for the account this command tells you to
        log in as.
        """
        first = self.nodes_by_idx[0]
        hubs = [x for x in self.hubs if x is not first]
        pick = random.sample(self.regulars, min(3, len(self.regulars)))
        rows = []

        def stamp(days_ago):
            day = timezone.localdate() - timedelta(days=days_ago)
            while day.weekday() >= 5:
                day -= timedelta(days=1)
            return timezone.make_aware(datetime.combine(
                day, clock_time(random.randint(8, 17), random.randint(0, 59), 0)))

        for i, other in enumerate(pick):  # money out
            rows.append({"sender": first, "reciever": other, "ttype": "transfer",
                         "status": "completed",
                         "amount": _lognormal(LARGE_MEAN, LARGE_SIGMA, *LARGE_RANGE),
                         "when": stamp(3 + i * 2)})
        for i, other in enumerate(pick):  # money in
            rows.append({"sender": other, "reciever": first, "ttype": "transfer",
                         "status": "completed",
                         "amount": _lognormal(LARGE_MEAN, LARGE_SIGMA, *LARGE_RANGE),
                         "when": stamp(4 + i * 2)})
        rows.append({"sender": first, "reciever": random.choice(hubs or self.regulars),
                     "ttype": "request", "status": "request_settled",
                     "amount": _lognormal(LARGE_MEAN, LARGE_SIGMA, *LARGE_RANGE),
                     "when": stamp(2)})
        rows.append({"sender": first, "reciever": random.choice(self.regulars or self.hubs),
                     "ttype": "transfer", "status": "processing",
                     "amount": _lognormal(SMALL_MEAN, SMALL_SIGMA, *SMALL_RANGE),
                     "when": stamp(1)})
        return rows

    def _insert_rows(self, rows, nodes):
        """Bulk-insert, then back-date.

        ``Transaction.date`` is ``auto_now_add=True``, so Django ignores any
        value passed at creation. The rows therefore land with today's date and
        are corrected afterwards, one ``.update()`` per row inside a single
        transaction -- 3000 autocommitted statements would fsync 3000 times,
        which is the difference between seconds and a minute on this machine.
        """
        objs = []
        when_by_tid = {}
        for row in rows:
            txn = Transaction(
                user_id=row["sender"].pk,
                sender_id=row["sender"].pk,
                reciever_id=row["reciever"].pk,
                sender_account_id=row["sender"].account_pk,
                reciever_account_id=row["reciever"].account_pk,
                amount=row["amount"],
                status=row["status"],
                transaction_type=row["ttype"],
                description=DEMO_DESCRIPTION,
            )
            objs.append(txn)
            when_by_tid[txn.transaction_id] = row["when"]

        with db_transaction.atomic():
            Transaction.objects.bulk_create(objs, batch_size=500)
        self.stdout.write("  inserted %d transactions  (%.1fs)" % (len(objs), self._elapsed()))

        pks = [x.pk for x in nodes]
        stored = list(
            Transaction.objects
            .filter(Q(sender_id__in=pks) | Q(reciever_id__in=pks))
            .values_list("pk", "transaction_id")
        )
        start = time.monotonic()
        with db_transaction.atomic():
            for pk, tid in stored:
                Transaction.objects.filter(pk=pk).update(date=when_by_tid[tid])
        self.stdout.write("  back-dated %d rows in %.1fs  (%.1fs total)"
                          % (len(stored), time.monotonic() - start, self._elapsed()))

    # -------------------------------------------------------------- the balances
    def _set_balances(self, nodes, rows):
        """Replay the views' balance mutation so the ledger-less balance agrees.

        transfer + completed        -> sender pays reciever
        request  + request_settled  -> reciever pays sender  (the requester is ``sender``)
        anything else               -> no money has moved yet
        """
        deltas = {x.pk: Decimal("0.00") for x in nodes}
        for row in rows:
            sender, reciever, amount = row["sender"].pk, row["reciever"].pk, row["amount"]
            if row["ttype"] == "transfer" and row["status"] == "completed":
                deltas[sender] -= amount
                deltas[reciever] += amount
            elif row["ttype"] == "request" and row["status"] == "request_settled":
                deltas[sender] += amount
                deltas[reciever] -= amount

        topped_up = 0
        with db_transaction.atomic():
            for node in nodes:
                opening = Decimal(random.randint(*OPENING_RANGE))
                balance = opening + deltas[node.pk]
                if balance < 0:
                    # Nothing in the application allows a negative balance, so
                    # raise this user's opening balance instead of showing one.
                    opening = -deltas[node.pk] + Decimal(random.randint(*OPENING_RANGE))
                    balance = opening + deltas[node.pk]
                    topped_up += 1
                Account.objects.filter(pk=node.account_pk).update(
                    account_balance=balance.quantize(Decimal("0.01")))
        self.stdout.write("  balances set (%d opening balances topped up)  (%.1fs)"
                          % (topped_up, self._elapsed()))
