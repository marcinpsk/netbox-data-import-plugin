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
    validate_adapter_target_module,
    validate_contact_candidate_resolution,
    validate_section_applicability,
)


class PolicySectionSerializer(serializers.ModelSerializer):
    """Apply the shared policy-section applicability rule on the REST write path."""

    def validate(self, attrs):
        """
        Validate that the policy section applies to the profile.
        
        Parameters:
            attrs (dict): Validated serializer attributes.
        
        Returns:
            dict: The validated serializer attributes.
        
        Raises:
            serializers.ValidationError: If the policy section does not apply to the profile.
        """
        attrs = super().validate(attrs)
        profile = attrs.get("profile", getattr(self.instance, "profile", None))
        try:
            validate_section_applicability(profile, self.Meta.model.POLICY_SECTION)
        except ValidationError as exc:
            raise serializers.ValidationError({"profile": exc.messages}) from exc
        return attrs


def _validate_target_keys(instance, attrs, names, *, allow_candidates=True, required=False):
    """
    Validate configured target keys against the catalog and profile adapter output kinds.
    
    Parameters:
        instance: Existing object containing profile and target values.
        attrs: Candidate attribute values to validate.
        names: Target attribute names to validate.
        allow_candidates (bool): Whether candidate target keys are accepted.
        required (bool): Whether each target value must be present.
    
    Raises:
        serializers.ValidationError: If a required target is missing or a target key is invalid.
    """
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
        """
        Validate the selected source adapter and normalize its configuration.
        
        Parameters:
        	attrs (dict): Serializer attributes to validate and update.
        
        Returns:
        	dict: Validated attributes with normalized adapter configuration.
        
        Raises:
        	serializers.ValidationError: If the adapter is unknown, cannot be changed after profile creation, has an invalid target module, or has invalid configuration.
        """
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
        fields = ["id", "profile", "source_column", "target_field"]

    def validate(self, attrs):
        """Resolve the target field through the catalog."""
        attrs = super().validate(attrs)
        _validate_target_keys(self.instance, attrs, ("target_field",), required=True)
        return attrs


class _RackTypeSlugField(serializers.SlugRelatedField):
    """SlugRelatedField for RackType that defers the queryset import."""

    def get_queryset(self):
        """Return all available rack types."""
        from dcim.models import RackType

        return RackType.objects.all()


class ClassRoleMappingSerializer(PolicySectionSerializer):
    """Serializer for ClassRoleMapping (plain model)."""

    rack_type = _RackTypeSlugField(slug_field="slug", allow_null=True, required=False)

    class Meta:
        model = ClassRoleMapping
        fields = ["id", "profile", "source_class", "creates_rack", "rack_type", "role_slug", "ignore"]


class DeviceTypeMappingSerializer(PolicySectionSerializer):
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


class IgnoredDeviceSerializer(PolicySectionSerializer):
    """Serializer for IgnoredDevice (plain model)."""

    class Meta:
        model = IgnoredDevice
        fields = ["id", "profile", "source_id", "device_name"]


class ColumnTransformRuleSerializer(PolicySectionSerializer):
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
        """
        Validate both group targets against the catalog without allowing candidate targets.
        
        Parameters:
        	attrs (dict): Serializer attributes to validate.
        
        Returns:
        	dict: The validated serializer attributes.
        """
        attrs = super().validate(attrs)
        _validate_target_keys(self.instance, attrs, ("group_1_target", "group_2_target"), allow_candidates=False)
        return attrs


class SourceResolutionSerializer(PolicySectionSerializer):
    """Serializer for SourceResolution (rerere, plain model)."""

    def validate(self, attrs):
        """
        Validate source resolutions, including Contact candidate values and resolved fields.
        
        Returns:
            dict: Validated serializer attributes.
        """
        attrs = super().validate(attrs)
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
