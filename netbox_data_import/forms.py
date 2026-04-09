# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
from django import forms
from dcim.models import Site, Location
from tenancy.models import Tenant
from netbox.forms import NetBoxModelForm
from utilities.forms.fields import DynamicModelChoiceField
from .models import ImportProfile, ColumnMapping, ClassRoleMapping, DeviceTypeMapping, ColumnTransformRule


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
            "tags",
        ]


class ColumnMappingForm(forms.ModelForm):
    """Form for creating and editing ColumnMapping instances."""

    class Meta:
        model = ColumnMapping
        fields = ["profile", "source_column", "target_field"]
        widgets = {"profile": forms.HiddenInput()}


class ClassRoleMappingForm(forms.ModelForm):
    """Form for creating and editing ClassRoleMapping instances."""

    class Meta:
        model = ClassRoleMapping
        fields = ["profile", "source_class", "creates_rack", "role_slug", "ignore"]
        widgets = {"profile": forms.HiddenInput()}


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

    def clean_excel_file(self):
        """Reject files that exceed the maximum upload size."""
        f = self.cleaned_data.get("excel_file")
        if f and f.size > self.MAX_UPLOAD_SIZE:
            limit_mb = self.MAX_UPLOAD_SIZE / (1024 * 1024)
            raise forms.ValidationError(
                f"File too large: {f.size / (1024 * 1024):.1f} MB. Maximum allowed is {limit_mb:.0f} MB."
            )
        return f
