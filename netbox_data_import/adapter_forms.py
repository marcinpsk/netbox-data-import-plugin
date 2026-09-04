# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Adapter-declared configuration forms.

The selected Source Adapter declares the form that validates ``ImportProfile.adapter_config`` at the
boundary. Unknown keys are rejected. Object references use a natural key, never a database id, so a
profile exported as YAML imports into a different NetBox instance.
"""

from __future__ import annotations

from django import forms
from django.core.exceptions import ValidationError

PREVIEW_VIEW_CHOICES = [
    ("rows", "Row view"),
    ("racks", "Rack view"),
]

CONTACT_LOOKUP_FIELD_CHOICES = [
    ("email", "Email address"),
    ("name", "Name"),
]


# Approved exception to the NetBox base-class rule: this validates adapter configuration rather
# than a model, and NetBox ships no generic non-model form base.
class AdapterConfigForm(forms.Form):
    """Base form for an adapter's scalar settings."""

    @classmethod
    def defaults(cls) -> dict:
        """Return the declared default for every key, so an absent key never falls back silently."""
        return {name: field.initial for name, field in cls.base_fields.items()}

    @classmethod
    def validate_config(cls, raw: dict | None) -> dict:
        """Return the normalized configuration for *raw*, rejecting unknown keys and invalid values."""
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise ValidationError({"adapter_config": "Adapter configuration must be a mapping."})
        unknown = sorted(set(raw) - set(cls.base_fields))
        if unknown:
            raise ValidationError({"adapter_config": f"Unknown adapter configuration key(s): {', '.join(unknown)}."})
        data = cls.defaults()
        data.update(raw)
        form = cls(data=data)
        if not form.is_valid():
            messages = "; ".join(f"{name}: {' '.join(errors)}" for name, errors in form.errors.items())
            raise ValidationError({"adapter_config": messages})
        return form.to_config()

    @classmethod
    def normalize(cls, cleaned: dict) -> dict:
        """Return the stored configuration mapping for one form's cleaned data."""
        return {name: cleaned.get(name) for name in cls.base_fields}

    def to_config(self) -> dict:
        """Return the cleaned data as the stored configuration mapping."""
        return self.normalize(self.cleaned_data)


class _ContactRoleNameField(forms.ModelChoiceField):
    """Reference a Contact Role by its name, so the stored value carries no instance-local id."""

    def __init__(self, **kwargs):
        from tenancy.models import ContactRole

        kwargs.setdefault("queryset", ContactRole.objects.all())
        kwargs.setdefault("to_field_name", "name")
        kwargs.setdefault("required", False)
        super().__init__(**kwargs)


class FlatWorkbookConfigForm(AdapterConfigForm):
    """Settings for the flat-workbook adapter."""

    sheet_name = forms.CharField(
        max_length=100,
        initial="Data",
        help_text="Name of the Excel worksheet to read",
    )
    source_id_column = forms.CharField(
        max_length=100,
        required=False,
        initial="",
        help_text="Column whose value is stored in a NetBox custom field (e.g. 'Id')",
    )
    custom_field_name = forms.CharField(
        max_length=100,
        required=False,
        initial="",
        help_text="NetBox custom field name to store the source ID in (e.g. 'cans_id')",
    )
    update_existing = forms.BooleanField(
        required=False,
        initial=True,
        help_text="Update existing NetBox objects when a match is found",
    )
    capture_extra_data = forms.BooleanField(
        required=False,
        initial=False,
        help_text="Store unmapped source column values in the import record the plugin keeps for each device.",
    )
    primary_contact_role = _ContactRoleNameField(
        initial=None,
        help_text="Contact role to assign when a source row contains a primary contact.",
    )
    primary_contact_lookup_field = forms.ChoiceField(
        choices=CONTACT_LOOKUP_FIELD_CHOICES,
        initial="email",
        help_text="Contact field used to match primary contact values from the source.",
    )
    preview_view_mode = forms.ChoiceField(
        choices=PREVIEW_VIEW_CHOICES,
        initial="rows",
        help_text="How to display the import preview (row table or rack diagrams)",
    )

    @classmethod
    def normalize(cls, cleaned: dict) -> dict:
        """Return the configuration with the Contact Role reduced to its natural key."""
        config = super().normalize(cleaned)
        role = config.get("primary_contact_role")
        config["primary_contact_role"] = role.name if role is not None else None
        return config


class TraceWorkbookConfigForm(AdapterConfigForm):
    """The trace-workbook adapter declares no settings; its sheet names are fixed."""


__all__ = (
    "CONTACT_LOOKUP_FIELD_CHOICES",
    "PREVIEW_VIEW_CHOICES",
    "AdapterConfigForm",
    "FlatWorkbookConfigForm",
    "TraceWorkbookConfigForm",
)
