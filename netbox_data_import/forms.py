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
    CableClassMapping,
    ImportProfile,
    ColumnMapping,
    ClassRoleMapping,
    DeviceTypeMapping,
    ColumnTransformRule,
    _require_adapter_config_mapping,
    validate_registered_adapter,
    cable_type_choices,
    compatible_cable_profile_choices,
)

_EXPLICIT_NONE = "__explicit_none__"


class _RuntimeCableChoiceField(forms.ChoiceField):
    """Render only current choices while letting shared validation classify submitted values."""

    def validate(self, value):
        """Apply required-field validation without duplicating the runtime choice rule."""
        forms.Field.validate(self, value)


def _decision_choices(runtime_choices):
    """Add unresolved and explicit-none form states to current NetBox choices."""
    if _EXPLICIT_NONE in {value for value, _label in runtime_choices}:
        raise RuntimeError("A NetBox Cable choice conflicts with the form's explicit-none control value.")
    return [("", "Unresolved"), (_EXPLICIT_NONE, "Explicitly none"), *runtime_choices]


def _with_stored_decision(choices, resolved, value):
    """Keep a stored decision selectable after NetBox stops offering it.

    A select cannot send back a value it does not list, so the browser would submit the first
    option and `clean()` would record the loss as an unresolved decision without an error.
    """
    if not resolved or value is None or value in {key for key, _label in choices}:
        return choices
    return [*choices, (value, f"{value} (no longer offered)")]


def _decision_initial(resolved, value):
    """Return the form value for one stored tri-state decision."""
    if not resolved:
        return ""
    return _EXPLICIT_NONE if value is None else value


def _decode_decision(value):
    """Return the resolved flag and nullable stored value for one form selection."""
    if value in (None, ""):
        return False, None
    if value == _EXPLICIT_NONE:
        return True, None
    return True, value


def _profile_output_kinds(form):
    """Return the output kinds of the profile this row belongs to, or None when unknown."""
    profile = form.instance.profile_id or form.initial.get("profile")
    if not profile:
        return None
    if not isinstance(profile, ImportProfile):
        # NetBox seeds form initial from the query string, so a non-numeric value must not reach the query.
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
        fields = ["source_column", "target_field"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        output_kinds = _profile_output_kinds(self)
        choices = CATALOG.choices(output_kinds=output_kinds)
        self.fields["target_field"].choices = _with_stored_target(choices, self.instance.target_field, output_kinds)


class ClassRoleMappingForm(forms.ModelForm):
    """Form for creating and editing ClassRoleMapping instances."""

    class Meta:
        model = ClassRoleMapping
        fields = ["source_class", "creates_rack", "rack_type", "role_slug", "ignore"]

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


class CableClassMappingForm(forms.ModelForm):
    """Form for one source CableClass and its two independent target decisions."""

    cable_type = _RuntimeCableChoiceField(required=False)
    cable_profile = _RuntimeCableChoiceField(required=False)

    class Meta:
        model = CableClassMapping
        fields = ["cable_class", "cable_type", "cable_profile"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["cable_type"].choices = _with_stored_decision(
            _decision_choices(cable_type_choices()),
            self.instance.cable_type_resolved,
            self.instance.cable_type,
        )
        self.fields["cable_profile"].choices = _with_stored_decision(
            _decision_choices(compatible_cable_profile_choices()),
            self.instance.cable_profile_resolved,
            self.instance.cable_profile,
        )
        self.initial["cable_type"] = _decision_initial(
            self.instance.cable_type_resolved,
            self.instance.cable_type,
        )
        self.initial["cable_profile"] = _decision_initial(
            self.instance.cable_profile_resolved,
            self.instance.cable_profile,
        )

    def clean(self):
        """Decode each tri-state value and apply the shared runtime-choice validation."""
        cleaned = super().clean()
        type_resolved, cable_type = _decode_decision(cleaned.get("cable_type"))
        profile_resolved, cable_profile = _decode_decision(cleaned.get("cable_profile"))
        self.instance.cable_type_resolved = type_resolved
        self.instance.cable_type = cable_type
        self.instance.cable_profile_resolved = profile_resolved
        self.instance.cable_profile = cable_profile
        cleaned["cable_type"] = cable_type
        cleaned["cable_profile"] = cable_profile
        return cleaned


class DeviceTypeMappingForm(forms.ModelForm):
    """Form for creating and editing DeviceTypeMapping instances."""

    class Meta:
        model = DeviceTypeMapping
        fields = [
            "source_make",
            "source_model",
            "netbox_manufacturer_slug",
            "netbox_device_type_slug",
        ]


class ColumnTransformRuleForm(forms.ModelForm):
    """Form for creating and editing ColumnTransformRule instances."""

    group_1_target = forms.ChoiceField(required=False)
    group_2_target = forms.ChoiceField(required=False)

    class Meta:
        model = ColumnTransformRule
        fields = ["source_column", "pattern", "group_1_target", "group_2_target"]

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
