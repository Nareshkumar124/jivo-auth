"""
The "Users with access" picker for a many-to-many field to users.

Every member is a row with a checkbox named after the field, so the form
works without JavaScript: untick to remove, paste emails into the
`<name>_emails` box to add. admin.js adds a search-as-you-type picker
(Django's admin autocomplete endpoint), Remove/Undo buttons, a filter and
an unsaved-changes summary on top of the same inputs.

The field still saves through the many-to-many manager (`set()`), so
`m2m_changed` receivers such as the audit log see every change.
"""

import re

from django import forms
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db.models.functions import Lower
from django.forms.boundfield import BoundField
from django.template.loader import render_to_string
from django.utils.text import get_text_list


EMAIL_SEPARATORS = re.compile(r"[\s,;]+")


def parse_emails(text):
    """Emails from pasted text (lines, commas, semicolons), lowercased, deduplicated."""

    return list(dict.fromkeys(
        email.lower()
        for email in EMAIL_SEPARATORS.split(text or "")
        if email
    ))


def initials(user):
    words = user.get_full_name().split() or [user.email]

    return "".join(word[0] for word in words[:2]).upper()


def describe_members(users, *, checked_ids, initial_ids):
    """
    Rows for the template, sorted by name. `state` is "added" (not in the
    saved list), "removed" (saved, now unticked) or "member".
    """

    rows = []

    for user in users:
        pk = str(user.pk)

        if pk not in initial_ids:
            state = "added"
        elif pk not in checked_ids:
            state = "removed"
        else:
            state = "member"

        name = user.get_full_name()

        rows.append(
            {
                "id": pk,
                "name": name,
                "email": user.email,
                "employee_code": user.employee_code or "",
                "initials": initials(user),
                "is_active": user.is_active,
                "is_verified": user.is_verified,
                "state": state,
                "checked": state != "removed",
                "search": " ".join(filter(None, [name, user.email, user.employee_code])).lower(),
            }
        )

    # Unsaved additions first, then everyone by name (or email).
    rows.sort(key=lambda row: (row["state"] != "added", (row["name"] or row["email"]).lower()))

    return rows


def valid_pks(model, values):
    pks = []

    for value in values:
        try:
            pks.append(model._meta.pk.to_python(value))
        except ValidationError:
            continue

    return pks


def render_access_list(users, *, read_only=True, **context):
    """The member list on its own, e.g. for staff who may only view."""

    ids = {str(user.pk) for user in users}

    return render_to_string(
        UserAccessWidget.template_name,
        {
            "widget": {
                "read_only": read_only,
                "members": describe_members(users, checked_ids=ids, initial_ids=ids),
                "count": len(ids),
                "show_verification": settings.REQUIRE_VERIFIED_EMAIL,
                **context,
            },
        },
    )


class UserAccessWidget(forms.Widget):

    template_name = "adminpanel/widgets/user_access.html"

    def __init__(self, search_url="", can_add=True, attrs=None):
        super().__init__(attrs)

        self.search_url = search_url
        self.can_add = can_add

        # Saved members, set by UserAccessBoundField, so a form re-rendered
        # with errors still shows what is being added and removed.
        self.initial_ids = None

    def value_from_datadict(self, data, files, name):
        ids = data.getlist(name) if hasattr(data, "getlist") else data.get(name) or []

        return {
            "ids": [str(pk) for pk in ids],
            "emails": data.get(f"{name}_emails", ""),
        }

    def value_omitted_from_data(self, data, files, name):
        # Unticking everyone submits nothing for the field: that means "no one".
        return False

    @staticmethod
    def split_value(value):
        if isinstance(value, dict):
            return list(value.get("ids") or []), value.get("emails") or ""

        return [str(pk) for pk in value or []], ""

    def get_context(self, name, value, attrs):
        context = super().get_context(name, value, attrs)

        ids, emails = self.split_value(value)
        initial_ids = set(self.initial_ids if self.initial_ids is not None else ids)
        checked_ids = set(ids)

        User = get_user_model()
        users = User.objects.filter(pk__in=valid_pks(User, checked_ids | initial_ids))

        members = describe_members(users, checked_ids=checked_ids, initial_ids=initial_ids)

        context["widget"].update(
            {
                "read_only": False,
                "members": members,
                "count": sum(row["checked"] for row in members),
                "emails": emails,
                "search_url": self.search_url,
                "can_add": self.can_add,
                "show_verification": settings.REQUIRE_VERIFIED_EMAIL,
            }
        )

        return context


class UserAccessBoundField(BoundField):

    def as_widget(self, widget=None, attrs=None, only_initial=False):
        widget = widget or self.field.widget

        if isinstance(widget, UserAccessWidget):
            widget.initial_ids = [str(pk) for pk in self.field.prepare_value(self.initial) or []]

        return super().as_widget(widget, attrs, only_initial)


class UserAccessField(forms.ModelMultipleChoiceField):
    """
    ModelMultipleChoiceField for UserAccessWidget: the ticked IDs plus any
    emails pasted into the `<name>_emails` box, which must belong to
    existing accounts.
    """

    widget = UserAccessWidget

    default_error_messages = {
        **forms.ModelMultipleChoiceField.default_error_messages,
        "invalid_emails": "These aren't email addresses: %(emails)s.",
        "unknown_emails": "No account has the email %(emails)s.",
    }

    def __init__(self, queryset, *, allow_email_lookup=True, **kwargs):
        super().__init__(queryset, **kwargs)

        self.allow_email_lookup = allow_email_lookup

    def get_bound_field(self, form, field_name):
        return UserAccessBoundField(form, self, field_name)

    def prepare_value(self, value):
        if isinstance(value, dict):
            return value

        return super().prepare_value(value)

    def clean(self, value):
        ids, emails_text = UserAccessWidget.split_value(value)
        emails = parse_emails(emails_text) if self.allow_email_lookup else []

        if emails:
            invalid = []

            for email in emails:
                try:
                    validate_email(email)
                except ValidationError:
                    invalid.append(email)

            if invalid:
                raise ValidationError(
                    self.error_messages["invalid_emails"],
                    code="invalid_emails",
                    params={"emails": get_text_list(invalid, "and")},
                )

            found = {
                user.email_lower: user.pk
                for user in self.queryset.annotate(email_lower=Lower("email")).filter(email_lower__in=emails)
            }

            unknown = [email for email in emails if email not in found]

            if unknown:
                raise ValidationError(
                    self.error_messages["unknown_emails"],
                    code="unknown_emails",
                    params={"emails": get_text_list(unknown, "or")},
                )

            ids = list(dict.fromkeys([*ids, *(str(pk) for pk in found.values())]))

        return super().clean(ids)

    def has_changed(self, initial, data):
        ids, emails = UserAccessWidget.split_value(data)

        if self.allow_email_lookup and parse_emails(emails):
            return True

        return super().has_changed(initial, ids)
