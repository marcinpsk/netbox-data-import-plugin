# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
from django import forms
from dcim.models import Site, Location
from tenancy.models import Tenant
from netbox.forms import NetBoxModelForm
from utilities.forms.fields import DynamicModelChoiceField
from .models import ImportProfile, ColumnMapping, ClassRoleMapping, DeviceTypeMapping


class ImportProfileForm(NetBoxModelForm):
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
            "tags",
        ]


class ColumnMappingForm(forms.ModelForm):
    class Meta:
        model = ColumnMapping
        fields = ["profile", "source_column", "target_field"]
        widgets = {"profile": forms.HiddenInput()}


class ClassRoleMappingForm(forms.ModelForm):
    class Meta:
        model = ClassRoleMapping
        fields = ["profile", "source_class", "creates_rack", "role_slug"]
        widgets = {"profile": forms.HiddenInput()}


class DeviceTypeMappingForm(forms.ModelForm):
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


class ImportSetupForm(forms.Form):
    profile = DynamicModelChoiceField(
        queryset=ImportProfile.objects.all(),
        label="Import Profile",
    )
    excel_file = forms.FileField(
        label="Excel File",
        help_text="Upload the Excel file to import (.xlsx)",
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
