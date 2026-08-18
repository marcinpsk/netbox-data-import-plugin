# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""DRF serializers for the data-import plugin models."""

import json

from django.core.exceptions import ValidationError
from netbox.api.serializers import NetBoxModelSerializer
from rest_framework import serializers

from ..adapters import DEFAULT_ADAPTER_KEY, get_adapter
from ..catalog import CATALOG
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


def _validate_target_keys(instance, attrs, names, *, allow_candidates=True):
    """Reject a target key the profile's Source Adapter cannot supply."""
    profile = attrs.get("profile", getattr(instance, "profile", None))
    output_kinds = profile.output_kinds if profile is not None else None
    for name in names:
        value = attrs.get(name, getattr(instance, name, None)) or ""
        if not value and name != "target_field":
            continue
        if not CATALOG.is_valid(value, output_kinds=output_kinds, allow_candidates=allow_candidates):
            raise serializers.ValidationError({name: CATALOG.invalid_key_message(value)})


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
            "source_adapter",
            "adapter_config",
            "tags",
            "custom_fields",
            "created",
            "last_updated",
        ]

    def validate(self, attrs):
        """Validate the adapter configuration and keep the Source Adapter immutable."""
        instance = self.instance
        adapter_key = attrs.get("source_adapter") or getattr(instance, "source_adapter", DEFAULT_ADAPTER_KEY)
        if instance is not None and "source_adapter" in attrs and attrs["source_adapter"] != instance.source_adapter:
            raise serializers.ValidationError(
                {"source_adapter": "The source adapter cannot change after the profile is created."}
            )
        adapter = get_adapter(adapter_key)
        if adapter is None:
            raise serializers.ValidationError({"source_adapter": f"Unknown source adapter '{adapter_key}'."})
        if "adapter_config" in attrs:
            try:
                attrs["adapter_config"] = adapter.config_form_class().validate_config(attrs["adapter_config"])
            except ValidationError as exc:
                raise serializers.ValidationError({"adapter_config": exc.messages}) from exc
        return attrs


class ColumnMappingSerializer(serializers.ModelSerializer):
    """Serializer for ColumnMapping (plain model)."""

    class Meta:
        model = ColumnMapping
        fields = ["id", "profile", "source_column", "target_field"]

    def validate(self, attrs):
        """Resolve the target field through the catalog."""
        _validate_target_keys(self.instance, attrs, ("target_field",))
        return attrs


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

    def validate(self, attrs):
        """Resolve both group targets through the catalog, excluding the candidate targets."""
        _validate_target_keys(self.instance, attrs, ("group_1_target", "group_2_target"), allow_candidates=False)
        return attrs


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
                configured_sources = profile.column_mappings.filter(target_field="candidate:contact").values_list(
                    "source_column", flat=True
                )
                validate_contact_candidate_resolution(
                    resolved_fields,
                    profile.adapter_settings.primary_contact_lookup_field,
                    set(candidate_values) & set(configured_sources),
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
