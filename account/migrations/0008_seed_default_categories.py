"""Create the six default budget categories for every existing user.

New users get them from the ``create_account`` post_save receiver in
``account.models``; this migration is the other half, for the accounts that
already existed when the feature landed.

``bulk_create`` normally validates the ``unique_category_per_user`` constraint
only at the ORM layer, but the database enforces it too: on a database that
already has some of these rows -- code deployed with the receiver before this
migration ran, or a re-applied migration -- a plain ``bulk_create`` dies with
``UNIQUE constraint failed``. ``ignore_conflicts=True`` (``INSERT OR IGNORE``)
is what actually makes the backfill idempotent, which is the point of a
defaults backfill.
"""
from django.db import migrations

DEFAULTS = [
    ("Groceries",     "groceries",     "cart",   "blue"),
    ("Housing",       "housing",       "home",   "purple"),
    ("Transport",     "transport",     "car",    "green"),
    ("Entertainment", "entertainment", "ticket", "orange"),
    ("Utilities",     "utilities",     "bolt",   "red"),
    ("Other",         "other",         "dots",   "gray"),
]


def create_defaults(apps, schema_editor):
    User = apps.get_model("userauths", "User")
    Category = apps.get_model("account", "Category")
    to_create = []
    for user_id in User.objects.values_list("id", flat=True).iterator():
        for name, slug, icon, color in DEFAULTS:
            to_create.append(Category(
                user_id=user_id, name=name, slug=slug,
                icon=icon, color=color,
            ))
    Category.objects.bulk_create(to_create, batch_size=500, ignore_conflicts=True)


def remove_defaults(apps, schema_editor):
    # Reverse: delete every category. That would also SET_NULL
    # every transaction's category FK, which is the intent of
    # a reverse migration -- the feature is being removed.
    Category = apps.get_model("account", "Category")
    Category.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("account", "0007_category"),
    ]

    operations = [
        migrations.RunPython(create_defaults, remove_defaults),
    ]
