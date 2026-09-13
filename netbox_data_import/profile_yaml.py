# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Serialize and apply portable Import Profile YAML documents."""

from dataclasses import dataclass
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction

from .catalog import POLICY_SECTIONS, policy_section
from .models import (
    CableClassMapping,
    ClassRoleMapping,
    ColumnMapping,
    ColumnTransformRule,
    DeviceTypeMapping,
    ImportProfile,
    ManufacturerMapping,
)
from .object_permissions import save_or_refetch


@dataclass(frozen=True)
class NaturalKeyField:
    """Describe one related field represented by a stable lookup value."""

    name: str
    lookup: str


@dataclass(frozen=True)
class PolicyDocumentSchema:
    """Describe one catalog policy section's portable document shape."""

    model: Any
    identity_fields: tuple[str, ...]
    required_fields: tuple[str, ...]
    fields: tuple[str, ...]
    natural_keys: tuple[NaturalKeyField, ...] = ()
    release_changed_before_write: bool = False

    @property
    def key(self) -> str:
        """Return the catalog key owned by the model."""
        return self.model.POLICY_SECTION

    @property
    def natural_key_map(self) -> dict[str, NaturalKeyField]:
        """Return natural-key declarations by model field name."""
        return {field.name: field for field in self.natural_keys}


_POLICY_DOCUMENT_SCHEMAS = (
    PolicyDocumentSchema(
        model=ColumnMapping,
        identity_fields=("source_column", "target_field"),
        required_fields=("source_column", "target_field"),
        fields=("source_column", "target_field"),
        release_changed_before_write=True,
    ),
    PolicyDocumentSchema(
        model=ClassRoleMapping,
        identity_fields=("source_class",),
        required_fields=("source_class",),
        fields=("source_class", "creates_rack", "rack_type", "role_slug", "ignore"),
        natural_keys=(NaturalKeyField("rack_type", "slug"),),
    ),
    PolicyDocumentSchema(
        model=DeviceTypeMapping,
        identity_fields=("source_make", "source_model"),
        required_fields=(
            "source_make",
            "source_model",
            "netbox_manufacturer_slug",
            "netbox_device_type_slug",
        ),
        fields=(
            "source_make",
            "source_model",
            "netbox_manufacturer_slug",
            "netbox_device_type_slug",
        ),
    ),
    PolicyDocumentSchema(
        model=ManufacturerMapping,
        identity_fields=("source_make",),
        required_fields=("source_make", "netbox_manufacturer_slug"),
        fields=("source_make", "netbox_manufacturer_slug"),
    ),
    PolicyDocumentSchema(
        model=ColumnTransformRule,
        identity_fields=("source_column",),
        required_fields=("source_column", "pattern"),
        fields=("source_column", "pattern", "group_1_target", "group_2_target"),
        release_changed_before_write=True,
    ),
    PolicyDocumentSchema(
        model=CableClassMapping,
        identity_fields=("cable_class",),
        required_fields=("cable_class",),
        fields=(
            "cable_class",
            "cable_type_resolved",
            "cable_type",
            "cable_profile_resolved",
            "cable_profile",
        ),
    ),
)

_SCHEMAS_BY_KEY = {schema.key: schema for schema in _POLICY_DOCUMENT_SCHEMAS}
_PROFILE_FIELDS = ("description", "source_adapter")


def serialize_profile(profile: ImportProfile) -> dict[str, Any]:
    """Return one portable profile document with only applicable policy sections."""
    document: dict[str, Any] = {
        "profile": {
            "name": profile.name,
            "description": profile.description,
            "source_adapter": profile.source_adapter,
            "adapter_config": profile.adapter_config,
        }
    }
    for section in POLICY_SECTIONS:
        schema = _SCHEMAS_BY_KEY.get(section.key)
        if schema is None or not section.applies_to(profile.output_kinds):
            continue
        relation = schema.model._meta.get_field("profile").remote_field.get_accessor_name()
        rows = getattr(profile, relation).all()
        if schema.natural_keys:
            rows = rows.select_related(*(field.name for field in schema.natural_keys))
        document[section.key] = [_serialize_policy_row(schema, row) for row in rows]
    return document


