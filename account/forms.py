from django import forms 
from django.contrib.auth.forms import PasswordChangeForm
from account.models import Category, KYC, SavingsGoal
from django.forms import ImageField, FileInput, DateInput
from userauths.models import User

class DateInput(forms.DateInput):
    input_type = 'date'

class KYCForm(forms.ModelForm):
    image = ImageField(widget=FileInput)
    signature = ImageField(widget=FileInput)

    class Meta:
        model = KYC
        fields = [ 'full_name', 'image', 'nationality', 'marrital_status', 'gender', 'identity_type', 'date_of_birth', 'signature', 'country', 'state', 'city', 'mobile', 'fax']
        widgets = {
            "full_name": forms.TextInput(attrs={"placeholder":"Full Name"}),
            "nationality": forms.TextInput(attrs={"placeholder":"Nationality"}),
            "mobile": forms.TextInput(attrs={"placeholder":"Mobile Number"}),
            "fax": forms.TextInput(attrs={"placeholder":"Fax Number"}),
            "country": forms.TextInput(attrs={"placeholder":"Country"}),
            "state": forms.TextInput(attrs={"placeholder":"State"}),
            "city": forms.TextInput(attrs={"placeholder":"City"}),
            'date_of_birth':DateInput
        }


class ProfileForm(forms.ModelForm):
    """The three User columns a signed-in user may edit for themselves.

    ``email`` is ``USERNAME_FIELD`` on ``userauths.User`` and the column already
    carries ``unique=True``, so a clash with another account is caught by the
    ModelForm's own uniqueness check -- nothing extra is validated here.

    Deliberately no password fields: changing a password goes through
    ``StyledPasswordChangeForm`` below, which demands the current password.
    """

    class Meta:
        model = User
        fields = ["first_name", "last_name", "email"]
        # No widget attrs: Django already picks TextInput for the two CharFields
        # and EmailInput for the EmailField, and the ``form-control`` class is
        # applied in the template with the project's ``add_class`` filter, which
        # is how KYCForm and the sign-up form are styled. Declaring the class in
        # both places would emit ``class="form-control form-control"``, because
        # the filter appends rather than replaces.


