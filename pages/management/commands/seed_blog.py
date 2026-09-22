"""Seed the nine blog articles the marketing design shows.

    manage.py seed_blog [--force | --wipe]

The copy is written to match the project's actual posture rather than a
commercial bank's: O-Banking is a final-year coursework demo, it is not
connected to any payment network, and no real money moves. Several articles say
so outright. The product-style titles are kept because they are what the design
calls for, but nothing here claims real customers, real partnerships or real
funds.

Idempotent: refuses to run when any post already exists, unless ``--force``
(deletes every post, then reseeds) or ``--wipe`` (deletes every post and exits).
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from pages.models import BlogPost

#: ``days_ago`` spaces the posts roughly twenty days apart across six months.
#: The two featured posts take the two most recent slots.
POSTS = [
    {
        "slug": "the-future-of-fintech-trends-for-2025",
        "title": "The Future of Fintech: Trends for 2025",
        "category": "fintech",
        "author_name": "Sarah Winter",
        "cover_gradient": "primary-purple",
        "is_featured": True,
        "days_ago": 5,
        "tags": "fintech, payments, open banking, trends",
        "excerpt": (
            "Open banking, instant settlement and programmable limits are reshaping how "
            "payments move — and how a teaching project has to model them."
        ),
        "body": """Fintech rarely changes all at once. Most of the shifts that matter arrive as plumbing: a new API, a shorter settlement window, a stricter identity rule. The headline products follow years later, once the plumbing is boring enough that nobody has to think about it.

Open banking is the clearest example. When account information becomes readable through a standard interface, the interesting question stops being "can we see the balance" and becomes "who is allowed to see it, for how long, and what happens when they are wrong". Consent, revocation and audit trails turn out to be the hard parts, not the connection.

Instant settlement is the second thread. When a payment confirms in seconds rather than days, the window in which a transaction can be reversed nearly disappears. That pushes fraud checks, balance checks and limit checks to the moment of authorisation, because there is no longer a quiet period in which a mistake can be caught and unwound.

Programmable limits are the third. Rather than a single balance with a single overdraft rule, accounts increasingly carry their own constraints: a monthly ceiling, a permitted merchant set, a rule that says this pot may only be spent on rent. Each rule is easy to describe and surprisingly fiddly to enforce consistently across every route that can move money.

For a teaching project, the useful move is to model the boring parts honestly rather than the exciting ones. A demo that shows a transfer being confirmed twice, or a settlement that can be replayed, teaches more about real payment systems than a screen full of animated charts. O-Banking is a coursework project, so that is the trade it makes: two-step confirmations, explicit statuses, and money that only ever moves between simulated accounts.

What to watch over the next year is therefore less about new apps and more about which of these three threads becomes invisible. When a technology stops being a feature and becomes an assumption, that is usually the point at which it has actually arrived.""",
    },
    {
        "slug": "why-security-matters-in-digital-payments",
        "title": "Why Security Matters in Digital Payments",
        "category": "security",
        "author_name": "Taylor Waltz",
        "cover_gradient": "azure-blue",
        "is_featured": True,
        "days_ago": 25,
        "tags": "security, authentication, authorization, replay attacks",
        "excerpt": (
            "Authentication, authorization and replay protection are the three checks that "
            "decide whether a payment system is trustworthy."
        ),
        "body": """Payment software attracts attention because the thing it moves is valuable and, in most systems, at least partly reversible. A flaw that would be an annoyance in a to-do app is a direct loss here, which is why the security questions in payments are unusually concrete.

Authentication answers "who is asking". Passwords alone are weak, but even a weak factor is much stronger when it is demanded at the right moment. In O-Banking a transfer is not settled by the form that creates it: the transfer is created in a processing state, and a second screen asks for the password again before any balance changes. That second gate is deliberate, because it separates "I started this" from "I confirm this".

