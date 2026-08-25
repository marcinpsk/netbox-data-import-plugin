# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""DRF serializers for the data-import plugin models."""

import json

from django.core.exceptions import ValidationError
from netbox.api.serializers import NetBoxModelSerializer, ValidatedModelSerializer
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
    validate_adapter_target_module,
    validate_contact_candidate_resolution,
    validate_section_applicability,
)


class PolicySectionApplicabilityMixin:
    """Apply the shared policy-section applicability rule on the REST write path."""

    def validate_policy_section(self, attrs):
        """Reject a row whose section does not apply to the profile's Source Adapter."""
        profile = attrs.get("profile", getattr(self.instance, "profile", None))
        try:
            validate_section_applicability(profile, self.Meta.model.POLICY_SECTION)
        except ValidationError as exc:
            raise serializers.ValidationError({"profile": exc.messages}) from exc


class PolicySectionSerializer(PolicySectionApplicabilityMixin, ValidatedModelSerializer):
    """Base for the policy-section serializers, which are backed by plain Django models."""

    # No Meta.fields below lists display_url: these rows have no UI detail route to reverse.

    def validate(self, attrs):
        """Run the policy checks, then the NetBox model validation."""
        # A nested serializer is handed a resolved instance, so no field mapping exists to check.
        if getattr(self, "nested", False):
            return super().validate(attrs)
        # Ahead of super(), whose full_clean() reports the same rules as non-field errors.
        self.validate_policy_section(attrs)
        self.validate_policy_row(attrs)
        return super().validate(attrs)

    def validate_policy_row(self, attrs):
        """Check the fields this model resolves through the catalog. Subclasses override."""


def _validate_target_keys(instance, attrs, names, *, allow_candidates=True, required=False):
    """Reject a target key the profile's Source Adapter cannot supply."""
    profile = attrs.get("profile", getattr(instance, "profile", None))
    output_kinds = profile.output_kinds if profile is not None else None
    for name in names:
        value = attrs.get(name, getattr(instance, name, None)) or ""
        if not value and not required:
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
        if instance is None:
            try:
                validate_adapter_target_module(adapter_key)
            except ValidationError as exc:
                raise serializers.ValidationError(exc.message_dict) from exc
        # Normalize unconditionally: this serializer never calls Model.full_clean, so an absent key
        # would otherwise persist {} while the form path persists the full mapping.
        raw_config = attrs.get("adapter_config", getattr(instance, "adapter_config", None))
        try:
            attrs["adapter_config"] = adapter.config_form_class().validate_config(raw_config)
        except ValidationError as exc:
            raise serializers.ValidationError({"adapter_config": exc.messages}) from exc
        return attrs


class ColumnMappingSerializer(PolicySectionSerializer):
    """Serializer for ColumnMapping (plain model)."""

    class Meta:
        model = ColumnMapping
        fields = ["id", "url", "display", "profile", "source_column", "target_field"]

    def validate_policy_row(self, attrs):
        """Resolve the target field through the catalog."""
        _validate_target_keys(self.instance, attrs, ("target_field",), required=True)


class _RackTypeSlugField(serializers.SlugRelatedField):
    """SlugRelatedField for RackType that defers the queryset import."""

    def get_queryset(self):
        from dcim.models import RackType

        return RackType.objects.all()


class ClassRoleMappingSerializer(PolicySectionSerializer):
    """Serializer for ClassRoleMapping (plain model)."""

    rack_type = _RackTypeSlugField(slug_field="slug", allow_null=True, required=False)

    class Meta:
        model = ClassRoleMapping
        fields = ["id", "url", "display", "profile", "source_class", "creates_rack", "rack_type", "role_slug", "ignore"]


class DeviceTypeMappingSerializer(PolicySectionSerializer):
    """Serializer for DeviceTypeMapping (plain model)."""

    class Meta:
        model = DeviceTypeMapping
        fields = [
            "id",
            "url",
            "display",
            "profile",
            "source_make",
            "source_model",
            "netbox_manufacturer_slug",
            "netbox_device_type_slug",
        ]


class IgnoredDeviceSerializer(PolicySectionSerializer):
    """Serializer for IgnoredDevice (plain model)."""

    class Meta:
        model = IgnoredDevice
        fields = ["id", "url", "display", "profile", "source_id", "device_name"]


class ColumnTransformRuleSerializer(PolicySectionSerializer):
    """Serializer for ColumnTransformRule (plain model)."""

    class Meta:
        model = ColumnTransformRule
        fields = [
            "id",
            "url",
            "display",
            "profile",
            "source_column",
            "pattern",
            "group_1_target",
            "group_2_target",
        ]

    def validate_policy_row(self, attrs):
        """Resolve both group targets through the catalog, excluding the candidate targets."""
        _validate_target_keys(self.instance, attrs, ("group_1_target", "group_2_target"), allow_candidates=False)


class SourceResolutionSerializer(PolicySectionSerializer):
    """Serializer for SourceResolution (rerere, plain model)."""

    def validate_policy_row(self, attrs):
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

    class Meta:
        model = SourceResolution
        fields = [
            "id",
            "url",
            "display",
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
