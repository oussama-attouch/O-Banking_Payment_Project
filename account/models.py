from django.core.exceptions import ValidationError
from django.db import models
import uuid
from shortuuid.django_fields import ShortUUIDField
from userauths.models import User  # Importing the User model from another module
from django.db.models.signals import post_save  # Importing a signal for post-save actions
from django.dispatch import receiver

# Function to determine the directory path for user-uploaded files
def user_directory_path(instance, filename):
    ext = filename.split(".")[-1]
    filename = "%s_%s" % (instance.id, ext)  # Create a new filename
    return "user_{0}/{1}".format(instance.user.id, filename)  # Return the path

# Choices for the account status
ACCOUNT_STATUS_CHOICES = [
    ('active', 'Active'),
    ('inactive', 'Inactive'),
    ('in-review', 'In Review'),
]

# Choices for marital status
# NOTE: deliberately a tuple, not a set. A set's iteration order is randomised
# per process by PYTHONHASHSEED, which made makemigrations serialise these
# choices in a different order on every run and produced permanent phantom
# drift against the migration state.
MARRTIAL_STATUS = (
    ("married", "Married"),
    ("single", "Single"),
    ("other", "Other"),
)

# Choices for gender
GENDER = (
    ("male", "Male"),
    ("female", "Female"),
    ("other", "Other"),
)

# Choices for identity types
IDENTITY_TYPE = [
    ("passport", "Passport"),
    ("driver_license", "Driver's License"),
    ("national_id", "National ID"),
]


# Definition of the Account model
class Account(models.Model):
    # Primary key field using UUID, which is automatically generated and not editable
    Id = models.UUIDField(primary_key=True, unique=True, default=uuid.uuid4, editable=False)
    
    # One-to-one relationship with the User model
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    
    # Decimal field for account balance
    account_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    
    # ShortUUIDField for account number, with specified parameters
    account_number = ShortUUIDField(unique=True, length=10, max_length=25, prefix="217", alphabet="1234567890")
    
    # ShortUUIDField for account ID, with specified parameters
    account_id = ShortUUIDField(unique=True, length=7, max_length=25, prefix="DEX", alphabet="1234567890")
    
    
    # ShortUUIDField for a reference code, with speci<fied parameters
    ref_code = ShortUUIDField(unique=True, length=10, max_length=10, alphabet="abcdfgh1234567890")
    
    # Char field for account status, with choices from ACCOUNT_STATUS_CHOICES
    account_status = models.CharField(max_length=100, choices=ACCOUNT_STATUS_CHOICES, default="inactive")
    
    # DateTime field for the creation date, set automatically on creation
    date = models.DateTimeField(auto_now_add=True)
    
    # Boolean field for KYC (Know Your Customer) submission status
    kyc_submitted = models.BooleanField(default=False)
    
    # Boolean field for KYC confirmation status
    kyc_confirmed = models.BooleanField(default=False)
    
    # ForeignKey relationship to the User model for the recommending user
    # recommended_by = models.ForeignKey(User, on_delete=models.DO_NOTHING, blank=True, null=True, related_name="recommended_by")
    
 # Meta class for the model, specifying ordering by date in descending order
    class Meta:
        ordering = ['-date']

 # Define a special method __str__ for the Account model
    def __str__(self):
        # Return a formatted string representation of the Account object
        return f"{self.user}"