Authorization answers "is this yours". The most common serious bug in this class of application is not a broken password — it is a missing scope. A view that fetches a record by its identifier and renders it will happily show a stranger's data unless the query itself is restricted to the requesting user. O-Banking expresses that restriction inside the query, so an unrelated identifier matches nothing at all and cannot be probed for existence.

Replay protection answers "did this already happen". Any state-changing URL is a candidate for being submitted twice, whether by an impatient double-click, a browser Back button, or on purpose. If the handler only writes a status and never checks it, the second submission applies the change again. The fix is unglamorous: re-read the row under a lock, verify it is still in the state that permits the operation, and only then mutate.

Least privilege is the quiet fourth check. An administrative interface that lets a staff user edit balances in a list view is convenient during development and dangerous in production, because a single mis-click is indistinguishable from a legitimate correction.

None of this makes a project audited or safe to use with real money. O-Banking is coursework: it has no external security review, no penetration test and no operational monitoring. The value of getting these four checks right in a demo is that they are the same four checks that matter in the real thing.""",
    },
    {
        "slug": "5-ways-to-improve-your-financial-habits",
        "title": "5 Ways to Improve Your Financial Habits",
        "category": "finance-tips",
        "author_name": "Amina Khelifi",
        "cover_gradient": "teal-cyan",
        "days_ago": 45,
        "tags": "personal finance, habits, saving, budgeting",
        "excerpt": (
            "Small, repeatable behaviours beat dramatic resolutions. These five take minutes "
            "to set up and compound over a year."
        ),
        "body": """Most financial advice fails for the same reason most fitness advice fails: it asks for a permanent change in behaviour on the strength of a temporary burst of enthusiasm. The five habits below are chosen because they are cheap to start and hard to get wrong.

First, measure before you optimise. A single month of recorded spending is worth more than any budgeting framework, because it replaces assumptions with facts. Most people are wrong about at least one category, and it is rarely the one they expect.

Second, automate the boring part. A standing transfer on payday turns saving from a monthly decision into a monthly non-event. The amount matters far less than the fact that it happens without requiring willpower.

Third, separate the money by purpose. One account holding rent, groceries, holidays and emergencies invites quiet borrowing from the future. Splitting even a single "do not touch" pot makes the trade-off visible at the moment of spending rather than at the end of the month.

Fourth, review on a schedule rather than continuously. Checking a balance every day mostly produces anxiety; checking it once a month produces decisions. Put the review in the calendar and ignore the app in between.

Fifth, build a buffer before optimising returns. An emergency fund is not an investment, it is insurance against having to borrow at the worst possible moment. Until it exists, every other decision is being made under pressure.

None of these require a product, a subscription or an adviser. They require a few minutes of setup and the willingness to look at the numbers honestly — which, conveniently, is exactly what the dashboard in a project like this one is for.""",
    },
    {
        "slug": "master-your-budget-in-3-simple-steps",
        "title": "Master Your Budget in 3 Simple Steps",
        "category": "budgeting",
        "author_name": "Youssef Alaoui",
        "cover_gradient": "green-lime",
        "days_ago": 65,
        "tags": "budgeting, planning, saving, cash flow",
        "excerpt": (
            "A budget that survives contact with real life has three parts: a baseline, a "
            "decision, and a review."
        ),
        "body": """Budgets fail when they are treated as a document rather than a loop. A spreadsheet that is accurate in January and ignored by March is worse than no spreadsheet at all, because it creates the illusion of control. Three steps, repeated, work better than thirty columns.

Step one is the baseline. Write down what actually leaves the account each month, split into fixed commitments and variable spending. This is descriptive, not aspirational, and it is the step people skip because the answer is uncomfortable.

Step two is the decision. Given that baseline, choose what changes. The common mistake is to change everything at once, which produces a plan that is theoretically balanced and practically impossible. Changing one or two lines is enough for a first pass, and it is far more likely to survive.