def apply_profile_document(data: Any) -> tuple[ImportProfile, dict[str, int]]:
    """Create or update one profile and reconcile each supplied policy section."""
    profile_data, section_rows = _validate_document_shape(data)
    with transaction.atomic():
        profile = ImportProfile.objects.filter(name=profile_data["name"]).first()
        if profile is None:
            profile = ImportProfile(name=profile_data["name"])
        for field, value in _profile_values(profile_data).items():
            setattr(profile, field, value)
        _validate_instance(profile, "profile")
        profile, _created = save_or_refetch(profile, ImportProfile, {"name": profile_data["name"]})

        _validate_section_applicability(profile, section_rows)
        prepared_rows = {
            key: [_prepare_policy_row(_SCHEMAS_BY_KEY[key], row, index) for index, row in enumerate(rows, 1)]
            for key, rows in section_rows.items()
        }
        for key, rows in prepared_rows.items():
            schema = _SCHEMAS_BY_KEY[key]
            if schema.release_changed_before_write:
                _release_changed_rows(profile, schema, rows)

        stats = {}
        for section in POLICY_SECTIONS:
            if section.key not in prepared_rows:
                continue
            schema = _SCHEMAS_BY_KEY[section.key]
            stats[section.key] = _reconcile_policy_rows(profile, schema, prepared_rows[section.key])
        # atomic-exit-safe: profile-import-committed
        return profile, stats


def _serialize_policy_row(schema: PolicyDocumentSchema, row) -> dict[str, Any]:
    """Return one policy row using stable related-object references."""
    natural_keys = schema.natural_key_map
    serialized = {}
    for name in schema.fields:
        value = getattr(row, name)
        if name in natural_keys:
            value = None if value is None else getattr(value, natural_keys[name].lookup)
        serialized[name] = value
    return serialized


def _validate_document_shape(data: Any) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    """Validate the document container and return its profile and section mappings."""
    if not isinstance(data, dict) or "profile" not in data:
        raise ValueError("YAML must contain a top-level 'profile' key.")
    profile_data = data["profile"]
    if not isinstance(profile_data, dict):
        raise TypeError("The 'profile' value must be a mapping (dict), not a scalar or list.")
    if not profile_data.get("name"):
        raise ValueError("Profile YAML must include a 'name' field.")

    unknown = sorted(set(data) - {"profile", *_SCHEMAS_BY_KEY})
    if unknown:
        raise ValueError(f"Unknown profile YAML section(s): {', '.join(unknown)}")

    sections = {}
    for key in data:
        if key == "profile":
            continue
        value = data[key]
        if not isinstance(value, list):
            raise ValueError(  # noqa: TRY004 - Keep the established document-validation interface.
                f"'{key}' must be a list of mappings; use [] to explicitly remove all entries, "
                f"got {type(value).__name__}."
            )
        for index, row in enumerate(value, 1):
            if not isinstance(row, dict):
                raise TypeError(f"'{key}[{index}]' must be a mapping, got {type(row).__name__}.")
        sections[key] = value
    return profile_data, sections


def _legacy_adapter_config(profile_data: dict[str, Any]) -> dict[str, Any] | None:
    """Translate profile keys exported before the Source Adapter cutover."""
    from .adapter_forms import FlatWorkbookConfigForm

    legacy_keys = set(FlatWorkbookConfigForm.base_fields) & set(profile_data)
    if not legacy_keys:
        return None
    conflicting = sorted({"adapter_config", "source_adapter"} & set(profile_data))
    if conflicting:
        raise ValueError(
            f"Profile key(s) {', '.join(sorted(legacy_keys))} belong to a release before the adapter "
            f"cutover and cannot be combined with {', '.join(conflicting)}."
        )
    config = {key: profile_data[key] for key in legacy_keys}
    slug = config.get("primary_contact_role")
    if slug:
        from tenancy.models import ContactRole

        role = ContactRole.objects.filter(slug=slug).first()
        if role is None:
            raise ValueError(f"No Contact Role matches the primary_contact_role slug '{slug}'.")
        config["primary_contact_role"] = role.name
    return config