# Define the KYC model
class KYC(models.Model):
    # Primary key field using UUID, automatically generated and not editable
    id = models.UUIDField(primary_key=True, unique=True, default=uuid.uuid4, editable=False)
    
    user = models.OneToOneField(User,on_delete=models.CASCADE)

    account = models.OneToOneField(Account,on_delete=models.CASCADE,null=True,blank=True)

    # Field for full name, with maximum length of 1000 characters
    full_name = models.CharField(max_length=1000)
    
    # Field for image upload, stored in the "Kyc" directory, with a default image
    image = models.ImageField(upload_to="kyc", default="default.jpg")
    
    # Field for nationality, with maximum length of 100 characters
    nationality = models.CharField(max_length=100)
    
    # Field for marital status, with choices from the MARTIAL_STATUS defined elsewhere
    marrital_status = models.CharField(choices=MARRTIAL_STATUS, max_length=40)
    
    # Field for gender, with choices from the GENDER defined elsewhere
    gender = models.CharField(choices=GENDER, max_length=40)
    
    # Field for identity type, with choices from the IDENTITY_TYPE (not defined here)
    identity_type = models.CharField(choices=IDENTITY_TYPE, max_length=140)
    
    # Field for date of birth, allowing user input
    date_of_birth = models.DateTimeField(auto_now_add=False)
    
    # Field for signature upload, stored in the "kyc" directory
    signature = models.ImageField(upload_to="kyc")
    
    # Address fields
    country = models.CharField(max_length=100)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=100)
    
    # Contact detail fields
    mobile = models.CharField(max_length=1000)
    fax = models.CharField(max_length=1000)
    
    # DateTime field for the creation date, set automatically on creation
    date = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
    # Return a formatted string representation of the Account object
        return f"{self.user}"
    class Meta:
        ordering = ['-date']


# Account provisioning for new users.
#
# This receiver used to be declared inside the KYC class body (as a method of
# KYC, which was never correct) and was paired with a second receiver:
#
#     def save_account(sender, instance, **kwargs):
#         instance.account.save()
#     post_save.connect(save_account, sender=User)
#
# That second receiver was removed in Phase 1. Accessing ``instance.account``
# caches the Account on the User instance, so on every subsequent User.save()
# -- including the one Django itself performs on every login via
# update_last_login -- the stale cached Account row was written back verbatim.
# Any balance change made through a different Account instance (Django admin,
# a management command, a shell session, a data migration) was silently
# reverted. Nothing needs that receiver: whatever mutates an Account already
# saves it.
#
# Only create_account remains. It is idempotent per new User and is what
# provisions the one-to-one Account that Account.user requires.
@receiver(post_save, sender=User)
def create_account(sender, instance, created, **kwargs):
    if created:
        Account.objects.create(user=instance)
        Category.objects.bulk_create([
            Category(user=instance, name=n, slug=s, icon=i, color=c)
            for n, s, i, c in [
                ("Groceries",     "groceries",     "cart",   "blue"),
                ("Housing",       "housing",       "home",   "purple"),
                ("Transport",     "transport",     "car",    "green"),
                ("Entertainment", "entertainment", "ticket", "orange"),
                ("Utilities",     "utilities",     "bolt",   "red"),
                ("Other",         "other",         "dots",   "gray"),
            ]
        ])


# Define the Recipient model: a saved-payee list, one row per (owner, target
# account) pair. Phase 5f-2 builds the views and templates that use it; this
# phase is the model, its migration, its admin and its tests only.
class Recipient(models.Model):
    """A saved payee. One user saves another account they transact with,
    optionally with a nickname distinct from the target's own name."""

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="recipients",
    )
    target_account = models.ForeignKey(
        Account,
        on_delete=models.CASCADE,
        related_name="saved_by",
    )
    nickname = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "target_account"],
                name="unique_recipient_per_user",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.display_name} → {self.target_account.account_number}"

    @property
    def display_name(self):
        """Nickname if set, else the target's KYC full_name,
        else the target's username."""
        if self.nickname:
            return self.nickname
        kyc = getattr(self.target_account.user, "kyc", None)
        if kyc is not None and kyc.full_name:
            return kyc.full_name
        return self.target_account.user.username

    def save(self, *args, **kwargs):
        # Reject saving yourself as a recipient. This mirrors the
        # self-transfer guard in core/transfer.py::process_amount_transfer.
        # Do NOT raise ValueError — use ValidationError so it surfaces in
        # forms and admin cleanly.
        if self.target_account_id and self.user_id:
            if self.target_account.user_id == self.user_id:
                raise ValidationError("You cannot save your own account as a recipient.")
        super().save(*args, **kwargs)

    def clean(self):
        if self.target_account_id and self.user_id:
            if self.target_account.user_id == self.user_id:
                raise ValidationError({"target_account": "You cannot save your own account as a recipient."})