class StyledPasswordChangeForm(PasswordChangeForm):
    """Django's ``PasswordChangeForm`` with Tabler's ``form-control`` applied.

    ``PasswordChangeForm`` is a plain ``Form``, not a ``ModelForm``, so it has no
    ``Meta.widgets`` to declare the class in. Setting it here keeps the template
    free of per-field filter calls and keeps every widget in step if Django ever
    adds a field to the form.

    Everything else -- checking the current password, running the configured
    validators, refusing a reused password -- is Django's own behaviour.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "form-control")

    def save(self, commit=True):
        user = super().save(commit=commit)
        # The thread-local request is set by AuditContextMiddleware,
        # so audit.log() can resolve the actor without threading the
        # request through. Importing here avoids a module-level
        # dependency from account into audit for a single method.
        from audit.utils import log
        log("password_changed", target=user)
        return user


class RecipientForm(forms.Form):
    """Save a payee by account number.

    A plain ``Form``, not a ``ModelForm``: the user types an account number (or
    account ID) string, and the view is what turns it into an ``Account`` row.
    Every rule that needs the database -- self-save, duplicates, the 50-per-user
    cap -- belongs to the view, which can say *why* it refused; this form only
    normalises the two strings.
    """

    account_number = forms.CharField(
        max_length=25,
        widget=forms.TextInput(attrs={
            "class": "form-control",
            "placeholder": "Account number or ID",
            "autocomplete": "off",
        }),
        label="Account number or ID",
        help_text="The 217... account number or DEX... account ID.",
    )
    nickname = forms.CharField(
        max_length=100,
        required=False,
        widget=forms.TextInput(attrs={
            "class": "form-control",
            "placeholder": "Optional — e.g. 'Alice (work)'",
            "autocomplete": "off",
        }),
        label="Nickname",
        help_text="If left blank, the account holder's name is used.",
    )

    def clean_account_number(self):
        value = (self.cleaned_data.get("account_number") or "").strip()
        if not value:
            raise forms.ValidationError("Please enter an account number.")
        return value


class SupportTicketForm(forms.Form):
    """Open a ticket: a subject plus the first message of the thread.

    A plain ``Form``, not a ``ModelForm``: the subject becomes
    ``SupportTicket.subject`` and the message becomes the first
    ``SupportReply``, so one form writes two models and the view owns that.
    ``status`` and ``priority`` are deliberately absent -- only staff set
    them, through the admin.
    """

    subject = forms.CharField(
        max_length=200,
        widget=forms.TextInput(attrs={
            "class": "form-control",
            "placeholder": "Short summary of the issue",
            "autocomplete": "off",
        }),
    )
    message = forms.CharField(
        widget=forms.Textarea(attrs={
            "class": "form-control",
            "rows": 5,
            "placeholder": "Describe what happened.",
        }),
        label="Your message",
    )


class SupportReplyForm(forms.Form):
    """One message appended to an existing thread."""

    body = forms.CharField(
        widget=forms.Textarea(attrs={
            "class": "form-control",
            "rows": 4,
            "placeholder": "Add a reply…",
        }),
        label="Reply",
    )


class CategoryForm(forms.ModelForm):
    """Add a budget category.

    ``slug`` is deliberately absent: the view derives it from the name, so the
    user cannot create two categories whose names differ only in punctuation.
    Uniqueness is not checked here either -- the model's
    ``unique_category_per_user`` constraint is the single source of truth, and
    the view turns the resulting ``IntegrityError`` into a field error.
    """

    class Meta:
        model = Category
        fields = ["name", "icon", "color"]
        widgets = {
            "name": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "e.g. Restaurants, Travel, Savings",
                "autocomplete": "off",
            }),
            "icon": forms.Select(attrs={"class": "form-select"}),
            "color": forms.Select(attrs={"class": "form-select"}),
        }

    def clean_name(self):
        return (self.cleaned_data.get("name") or "").strip()


class SavingsGoalForm(forms.ModelForm):
    """Create a savings goal: a name, a target, and an optional date.

    ``current_amount`` and ``is_completed`` are deliberately absent. A goal
    starts at zero -- the model's default -- and only ``account:goal_add``
    moves it, so the create form cannot open a goal that already claims to
    be part-funded.
    """

    class Meta:
        model = SavingsGoal
        fields = ["name", "target_amount", "deadline"]
        widgets = {
            "name": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "e.g. Emergency fund, Japan trip",
                "autocomplete": "off",
            }),
            "target_amount": forms.NumberInput(attrs={
                "class": "form-control",
                "min": "0.01", "step": "0.01",
                "placeholder": "0.00",
            }),
            "deadline": forms.DateInput(attrs={
                "class": "form-control", "type": "date",
            }),
        }

    def clean_target_amount(self):
        v = self.cleaned_data.get("target_amount")
        if v is None or v <= 0:
            raise forms.ValidationError(
                "Target must be greater than zero."
            )
        return v

    def clean_deadline(self):
        d = self.cleaned_data.get("deadline")
        # deadline is optional; no validation beyond the widget.
        return d


class AddToGoalForm(forms.Form):
    """Record a contribution to an existing goal.

    A plain ``Form``, not a ``ModelForm``: the view owns which goal is
    credited, so the only input here is the amount. ``min_value=0`` is a
    floor, not the rule -- ``clean_amount`` rejects zero as well, because a
    contribution of nothing is not a contribution.
    """

    amount = forms.DecimalField(
        max_digits=12, decimal_places=2, min_value=0,
        widget=forms.NumberInput(attrs={
            "class": "form-control form-control-sm",
            "min": "0", "step": "0.01", "placeholder": "0.00",
        }),
    )

    def clean_amount(self):
        v = self.cleaned_data.get("amount")
        if v is None or v <= 0:
            raise forms.ValidationError("Amount must be greater than zero.")
        return v