Step three is the review. Once a month, compare what happened with what was decided, and adjust. The purpose is not to assign blame but to correct the estimate. A budget that is revised twelve times a year is working exactly as intended.

The loop matters more than the numbers because income and costs both move. A job change, a move, a repair bill or a new subscription all invalidate last quarter's assumptions. A budget that cannot absorb those events is not a plan, it is a snapshot.

For anyone building a tool rather than a spreadsheet, the design lesson is the same. Showing a single balance tells the user almost nothing; showing the flow over time, with the categories that moved, is what makes the review step possible. That is why the dashboard in this project leads with trends and a status breakdown rather than one large number.""",
    },
    {
        "slug": "how-ai-is-revolutionizing-personal-finance",
        "title": "How AI Is Revolutionizing Personal Finance",
        "category": "technology",
        "author_name": "Salma Bouzid",
        "cover_gradient": "indigo-purple",
        "days_ago": 85,
        "tags": "technology, machine learning, fraud detection, forecasting",
        "excerpt": (
            "Categorisation, anomaly detection and forecasting are the three places machine "
            "learning genuinely earns its keep in finance."
        ),
        "body": """The word "revolution" is doing a lot of work in most articles with this title. In practice, machine learning has changed a small number of financial tasks a great deal, and everything else only slightly. It is worth separating the two.

Transaction categorisation is the clearest success. Turning a stream of cryptic merchant strings into meaningful groups is a text-classification problem with plenty of labelled history, and modern models handle it well. The result is unglamorous and genuinely useful: budgets that fill themselves in.

Anomaly detection is the second. Fraud models look for the transaction that does not resemble the account's usual pattern — an unusual merchant, an unusual amount, an unusual hour. The hard part is not the model but the trade-off: every additional catch also blocks some legitimate payments, and the cost of a false positive is a frustrated customer.

Forecasting is the third and the least reliable. Predicting next month's cash flow from history works reasonably well when income is regular and much worse when it is not. The output is best presented as a range with visible assumptions, not as a single confident line.

Conversational interfaces have made all three more accessible, and also easier to over-trust. A model that explains its reasoning in fluent prose can be confidently wrong, and the fluency makes the error harder to spot rather than easier.

The risks are the familiar ones: historical bias encoded as prediction, opaque decisions that cannot be appealed, and data collected for one purpose reused for another. In a financial context each of these has a real cost for a real person.

O-Banking contains no machine learning at all, and says so. Its analytics are plain aggregates over a simulated dataset. That is a deliberate choice for a coursework project: an honest SQL query that a reader can verify is more useful for learning than a model whose behaviour nobody can explain.""",
    },
    {
        "slug": "the-ultimate-guide-to-smart-financial-planning",
        "title": "The Ultimate Guide to Smart Financial Planning",
        "category": "finance-tips",
        "author_name": "Omar Benali",
        "cover_gradient": "orange-red",
        "days_ago": 105,
        "tags": "financial planning, goals, emergency fund, risk",
        "excerpt": (
            "Planning is mostly the discipline of writing down a number, a date and a "
            "constraint — then checking them again in six months."
        ),
        "body": """Financial planning has a reputation for complexity because the industry sells complex products. The underlying exercise is simple: decide what you want, by when, and what you are willing to give up to get it. Everything else is detail.

Start with a number and a date. "Save more" is not a goal; "hold three months of expenses by next December" is, because it can be checked. A goal that cannot be checked cannot be adjusted, and a goal that cannot be adjusted will be abandoned.

Then match the horizon to the instrument. Money needed within a year or two should not be exposed to the kind of volatility that only pays off over a decade. This single rule prevents more damage than any amount of forecasting.

Build the buffer before the plan. An emergency fund converts a crisis from a debt event into an inconvenience. Until it exists, every other objective is being pursued on borrowed time.

Diversification is the next principle, and it is better understood as an admission of ignorance than as a strategy: since nobody reliably knows which single asset will do best, holding several is a way of not having to. It does not remove risk, it spreads it.

