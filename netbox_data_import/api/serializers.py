# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""DRF serializers for the data-import plugin models."""

import json

from django.core.exceptions import ValidationError
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
    validate_contact_candidate_resolution,
)


class ImportProfileSerializer(NetBoxModelSerializer):
    """Full serializer for ImportProfile (NetBoxModel)."""

    url = serializers.HyperlinkedIdentityField(
        view_name="plugins-api:netbox_data_import-api:importprofile-detail",
    )

    class Meta:
        model = ImportProfile
        brief_fields = ["id", "url", "display", "name", "description"]
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
            "capture_extra_data",
            "primary_contact_role",
            "primary_contact_lookup_field",
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


class _RackTypeSlugField(serializers.SlugRelatedField):
    """SlugRelatedField for RackType that defers the queryset import."""

    def get_queryset(self):
        from dcim.models import RackType

        return RackType.objects.all()


class ClassRoleMappingSerializer(serializers.ModelSerializer):
    """Serializer for ClassRoleMapping (plain model)."""

    rack_type = _RackTypeSlugField(slug_field="slug", allow_null=True, required=False)

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

    def validate(self, attrs):
        """Reject Contact candidate resolutions that the importer cannot apply."""
        instance = self.instance
        source_column = attrs.get("source_column", getattr(instance, "source_column", None))
        if source_column == "candidate:contact":
            profile = attrs.get("profile", getattr(instance, "profile", None))
            original_value = attrs.get("original_value", getattr(instance, "original_value", None))
            resolved_fields = attrs.get("resolved_fields", getattr(instance, "resolved_fields", None))
            try:
                candidate_values = json.loads(original_value)
                if not isinstance(candidate_values, dict):
                    raise ValueError
                validate_contact_candidate_resolution(
                    resolved_fields,
                    profile.primary_contact_lookup_field,
                    candidate_values,
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                raise serializers.ValidationError(
                    {"original_value": "Enter the Contact candidate values as a JSON object."}
                ) from None
            except ValidationError as exc:
                raise serializers.ValidationError({"resolved_fields": exc.messages}) from exc
        return attrs

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
