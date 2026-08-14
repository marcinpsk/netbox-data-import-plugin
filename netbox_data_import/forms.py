# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
from django import forms
from dcim.models import Site, Location
from tenancy.models import ContactRole, Tenant
from netbox.forms import NetBoxModelBulkEditForm, NetBoxModelForm, NetBoxModelImportForm
from utilities.forms.fields import DynamicModelChoiceField
from utilities.forms.widgets import BulkEditNullBooleanSelect
from .models import ImportProfile, ColumnMapping, ClassRoleMapping, DeviceTypeMapping, ColumnTransformRule
from .models import (
    COLUMN_TRANSFORM_TARGET_FIELD_CHOICES,
    CONTACT_LOOKUP_FIELD_CHOICES,
    PREVIEW_VIEW_CHOICES,
    TARGET_FIELD_CHOICES,
)


class ImportProfileForm(NetBoxModelForm):
    """Form for creating and editing ImportProfile instances."""

    class Meta:
        model = ImportProfile
        fields = [
            "name",
            "description",
            "sheet_name",
            "source_id_column",
            "custom_field_name",
            "update_existing",
            "create_missing_device_types",
            "preview_view_mode",
            "capture_extra_data",
            "primary_contact_role",
            "primary_contact_lookup_field",
            "tags",
        ]


class ImportProfileImportForm(NetBoxModelImportForm):
    """CSV/YAML bulk-import form for ImportProfile objects (profile metadata only)."""

    primary_contact_lookup_field = forms.ChoiceField(
        choices=CONTACT_LOOKUP_FIELD_CHOICES,
        required=False,
    )

    def clean_primary_contact_lookup_field(self):
        """Use email matching when an older CSV omits the new column."""
        return self.cleaned_data["primary_contact_lookup_field"] or "email"

    class Meta:
        model = ImportProfile
        fields = [
            "name",
            "description",
            "sheet_name",
            "source_id_column",
            "custom_field_name",
            "update_existing",
            "create_missing_device_types",
            "preview_view_mode",
            "capture_extra_data",
            "primary_contact_role",
            "primary_contact_lookup_field",
            "tags",
        ]


class ImportProfileBulkEditForm(NetBoxModelBulkEditForm):
    """Bulk-edit fields that apply safely across import profiles."""

    model = ImportProfile

    description = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))
    sheet_name = forms.CharField(max_length=100, required=False)
    source_id_column = forms.CharField(max_length=100, required=False)
    custom_field_name = forms.CharField(max_length=100, required=False)
    update_existing = forms.NullBooleanField(required=False, widget=BulkEditNullBooleanSelect())
    create_missing_device_types = forms.NullBooleanField(required=False, widget=BulkEditNullBooleanSelect())
    preview_view_mode = forms.ChoiceField(
        choices=[("", "---------"), *PREVIEW_VIEW_CHOICES],
        required=False,
    )
    capture_extra_data = forms.NullBooleanField(required=False, widget=BulkEditNullBooleanSelect())
    primary_contact_role = DynamicModelChoiceField(queryset=ContactRole.objects.all(), required=False)
    primary_contact_lookup_field = forms.ChoiceField(
        choices=[("", "---------"), *CONTACT_LOOKUP_FIELD_CHOICES],
        required=False,
    )

    nullable_fields = ("description", "source_id_column", "custom_field_name", "primary_contact_role")


class ColumnMappingForm(forms.ModelForm):
    """Form for creating and editing ColumnMapping instances."""

    target_field = forms.ChoiceField(choices=TARGET_FIELD_CHOICES)

    class Meta:
        model = ColumnMapping
        fields = ["profile", "source_column", "target_field"]
        widgets = {"profile": forms.HiddenInput()}


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

    group_1_target = forms.ChoiceField(
        choices=[("", "---------")] + COLUMN_TRANSFORM_TARGET_FIELD_CHOICES,
        required=False,
    )
    group_2_target = forms.ChoiceField(
        choices=[("", "---------")] + COLUMN_TRANSFORM_TARGET_FIELD_CHOICES,
        required=False,
    )

    class Meta:
        model = ColumnTransformRule
        fields = ["profile", "source_column", "pattern", "group_1_target", "group_2_target"]
        widgets = {"profile": forms.HiddenInput()}


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

    def clean_excel_file(self):
        """Reject files that exceed the maximum upload size."""
        f = self.cleaned_data.get("excel_file")
        if f and f.size > self.MAX_UPLOAD_SIZE:
            limit_mb = self.MAX_UPLOAD_SIZE / (1024 * 1024)
            raise forms.ValidationError(
                f"File too large: {f.size / (1024 * 1024):.1f} MB. Maximum allowed is {limit_mb:.0f} MB."
            )
        return f
