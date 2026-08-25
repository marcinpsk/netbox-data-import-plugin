# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
import copy

from django import forms
from dcim.models import Site, Location
from tenancy.models import Tenant
from netbox.forms import NetBoxModelBulkEditForm, NetBoxModelForm, NetBoxModelImportForm
from utilities.forms.fields import DynamicModelChoiceField
from .adapters import get_adapter, selectable_adapter_choices
from .catalog import CATALOG
from .models import (
    ImportProfile,
    ColumnMapping,
    ClassRoleMapping,
    DeviceTypeMapping,
    ColumnTransformRule,
    _require_adapter_config_mapping,
    validate_registered_adapter,
)


def _profile_output_kinds(form):
    """Return the output kinds of the profile this row belongs to, or None when unknown."""
    profile = form.initial.get("profile") or form.instance.profile_id
    if form.is_bound:
        profile = form.data.get(form.add_prefix("profile")) or profile
    if not profile:
        return None
    if not isinstance(profile, ImportProfile):
        # A hidden field carries raw POST text, so a non-numeric value must not reach the query.
        try:
            profile = ImportProfile.objects.filter(pk=int(profile)).first()
        except (TypeError, ValueError):
            return None
        if profile is None:
            return None
    return profile.output_kinds


def _with_stored_target(choices, stored, output_kinds, *, allow_candidates=True):
    """Keep a stored key-family target selectable, so an existing row can be re-saved.

    CATALOG.choices lists fixed keys only, so a stored family key such as `extra_json:asset_id` is
    never among them. Re-offer it only when the model would still accept it: the row's clean()
    runs the same check, and offering more would put a choice in the list that saving rejects.
    """
    if not stored or stored in {key for key, _label in choices}:
        return choices
    if not CATALOG.is_valid(stored, output_kinds=output_kinds, allow_candidates=allow_candidates):
        return choices
    return [*choices, (stored, CATALOG.display(stored))]


class ImportProfileForm(NetBoxModelForm):
    """Form for creating and editing ImportProfile instances.

    The Source Adapter is asked for first and is disabled after creation. The selected adapter
    declares the remaining configuration fields.
    """

    class Meta:
        model = ImportProfile
        fields = ["name", "description", "source_adapter", "tags"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._config_form_class = None
        stored = _require_adapter_config_mapping(self.instance.adapter_config)
        adapter = get_adapter(self._selected_adapter_key())
        if adapter is None:
            return
        if self.instance.pk:
            self.fields["source_adapter"].disabled = True
        else:
            self.fields["source_adapter"].choices = selectable_adapter_choices()
        self._config_form_class = adapter.config_form_class()
        for name, field in self._config_form_class.base_fields.items():
            self.fields[name] = copy.deepcopy(field)
            if name in stored:
                self.initial.setdefault(name, stored[name])

    def _selected_adapter_key(self):
        """Return the adapter key this form edits: the stored one, or the submitted choice."""
        if self.instance.pk:
            return self.instance.source_adapter
        if self.is_bound:
            return self.data.get(self.add_prefix("source_adapter")) or self.instance.source_adapter
        return self.instance.source_adapter

    def clean(self):
        """Collect the adapter-declared fields into ``adapter_config``."""
        cleaned = super().clean()
        if cleaned is None:
            cleaned = self.cleaned_data
        if self._config_form_class is not None:
            self.instance.adapter_config = self._config_form_class.normalize(cleaned)
        return cleaned


class ImportProfileImportForm(NetBoxModelImportForm):
    """CSV/YAML bulk-import form for ImportProfile objects (profile metadata only).

    Adapter configuration is nested, so it is set through the edit UI, the REST API, or the
    hierarchical profile YAML import.
    """

    class Meta:
        model = ImportProfile
        fields = ["name", "description", "source_adapter", "tags"]


class ImportProfileBulkEditForm(NetBoxModelBulkEditForm):
    """Bulk-edit fields that apply safely across import profiles.

    The Source Adapter is immutable and ``adapter_config`` is adapter-scoped, so neither is
    bulk-editable.
    """

    model = ImportProfile

    description = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))

    nullable_fields = ("description",)


