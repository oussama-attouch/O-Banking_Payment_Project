from django.db import models

#: Categories offered by the blog index's filter chips. The keys are the values
#: stored on ``BlogPost.category`` and used in the ``?category=`` query string.
BLOG_CATEGORIES = [
    ("fintech", "Fintech"),
    ("finance-tips", "Finance Tips"),
    ("security", "Security"),
    ("budgeting", "Budgeting"),
    ("technology", "Technology"),
    ("business", "Business"),
]

#: The marketing design draws each article's cover as a CSS gradient rather than
#: a photograph. Tabler ships no per-colour gradient utility (its API is
#: ``.bg-gradient`` plus ``.bg-gradient-from-*`` / ``-to-*``), so a post stores a
#: named pair and the template maps it to those classes. Storing the name rather
#: than a raw CSS value keeps the choice editable in the admin.
GRADIENT_CHOICES = [
    ("primary-purple", "Primary → Purple"),
    ("azure-blue", "Azure → Blue"),
    ("teal-cyan", "Teal → Cyan"),
    ("orange-red", "Orange → Red"),
    ("indigo-purple", "Indigo → Purple"),
    ("green-lime", "Green → Lime"),
]


class BlogPost(models.Model):
    """One article on the public blog.

    Content is plain text on purpose: ``body`` is split into paragraphs on blank
    lines by the template, so nothing here depends on a markdown renderer or a
    third-party dependency.
    """

    slug = models.SlugField(max_length=120, unique=True)
    title = models.CharField(max_length=200)
    excerpt = models.CharField(max_length=300, blank=True)
    #: Plain text; the template splits paragraphs on blank lines.
    body = models.TextField()
    category = models.CharField(max_length=30, choices=BLOG_CATEGORIES)
    #: Comma-separated, read back through :meth:`tag_list`.
    tags = models.CharField(max_length=200, blank=True)
    author_name = models.CharField(max_length=100)
    cover_gradient = models.CharField(
        max_length=30, choices=GRADIENT_CHOICES, default="primary-purple"
    )
    #: Selected for the 2-up featured row at the top of the blog index.
    is_featured = models.BooleanField(default=False)
    is_published = models.BooleanField(default=True)
    published_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-published_at"]
        indexes = [models.Index(fields=["-published_at"])]

    def __str__(self):
        return self.title

    def tag_list(self):
        """The ``tags`` field as a list, ignoring empty entries."""
        return [t.strip() for t in self.tags.split(",") if t.strip()]


class ContactMessage(models.Model):
    """A submission from the public contact form.

    Stored rather than emailed: the project has no SMTP configuration at all, so
    Django's default backend would try ``localhost:25`` and fail. Rows are
    visible in the admin, which is what makes the form demonstrable offline.
    """

    name = models.CharField(max_length=100)
    email = models.EmailField()
    phone = models.CharField(max_length=30, blank=True)
    subject = models.CharField(max_length=150)
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    is_read = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} — {self.subject}"
