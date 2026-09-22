"""Public marketing views: the blog index, an article, and the contact form.

All three are deliberately public -- no ``@login_required`` -- because they are
the pages a visitor sees before signing in. None of them reads banking data.

Two presentation concerns live here rather than on the model, because
``pages/models.py`` is outside this phase's file scope:

* ``gradient_classes`` -- ``BlogPost.cover_gradient`` stores a *name* from
  ``GRADIENT_CHOICES``; the mapping to Tabler utilities is a CSS concern, so it
  is attached to each instance as a plain Python attribute by
  :func:`_with_gradient` (no model field, no migration).
* ``paragraphs`` -- ``body`` is plain text with blank lines between paragraphs.
  Django templates cannot call ``body.split("\\n\\n")``, so the split happens in
  :func:`_paragraphs` and the result is passed in the context.
"""
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.core.validators import validate_email
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

#: Length caps for the contact form. They mirror the model's ``max_length``
#: values. SQLite does not enforce ``varchar`` lengths, so without this check an
#: over-long value would be stored silently rather than rejected.
FIELD_LIMITS = {
    "name": 100,
    "email": 254,
    "phone": 30,
    "subject": 150,
    "message": 5000,
}

CATEGORY_KEYS = {key for key, _ in BLOG_CATEGORIES}

#: ``BlogPost.cover_gradient`` name -> the two Tabler utilities that paint it.
#: Tabler ships no per-colour gradient utility; ``.bg-gradient`` reads the
#: ``--tblr-gradient-from`` / ``--tblr-gradient-to`` variables that these
#: ``.bg-gradient-from-*`` / ``.bg-gradient-to-*`` classes set.
GRADIENT_CLASSES = {
    "primary-purple": "bg-gradient-from-primary bg-gradient-to-purple",
    "azure-blue": "bg-gradient-from-azure bg-gradient-to-blue",
    "teal-cyan": "bg-gradient-from-teal bg-gradient-to-cyan",
    "orange-red": "bg-gradient-from-orange bg-gradient-to-red",
    "indigo-purple": "bg-gradient-from-indigo bg-gradient-to-purple",
    "green-lime": "bg-gradient-from-green bg-gradient-to-lime",
}
DEFAULT_GRADIENT_CLASSES = GRADIENT_CLASSES["primary-purple"]


def _with_gradient(posts):
    """Attach ``gradient_classes`` to each post and return the same iterable.

    A plain instance attribute, not a model field: nothing is written to the
    database and ``makemigrations`` stays clean.
    """
    for post in posts:
        post.gradient_classes = GRADIENT_CLASSES.get(
            post.cover_gradient, DEFAULT_GRADIENT_CLASSES
        )
    return posts


def _paragraphs(body):
    """Split a plain-text article body on blank lines, dropping empty chunks."""
    return [chunk.strip() for chunk in (body or "").split("\n\n") if chunk.strip()]


def blog_list(request):
    """The blog index: a featured pair, then a paginated grid.

    ``?category=`` filters both the featured row and the grid. An unknown or
    missing value is treated as "all" and echoed back as an empty string, so a
    hand-edited query string cannot produce an empty page or an error.
    """
    category = (request.GET.get("category") or "").strip()
    if category not in CATEGORY_KEYS:
        category = ""

    featured_qs = BlogPost.objects.filter(is_published=True, is_featured=True)
    if category:
        featured_qs = featured_qs.filter(category=category)
    featured = _with_gradient(list(featured_qs[:2]))

    # With no category active the featured pair is rendered above the grid, so
    # it must not repeat inside it. With a category active the grid is the
    # complete list for that category, and the featured row is a subset of it --
    # excluding there would make a post disappear from the only list showing it.
    grid_exclude = [post.pk for post in featured] if not category else []

    posts_qs = BlogPost.objects.filter(is_published=True)
    if category:
        posts_qs = posts_qs.filter(category=category)
    posts_qs = posts_qs.exclude(pk__in=grid_exclude).order_by("-published_at")
    page = Paginator(posts_qs, POSTS_PER_PAGE).get_page(request.GET.get("page"))
    _with_gradient(page.object_list)

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
    _with_gradient([post])

    related = _with_gradient(
        list(
            BlogPost.objects.filter(is_published=True, category=post.category).exclude(
                pk=post.pk
            )[:3]
        )
    )
    recent = list(
        BlogPost.objects.filter(is_published=True).exclude(pk=post.pk)[:4]
    )

    context = {
        "post": post,
        "related": related,
        "recent": recent,
        "categories": BLOG_CATEGORIES,
        "paragraphs": _paragraphs(post.body),
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

        # Length first: an over-long value is rejected outright rather than
        # being truncated or stored, because SQLite would happily store it.
        for field, limit in FIELD_LIMITS.items():
            if len(form_data[field]) > limit:
                errors[field] = "This field is too long (max %d characters)." % limit

        for field, label in REQUIRED_FIELDS:
            if not form_data[field]:
                errors.setdefault(field, "%s is required." % label)

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
