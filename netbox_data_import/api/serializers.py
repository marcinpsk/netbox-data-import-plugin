# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""DRF serializers for the data-import plugin models."""

from netbox.api.serializers import NetBoxModelSerializer
from rest_framework import serializers

from ..models import (
    ImportProfile,
    ColumnMapping,
    ClassRoleMapping,
    DeviceTypeMapping,
    IgnoredDevice,
    ColumnTransformRule,
    SourceResolution,
    ImportJob,
)


class ImportProfileSerializer(NetBoxModelSerializer):
    """Full serializer for ImportProfile (NetBoxModel)."""

    url = serializers.HyperlinkedIdentityField(
        view_name="plugins-api:netbox_data_import-api:importprofile-detail",
    )

    class Meta:
        model = ImportProfile
        fields = [
            "id",
            "url",
            "display",
            "name",
            "description",
            "sheet_name",
            "source_id_column",
            "custom_field_name",
            "update_existing",
            "create_missing_device_types",
            "preview_view_mode",
            "tags",
            "custom_fields",
            "created",
            "last_updated",
        ]


class ColumnMappingSerializer(serializers.ModelSerializer):
    """Serializer for ColumnMapping (plain model)."""

    class Meta:
        model = ColumnMapping
        fields = ["id", "profile", "source_column", "target_field"]


class ClassRoleMappingSerializer(serializers.ModelSerializer):
    """Serializer for ClassRoleMapping (plain model)."""

    class Meta:
        model = ClassRoleMapping
        fields = ["id", "profile", "source_class", "creates_rack", "rack_type", "role_slug", "ignore"]


class DeviceTypeMappingSerializer(serializers.ModelSerializer):
    """Serializer for DeviceTypeMapping (plain model)."""

    class Meta:
        model = DeviceTypeMapping
        fields = [
            "id",
            "profile",
            "source_make",
            "source_model",
            "netbox_manufacturer_slug",
            "netbox_device_type_slug",
        ]


class IgnoredDeviceSerializer(serializers.ModelSerializer):
    """Serializer for IgnoredDevice (plain model)."""

    class Meta:
        model = IgnoredDevice
        fields = ["id", "profile", "source_id", "device_name"]


class ColumnTransformRuleSerializer(serializers.ModelSerializer):
    """Serializer for ColumnTransformRule (plain model)."""

    class Meta:
        model = ColumnTransformRule
        fields = [
            "id",
            "profile",
            "source_column",
            "pattern",
            "group_1_target",
            "group_2_target",
        ]


class SourceResolutionSerializer(serializers.ModelSerializer):
    """Serializer for SourceResolution (rerere, plain model)."""

    class Meta:
        model = SourceResolution
        fields = [
            "id",
            "profile",
            "source_id",
            "source_column",
            "original_value",
            "resolved_fields",
        ]


class ImportJobSerializer(serializers.ModelSerializer):
    """Read-only serializer for ImportJob (plain model)."""

    class Meta:
        model = ImportJob
        fields = [
            "id",
            "profile",
            "created",
            "input_filename",
            "dry_run",
            "site_name",
            "result_counts",
        ]
        read_only_fields = fields
