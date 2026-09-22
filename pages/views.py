"""Public marketing views: the blog index, an article, and the contact form.

All three are deliberately public -- no ``@login_required`` -- because they are
the pages a visitor sees before signing in. None of them reads banking data.
"""
from django.contrib import messages
from django.core.paginator import Paginator
from django.core.validators import validate_email
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render

from pages.models import BLOG_CATEGORIES, BlogPost, ContactMessage

#: Articles per page on the blog index. The design shows a 3-column grid of six
#: cards, so six is also what fills one grid exactly.
POSTS_PER_PAGE = 6

#: Required contact-form fields, in display order, with the label used in the
#: message shown to the visitor.
REQUIRED_FIELDS = (
    ("name", "Name"),
    ("email", "Email"),
    ("subject", "Subject"),
    ("message", "Message"),
)

CATEGORY_KEYS = {key for key, _ in BLOG_CATEGORIES}


def blog_list(request):
    """The blog index: a featured pair, then a paginated grid.

    ``?category=`` filters the grid. An unknown or missing value is treated as
    "all" and echoed back as an empty string, so a hand-edited query string
    cannot produce an empty page or an error.
    """
    category = (request.GET.get("category") or "").strip()
    if category not in CATEGORY_KEYS:
        category = ""

    published = BlogPost.objects.filter(is_published=True)
    featured = list(published.filter(is_featured=True)[:2])

    posts = published
    if category:
        posts = posts.filter(category=category)
    # The featured pair is rendered above the grid, so it must not repeat in it.
    posts = posts.exclude(pk__in=[post.pk for post in featured])

    page = Paginator(posts, POSTS_PER_PAGE).get_page(request.GET.get("page"))

    context = {
        "featured": featured,
        "posts": page,
        "categories": BLOG_CATEGORIES,
        "active_category": category,
    }
    return render(request, "pages/blog_list.html", context)


def blog_detail(request, slug):
    """A single published article, with related and recent sidebars."""
    post = get_object_or_404(BlogPost, slug=slug, is_published=True)

    related = (
        BlogPost.objects.filter(is_published=True, category=post.category)
        .exclude(pk=post.pk)[:3]
    )
    recent = BlogPost.objects.filter(is_published=True).exclude(pk=post.pk)[:4]

    context = {
        "post": post,
        "related": related,
        "recent": recent,
        "categories": BLOG_CATEGORIES,
    }
    return render(request, "pages/blog_detail.html", context)


def contact(request):
    """The public contact form.

    Validated by hand rather than with a ``ModelForm``: the form is four required
    fields and one optional one, and the submitted values have to survive a
    failed submission so the visitor does not retype them.

    There is no mail configuration in this project, so a valid submission is
    stored as a :class:`~pages.models.ContactMessage` row (visible in the admin)
    instead of being emailed.
    """
    form_data = {field: "" for field, _ in REQUIRED_FIELDS}
    form_data["phone"] = ""
    errors = {}

    if request.method == "POST":
        for field in form_data:
            form_data[field] = (request.POST.get(field) or "").strip()

        for field, label in REQUIRED_FIELDS:
            if not form_data[field]:
                errors[field] = "%s is required." % label

        if "email" not in errors:
            try:
                validate_email(form_data["email"])
            except ValidationError:
                errors["email"] = "Enter a valid email address."

        if not errors:
            ContactMessage.objects.create(**form_data)
            messages.success(request, "Thanks — we'll get back to you.")
            return redirect("pages:contact")

    context = {"errors": errors, "form_data": form_data, "categories": BLOG_CATEGORIES}
    return render(request, "pages/contact.html", context)