Then set a review cadence and keep it boring. Plans go stale because circumstances change, not because the original reasoning was wrong. An annual review with a mid-year check catches almost everything.

One caveat, stated plainly: this is general educational material written for a coursework project, not personalised financial advice. It does not account for your income, your obligations or your jurisdiction, and it should not be treated as a recommendation.""",
    },
    {
        "slug": "scaling-your-business-with-digital-payments",
        "title": "Scaling Your Business with Digital Payments",
        "category": "business",
        "author_name": "Leila Mansouri",
        "cover_gradient": "azure-blue",
        "days_ago": 125,
        "tags": "business, payments, reconciliation, cash flow",
        "excerpt": (
            "The bottleneck in small-business payments is rarely accepting money — it is "
            "knowing which payment belongs to which invoice."
        ),
        "body": """Most small businesses adopt digital payments expecting the hard part to be the checkout. In practice the checkout is solved: a card reader or a payment link takes an afternoon to set up. The difficulty arrives afterwards, in reconciliation.

Reconciliation is the work of matching what arrived in the bank with what was invoiced. When every payment carries a reference, this is a query. When payments arrive as anonymous transfers with a name and an amount, it is detective work, and it scales badly. The single highest-value change most small operators can make is insisting that every payment carries an identifier.

Settlement timing matters almost as much. Money that clears in two days and money that clears in two hours produce very different cash-flow behaviour for a business paying suppliers weekly. Faster settlement does not increase revenue, but it does reduce the amount of working capital parked in transit.

Accepting more methods increases conversion and increases complexity. Each additional route — card, transfer, wallet, cash — adds its own fees and its own reconciliation format. The right number is the smallest set that covers the customers actually being served, not the largest set that can be technically supported.

Beyond a certain volume, the ledger becomes the constraint. A balance that is updated in place tells you what you have and nothing about how you got there, which makes disputes hard to resolve and audits expensive. An append-only record of movements is more work to build and far cheaper to live with.

The honest caveat for a project like this one: O-Banking models none of the commercial layer. There are no fees, no multi-currency support and no acquirer integration. What it does model is the part that is usually hand-waved — a two-step confirmation, an explicit status on every movement, and a balance that is derived from the same history the dashboard displays.""",
    },
    {
        "slug": "how-o-banking-helped-small-businesses-thrive",
        "title": "How O-Banking Helped Small Businesses Thrive",
        "category": "business",
        "author_name": "Karim Idrissi",
        "cover_gradient": "primary-purple",
        "days_ago": 145,
        "tags": "business, case study, demo data, transparency",
        "excerpt": (
            "A title like this usually introduces paying customers. O-Banking has none, and "
            "this article explains what the demo's simulated data does show instead."
        ),
        "body": """A headline of this shape normally promises a success story with real customers in it. O-Banking cannot offer one. It is a final-year coursework project: it holds no real money, it is connected to no payment network, and every account in it belongs to a simulation. Saying that plainly is more useful than inventing a case study.

What the project does contain is a generated dataset that behaves like a small economy. One hundred and fifty simulated accounts move three thousand simulated payments across two years, with a handful of high-volume accounts acting as businesses and a long tail of occasional users around them. Thirty accounts never transact at all, which is realistic and also useful, because it gives the interface an empty state to render.

That dataset exists to exercise the parts of a payment system that a single happy-path transfer never touches. Large and small amounts, settled and pending movements, requests that are raised and later settled, accounts with identity documents on file and accounts without. The dashboard has to show a truthful summary of all of it, from an aggregate over rows rather than a number stored somewhere and quietly trusted.

What a small business would actually care about is visible in that summary: what came in, what went out, what is still in flight, and who the movement was with. Those four questions are the same ones a real operator asks, which is why they are the first four figures on the dashboard.

