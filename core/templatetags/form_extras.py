"""Project-local template filters.

Deliberately tiny. Django renders form widgets with no ``class`` attribute, and
core Django ships no ``add_class`` filter, so applying Tabler's ``form-control``
/ ``form-select`` styling to an auto-rendered field needs either widget
``attrs`` in Python or this filter in the template.

Load it in a template with ``{% load form_extras %}``.
"""
from django import template

register = template.Library()


@register.filter(name="add_class")
def add_class(field, css_class):
    """Add a CSS class to a Django form field widget. Used to apply
    Tabler form-control / form-select styling to auto-rendered fields.

    Appends rather than replaces, so a widget that already declares classes in
    ``Meta.widgets`` keeps them::

        {{ form.full_name|add_class:"form-control" }}
    """
    existing = field.field.widget.attrs.get("class", "")
    field.field.widget.attrs["class"] = f"{existing} {css_class}".strip()
    return field
