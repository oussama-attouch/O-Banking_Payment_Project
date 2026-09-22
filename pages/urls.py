from django.urls import path

from pages import views

app_name = "pages"

urlpatterns = [
    path("blog/", views.blog_list, name="blog_list"),
    path("blog/<slug:slug>/", views.blog_detail, name="blog_detail"),
    path("contact/", views.contact, name="contact"),
]