What is missing is equally worth naming. There is no ledger, so balances are derived by replaying the same mutation the views perform rather than by summing an immutable journal. There is no fee model, no multi-currency support, no dispute flow and no regulatory posture of any kind.

For a portfolio project, being explicit about the boundary is the point. A demo that claims to be a bank invites questions it cannot answer; a demo that documents exactly what it simulates invites a conversation about how the real thing is built.""",
    },
    {
        "slug": "understanding-settlement-flows-in-modern-banking",
        "title": "Understanding Settlement Flows in Modern Banking",
        "category": "fintech",
        "author_name": "Nadia Chraibi",
        "cover_gradient": "teal-cyan",
        "days_ago": 165,
        "tags": "fintech, settlement, clearing, idempotency",
        "excerpt": (
            "Authorisation, clearing and settlement are three different events. Most "
            "confusion about payments comes from treating them as one."
        ),
        "body": """The word "payment" hides three separate things. Authorisation is the promise: the payer's bank agrees the money may move. Clearing is the accounting: the two institutions agree on what is owed. Settlement is the movement: value actually changes hands between them. A card tap that confirms in a second may take days to settle.

The delay exists because settlement between institutions is a batch process with its own rules, cut-off times and netting arrangements. Individual payments are aggregated and settled in bulk, which is efficient and also why a reversal after authorisation is possible at all — between the promise and the movement there is a window.

Inside a single institution the same three stages appear in miniature. A transfer is created in a pending or processing state, then confirmed, then completed. The important property is that each stage is recorded explicitly, so the system can always answer which of the three has happened. A single boolean called "paid" cannot.

The two-step pattern shows up in this project for that reason. A transfer is written in a processing state, and settlement happens on a second screen that re-checks the balance under a lock and refuses to run twice. A payment request runs the same way: it is raised, then settled. The transaction list therefore shows movements that are genuinely in flight rather than pretending everything is instantaneous.

Idempotency is the discipline that makes this safe. Any operation that changes state may arrive twice — through a retry, a double-click, or a deliberate replay — and the second arrival must be a no-op rather than a second movement. The usual implementation is to re-read the record inside the same database transaction that performs the change and verify it is still in the state that permits the operation.

Reading a transaction list is easier once the stages are separated. A pending movement is not a failed one, a settled request moved money in the opposite direction to a transfer, and a completed transfer is not reversible by re-submitting the confirmation screen. Those distinctions are the difference between a list of numbers and a record of what actually happened.""",
    },
]


class Command(BaseCommand):
    help = "Seed the nine blog articles used by the marketing design."

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true",
                            help="delete existing posts, then seed again")
        parser.add_argument("--wipe", action="store_true",
                            help="delete existing posts and exit")

    def handle(self, *args, **options):
        existing = BlogPost.objects.count()

        if options["wipe"]:
            if not existing:
                self.stdout.write("nothing to wipe: no blog posts found")
                return
            with transaction.atomic():
                BlogPost.objects.all().delete()
            self.stdout.write("wiped %d blog posts" % existing)
            return

        if existing:
            if not options["force"]:
                self.stderr.write("blog posts present; use --force or --wipe.")
                raise SystemExit(1)
            with transaction.atomic():
                BlogPost.objects.all().delete()
            self.stdout.write("--force: removed %d blog posts" % existing)

        now = timezone.now()
        created = []
        with transaction.atomic():
            for post in POSTS:
                data = dict(post)
                days_ago = data.pop("days_ago")
                created.append(BlogPost.objects.create(
                    published_at=now - timedelta(days=days_ago), **data
                ))

        self.stdout.write(self.style.SUCCESS(
            "seed_blog done: %d posts (%d featured, %d categories, %s .. %s)"
            % (
                len(created),
                sum(1 for p in created if p.is_featured),
                len({p.category for p in created}),
                min(p.published_at for p in created).date(),
                max(p.published_at for p in created).date(),
            )
        ))
