"""Tests for the public marketing pages: blog index, article, contact form."""
from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from pages.models import BlogPost, ContactMessage


def make_post(**overrides):
    """A published post with sane defaults, overridable per test."""
    defaults = {
        "slug": "a-post",
        "title": "A Post",
        "excerpt": "An excerpt.",
        "body": "First paragraph.\n\nSecond paragraph.",
        "category": "fintech",
        "tags": "alpha, beta",
        "author_name": "A. Writer",
        "published_at": timezone.now() - timedelta(days=1),
    }
    defaults.update(overrides)
    return BlogPost.objects.create(**defaults)


class BlogTests(TestCase):
    def test_blog_list_renders_200(self):
        make_post()
        response = self.client.get(reverse("pages:blog_list"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "pages/blog_list.html")

    def test_blog_list_filters_by_category(self):
        make_post(slug="fintech-one", title="Fintech One", category="fintech")
        make_post(slug="security-one", title="Security One", category="security")

        response = self.client.get(reverse("pages:blog_list"), {"category": "fintech"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([p.title for p in response.context["posts"]], ["Fintech One"])
        self.assertEqual(response.context["active_category"], "fintech")

        # An unknown key is treated as "all" rather than producing an empty page.
        response = self.client.get(reverse("pages:blog_list"), {"category": "nonsense"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            sorted(p.title for p in response.context["posts"]),
            ["Fintech One", "Security One"],
        )
        self.assertEqual(response.context["active_category"], "")

    def test_blog_list_excludes_unpublished(self):
        make_post(slug="live", title="Live Post")
        make_post(slug="draft", title="Draft Post", is_published=False)

        response = self.client.get(reverse("pages:blog_list"))
        self.assertEqual(response.status_code, 200)
        titles = [p.title for p in response.context["posts"]]
        self.assertIn("Live Post", titles)
        self.assertNotIn("Draft Post", titles)
        self.assertNotContains(response, "Draft Post")

    def test_blog_list_featured_respects_category_filter(self):
        """The featured row is filtered by ?category=, and the grid is not."""
        fintech_featured = make_post(
            slug="fintech-featured", title="Fintech Featured",
            category="fintech", is_featured=True,
        )
        make_post(
            slug="security-featured", title="Security Featured",
            category="security", is_featured=True,
        )
        fintech_plain = make_post(
            slug="fintech-plain", title="Fintech Plain", category="fintech",
        )

        response = self.client.get(reverse("pages:blog_list"), {"category": "fintech"})
        self.assertEqual(response.status_code, 200)

        featured_titles = [p.title for p in response.context["featured"]]
        self.assertEqual(featured_titles, ["Fintech Featured"])
        self.assertNotIn("Security Featured", featured_titles)

        # With a category active the grid is the complete list for that
        # category, so the featured post stays in it rather than vanishing.
        grid_titles = sorted(p.title for p in response.context["posts"])
        self.assertEqual(grid_titles, ["Fintech Featured", "Fintech Plain"])
        self.assertIn(fintech_featured.pk, [p.pk for p in response.context["posts"]])
        self.assertIn(fintech_plain.pk, [p.pk for p in response.context["posts"]])

        # Unfiltered, the featured pair is still removed from the grid.
        unfiltered = self.client.get(reverse("pages:blog_list"))
        self.assertEqual(
            sorted(p.title for p in unfiltered.context["posts"]), ["Fintech Plain"]
        )

    def test_blog_detail_renders_200(self):
        post = make_post(slug="detail-me", title="Detail Me")
        response = self.client.get(reverse("pages:blog_detail", args=[post.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["post"], post)
        self.assertContains(response, "Detail Me")

    def test_blog_detail_404_for_unpublished(self):
        post = make_post(slug="hidden", is_published=False)
        response = self.client.get(reverse("pages:blog_detail", args=[post.slug]))
        self.assertEqual(response.status_code, 404)

    def test_blog_detail_404_for_missing_slug(self):
        response = self.client.get(reverse("pages:blog_detail", args=["no-such-post"]))
        self.assertEqual(response.status_code, 404)


class ContactTests(TestCase):
    VALID = {
        "name": "Amina Khelifi",
        "email": "amina@example.com",
        "phone": "+212600000000",
        "subject": "Question about the demo",
        "message": "Does this project move real money?",
    }

    def test_contact_get_renders_200(self):
        response = self.client.get(reverse("pages:contact"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "pages/contact.html")
        self.assertEqual(ContactMessage.objects.count(), 0)

    def test_contact_post_creates_row(self):
        response = self.client.post(reverse("pages:contact"), self.VALID)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("pages:contact"))
        self.assertEqual(ContactMessage.objects.count(), 1)
        stored = ContactMessage.objects.get()
        self.assertEqual(stored.name, self.VALID["name"])
        self.assertEqual(stored.email, self.VALID["email"])
        self.assertEqual(stored.phone, self.VALID["phone"])
        self.assertEqual(stored.subject, self.VALID["subject"])
        self.assertEqual(stored.message, self.VALID["message"])
        self.assertFalse(stored.is_read)

    def test_contact_post_missing_name_is_rejected_and_preserves_input(self):
        submitted = dict(self.VALID, name="")
        response = self.client.post(reverse("pages:contact"), submitted)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ContactMessage.objects.count(), 0)
        self.assertIn("name", response.context["errors"])
        # Everything the visitor typed survives the failed submission.
        self.assertEqual(response.context["form_data"]["subject"], self.VALID["subject"])
        self.assertEqual(response.context["form_data"]["message"], self.VALID["message"])
        self.assertEqual(response.context["form_data"]["email"], self.VALID["email"])
        self.assertEqual(response.context["form_data"]["name"], "")

    def test_contact_post_invalid_email_is_rejected(self):
        submitted = dict(self.VALID, email="not-an-email")
        response = self.client.post(reverse("pages:contact"), submitted)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ContactMessage.objects.count(), 0)
        self.assertIn("email", response.context["errors"])
        self.assertEqual(response.context["form_data"]["email"], "not-an-email")

    def test_contact_post_rejects_overlong_name(self):
        """A name past the model's max_length is refused, not stored truncated.

        SQLite does not enforce varchar lengths, so this is the only thing
        standing between an over-long value and the database.
        """
        submitted = dict(self.VALID, name="A" * 200)
        response = self.client.post(reverse("pages:contact"), submitted)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ContactMessage.objects.count(), 0)
        self.assertIn("name", response.context["errors"])
        self.assertIn("too long", response.context["errors"]["name"])
        # The over-long value comes back so the visitor can shorten it.
        self.assertEqual(response.context["form_data"]["name"], "A" * 200)
        self.assertContains(response, "too long")