# User-facing notifications about money events (Phase 5g-1). This phase is the
# model, the signal that fills it, the migration, the admin and the tests only;
# Phase 5g-2 builds the bell dropdown that reads it.
class Notification(models.Model):
    """A short, user-facing message about a money event. Created
    automatically by the signal in account/notifications.py when a
    Transaction transitions into a settled state. Never created
    for rows that fail; failed transactions get a separate kind."""

    KIND_MONEY_IN = "money_in"
    KIND_MONEY_OUT = "money_out"
    KIND_REQUEST = "request"
    KIND_SETTLED = "settled"
    KIND_KYC = "kyc"
    KIND_CHOICES = [
        (KIND_MONEY_IN, "Money in"),
        (KIND_MONEY_OUT, "Money out"),
        (KIND_REQUEST, "Payment request"),
        (KIND_SETTLED, "Settlement"),
        (KIND_KYC, "KYC"),
    ]

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="notifications",
    )
    kind = models.CharField(max_length=20, choices=KIND_CHOICES)
    title = models.CharField(max_length=200)
    body = models.CharField(max_length=400, blank=True)
    link = models.CharField(max_length=300, blank=True)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "-created_at"]),
            models.Index(fields=["user", "is_read"]),
        ]

    def __str__(self):
        return f"{self.get_kind_display()}: {self.title}"


# In-app support tickets (Phase 5h-1). Users open tickets from the app; staff
# reply from the Django admin. No email is sent, so the models are the whole
# backend: Phase 5h-2 adds the views and the template that read them.
class SupportTicket(models.Model):
    """A user-submitted question or problem. Staff reply from the
    admin; the user sees the thread under /account/support/."""

    STATUS_OPEN = "open"
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_RESOLVED = "resolved"
    STATUS_CLOSED = "closed"
    STATUS_CHOICES = [
        (STATUS_OPEN, "Open"),
        (STATUS_IN_PROGRESS, "In progress"),
        (STATUS_RESOLVED, "Resolved"),
        (STATUS_CLOSED, "Closed"),
    ]

    PRIORITY_LOW = "low"
    PRIORITY_NORMAL = "normal"
    PRIORITY_HIGH = "high"
    PRIORITY_CHOICES = [
        (PRIORITY_LOW, "Low"),
        (PRIORITY_NORMAL, "Normal"),
        (PRIORITY_HIGH, "High"),
    ]

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="support_tickets",
    )
    subject = models.CharField(max_length=200)
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_OPEN
    )
    priority = models.CharField(
        max_length=10, choices=PRIORITY_CHOICES, default=PRIORITY_NORMAL
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["user", "-updated_at"]),
            models.Index(fields=["status", "-updated_at"]),
        ]

    def __str__(self):
        return f"#{self.pk} {self.subject[:60]}"

    @property
    def is_open(self):
        return self.status in (self.STATUS_OPEN, self.STATUS_IN_PROGRESS)


class SupportReply(models.Model):
    """One message in a support thread. Author is a User; whether it
    came from staff is derived from author.is_staff at read time,
    not stored, so it cannot drift."""

    ticket = models.ForeignKey(
        SupportTicket,
        on_delete=models.CASCADE,
        related_name="replies",
    )
    author = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="support_replies",
    )
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["ticket", "created_at"]),
        ]

    def __str__(self):
        return f"Reply #{self.pk} on ticket #{self.ticket_id}"

    @property
    def from_staff(self):
        return bool(self.author.is_staff)


class Category(models.Model):
    """A user-defined budget category. Six defaults are created
    for every user at account creation time; users may add or
    remove categories from /account/categories/ (Phase E-2)."""

    ICON_CHOICES = [
        ("cart", "Groceries"),
        ("home", "Housing"),
        ("car", "Transport"),
        ("ticket", "Entertainment"),
        ("bolt", "Utilities"),
        ("dots", "Other"),
    ]
    COLOR_CHOICES = [
        ("blue", "Blue"),
        ("green", "Green"),
        ("orange", "Orange"),
        ("purple", "Purple"),
        ("red", "Red"),
        ("gray", "Gray"),
    ]

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="categories",
    )
    name = models.CharField(max_length=50)
    slug = models.SlugField(max_length=50)
    icon = models.CharField(max_length=20, choices=ICON_CHOICES, default="dots")
    color = models.CharField(max_length=20, choices=COLOR_CHOICES, default="gray")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "slug"],
                name="unique_category_per_user",
            ),
        ]

    def __str__(self):
        return f"{self.user.username} / {self.name}"