class ColumnMappingForm(forms.ModelForm):
    """Form for creating and editing ColumnMapping instances."""

    target_field = forms.ChoiceField(choices=CATALOG.choices)

    class Meta:
        model = ColumnMapping
        fields = ["profile", "source_column", "target_field"]
        widgets = {"profile": forms.HiddenInput()}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        output_kinds = _profile_output_kinds(self)
        choices = CATALOG.choices(output_kinds=output_kinds)
        self.fields["target_field"].choices = _with_stored_target(choices, self.instance.target_field, output_kinds)


class ClassRoleMappingForm(forms.ModelForm):
    """Form for creating and editing ClassRoleMapping instances."""

    class Meta:
        model = ClassRoleMapping
        fields = ["profile", "source_class", "creates_rack", "rack_type", "role_slug", "ignore"]
        widgets = {"profile": forms.HiddenInput()}

    def clean(self):
        """Require role_slug unless creates_rack or ignore is set."""
        cleaned = super().clean()
        creates_rack = cleaned.get("creates_rack")
        ignore = cleaned.get("ignore")
        role_slug = (cleaned.get("role_slug") or "").strip()
        if not creates_rack and not ignore and not role_slug:
            self.add_error(
                "role_slug",
                "A device role slug is required unless 'creates rack' or 'ignore' is checked.",
            )
        return cleaned


class DeviceTypeMappingForm(forms.ModelForm):
    """Form for creating and editing DeviceTypeMapping instances."""

    class Meta:
        model = DeviceTypeMapping
        fields = [
            "profile",
            "source_make",
            "source_model",
            "netbox_manufacturer_slug",
            "netbox_device_type_slug",
        ]
        widgets = {"profile": forms.HiddenInput()}


class ColumnTransformRuleForm(forms.ModelForm):
    """Form for creating and editing ColumnTransformRule instances."""

    group_1_target = forms.ChoiceField(required=False)
    group_2_target = forms.ChoiceField(required=False)

    class Meta:
        model = ColumnTransformRule
        fields = ["profile", "source_column", "pattern", "group_1_target", "group_2_target"]
        widgets = {"profile": forms.HiddenInput()}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # A capture group yields text, so the candidate targets are not offered.
        output_kinds = _profile_output_kinds(self)
        choices = CATALOG.choices(output_kinds=output_kinds, allow_candidates=False)
        for name in ("group_1_target", "group_2_target"):
            stored = getattr(self.instance, name, "")
            preserved = _with_stored_target(choices, stored, output_kinds, allow_candidates=False)
            self.fields[name].choices = [("", "---------"), *preserved]


class ImportSetupForm(forms.Form):
    """Form for the import wizard step 1: select profile, upload file, choose site/location/tenant."""

    MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10 MB

    # ImportProfile has no REST API endpoint yet, so use a plain select
    profile = forms.ModelChoiceField(
        queryset=ImportProfile.objects.all(),
        label="Import Profile",
        empty_label="— Select a profile —",
    )
    excel_file = forms.FileField(
        label="Excel File",
        help_text="Upload the Excel file to import (.xlsx, max 10 MB)",
    )
    site = DynamicModelChoiceField(
        queryset=Site.objects.all(),
        label="Target Site",
    )
    location = DynamicModelChoiceField(
        queryset=Location.objects.all(),
        label="Location (optional)",
        required=False,
        query_params={"site_id": "$site"},
    )
    tenant = DynamicModelChoiceField(
        queryset=Tenant.objects.all(),
        label="Tenant (optional)",
        required=False,
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if user is None:
            return
        self.fields["profile"].queryset = ImportProfile.objects.restrict(user, "change")
        self.fields["site"].queryset = Site.objects.restrict(user, "view")
        self.fields["location"].queryset = Location.objects.restrict(user, "view")
        self.fields["tenant"].queryset = Tenant.objects.restrict(user, "view")

    def clean_profile(self):
        """Reject a profile whose stored Source Adapter this release no longer registers."""
        profile = self.cleaned_data["profile"]
        validate_registered_adapter(profile)
        return profile

    def clean_excel_file(self):
        """Reject files that exceed the maximum upload size."""
        f = self.cleaned_data.get("excel_file")
        if f and f.size > self.MAX_UPLOAD_SIZE:
            limit_mb = self.MAX_UPLOAD_SIZE / (1024 * 1024)
            raise forms.ValidationError(
                f"File too large: {f.size / (1024 * 1024):.1f} MB. Maximum allowed is {limit_mb:.0f} MB."
            )
        return f