def _profile_values(profile_data: dict[str, Any]) -> dict[str, Any]:
    """Return validated scalar profile values and adapter configuration."""
    legacy_config = _legacy_adapter_config(profile_data)
    accepted = {"name", "adapter_config", *_PROFILE_FIELDS}
    if legacy_config is not None:
        accepted |= set(legacy_config)
    unknown = sorted(set(profile_data) - accepted)
    if unknown:
        raise ValueError(f"Unknown profile key(s): {', '.join(unknown)}")
    values = {field: profile_data[field] for field in _PROFILE_FIELDS if field in profile_data}
    if legacy_config is not None:
        from .adapters import FlatWorkbookAdapter

        values["source_adapter"] = FlatWorkbookAdapter.key
        values["adapter_config"] = legacy_config
    elif "adapter_config" in profile_data:
        values["adapter_config"] = profile_data["adapter_config"]
    return values


def _validate_section_applicability(profile: ImportProfile, sections: dict[str, list]) -> None:
    """Reject each supplied section that the selected Source Adapter cannot use."""
    for key in sections:
        section = policy_section(key)
        if section is None or not section.applies_to(profile.output_kinds):
            raise ValueError(f"Policy section '{key}' does not apply to source adapter '{profile.source_adapter}'.")


def _prepare_policy_row(schema: PolicyDocumentSchema, row: dict[str, Any], index: int) -> dict[str, Any]:
    """Validate one row's keys and resolve its stable related-object references."""
    missing = [field for field in schema.required_fields if field not in row]
    if missing:
        raise ValueError(f"'{schema.key}[{index}]' missing required key(s): {', '.join(missing)}")
    unknown = sorted(set(row) - set(schema.fields))
    if unknown:
        raise ValueError(f"'{schema.key}[{index}]' has unknown key(s): {', '.join(unknown)}")

    prepared = dict(row)
    for natural_key in schema.natural_keys:
        if natural_key.name not in prepared or prepared[natural_key.name] is None:
            continue
        model_field = schema.model._meta.get_field(natural_key.name)
        related_model = model_field.remote_field.model
        value = prepared[natural_key.name]
        try:
            prepared[natural_key.name] = related_model.objects.get(**{natural_key.lookup: value})
        except related_model.DoesNotExist as exc:
            raise ValueError(
                f"{schema.key}[{index}]: {related_model._meta.verbose_name.title()} with "
                f"{natural_key.lookup} '{value}' not found"
            ) from exc
    return prepared


def _release_changed_rows(profile: ImportProfile, schema: PolicyDocumentSchema, rows: list[dict[str, Any]]) -> None:
    """Release target ownership before validating replacement policy rows."""
    desired = {tuple(row[name] for name in schema.identity_fields): row for row in rows}
    for stored in schema.model.objects.filter(profile=profile):
        key = tuple(getattr(stored, name) for name in schema.identity_fields)
        row = desired.get(key)
        if row is None or any(getattr(stored, name) != value for name, value in row.items()):
            stored.delete()


def _reconcile_policy_rows(
    profile: ImportProfile,
    schema: PolicyDocumentSchema,
    rows: list[dict[str, Any]],
) -> int:
    """Create or update supplied rows and remove rows absent from the section."""
    retained_ids = []
    for row in rows:
        lookup = {"profile": profile, **{name: row[name] for name in schema.identity_fields}}
        instance = schema.model.objects.filter(**lookup).first()
        if instance is None:
            instance = schema.model(**lookup)
        for name, value in row.items():
            setattr(instance, name, value)
        _validate_instance(instance, _policy_row_label(schema, row))
        instance, _created = save_or_refetch(instance, schema.model, lookup)
        retained_ids.append(instance.pk)
    schema.model.objects.filter(profile=profile).exclude(pk__in=retained_ids).delete()
    return len(rows)


def _policy_row_label(schema: PolicyDocumentSchema, row: dict[str, Any]) -> str:
    """Return one stable validation label from the row identity."""
    identity = "/".join(str(row[field]) for field in schema.identity_fields)
    return f"{schema.key}[{identity}]"


def _validate_instance(instance, label: str) -> None:
    """Validate a model and expose its field errors through the document interface."""
    try:
        instance.full_clean(validate_unique=False)
    except ValidationError as exc:
        if hasattr(exc, "message_dict"):
            message = "; ".join(f"{field}: {', '.join(errors)}" for field, errors in exc.message_dict.items())
        else:
            message = "; ".join(exc.messages)
        raise ValueError(f"Validation error in {label}: {message}") from exc


__all__ = ("apply_profile_document", "serialize_profile")
