# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Serialize and apply portable Import Profile YAML documents."""

from dataclasses import dataclass
from typing import Any

import yaml
from django.core.exceptions import ValidationError
from django.db import transaction
from yaml.constructor import ConstructorError
from yaml.resolver import BaseResolver

from .catalog import POLICY_SECTIONS, policy_section
from .models import (
    CableClassMapping,
    ClassRoleMapping,
    ColumnMapping,
    ColumnTransformRule,
    DeviceTypeMapping,
    ImportProfile,
    ManufacturerMapping,
    locked_profile_policy,
)
from .object_permissions import (
    ObjectPermissionDenied,
    delete_permission_scoped_objects,
    enforce_saved_object_permission,
    save_permission_scoped_object,
)


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


class DuplicateYamlKeyError(ConstructorError):
    """A YAML mapping repeats a key whose first value would otherwise be discarded."""


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """Load the safe YAML subset and reject duplicate mapping keys at every depth."""


def _construct_unique_mapping(loader, node, deep=False):
    """Construct one mapping without PyYAML's last-value-wins behavior."""
    loader.flatten_mapping(node)
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable mapping key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise DuplicateYamlKeyError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate mapping key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


def load_yaml_document(stream) -> Any:
    """Load untrusted YAML through the shared duplicate-key-rejecting safe loader."""
    loader = _UniqueKeySafeLoader(stream)
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


def serialize_profile(profile: ImportProfile, actor=None) -> dict[str, Any]:
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
        if actor is not None:
            visible = schema.model.objects.restrict(actor, "view").filter(profile=profile)
            if rows.exclude(pk__in=visible).exists():
                raise ObjectPermissionDenied(f"{schema.model._meta.app_label}.view_{schema.model._meta.model_name}")
            rows = visible
        if schema.natural_keys:
            rows = rows.select_related(*(field.name for field in schema.natural_keys))
            if actor is not None:
                _enforce_natural_key_view_permissions(schema, rows, actor)
        document[section.key] = [_serialize_policy_row(schema, row) for row in rows]
    return document


def _enforce_natural_key_view_permissions(schema: PolicyDocumentSchema, rows, actor) -> None:
    """Reject export when a natural-key reference names an object the actor cannot view."""
    for natural_key in schema.natural_keys:
        relation = schema.model._meta.get_field(natural_key.name)
        related_model = relation.remote_field.model
        referenced_ids = set(
            rows.exclude(**{f"{natural_key.name}__isnull": True}).values_list(
                f"{natural_key.name}__pk",
                flat=True,
            )
        )
        visible_ids = set(
            related_model.objects.restrict(actor, "view").filter(pk__in=referenced_ids).values_list("pk", flat=True)
        )
        if referenced_ids - visible_ids:
            raise ObjectPermissionDenied(f"{related_model._meta.app_label}.view_{related_model._meta.model_name}")


def apply_profile_document(data: Any, actor=None) -> tuple[ImportProfile, dict[str, int]]:
    """Create or update one profile and reconcile each supplied policy section."""
    profile_data, section_rows = _validate_document_shape(data)
    with transaction.atomic():
        profile_values = _profile_values(profile_data)
        profile = ImportProfile.objects.filter(name=profile_data["name"]).first()
        created = False
        if profile is None:
            profile = ImportProfile(name=profile_data["name"])
            for field, value in profile_values.items():
                setattr(profile, field, value)
            _validate_instance(profile, "profile")
            result = save_permission_scoped_object(
                actor,
                ImportProfile,
                {"name": profile_data["name"]},
                profile_values,
            )
            profile = result.instance
            created = result.created
        elif actor is not None and not ImportProfile.objects.restrict(actor, "change").filter(pk=profile.pk).exists():
            raise ObjectPermissionDenied("netbox_data_import.change_importprofile")

        with locked_profile_policy(profile.pk):
            profile = ImportProfile.objects.get(pk=profile.pk)
            if not created:
                for field, value in profile_values.items():
                    setattr(profile, field, value)
                _validate_instance(profile, "profile")
                profile = save_permission_scoped_object(
                    actor,
                    ImportProfile,
                    {"pk": profile.pk},
                    profile_values,
                ).instance

            _validate_section_applicability(profile, section_rows)
            prepared_rows = {}
            for key, rows in section_rows.items():
                schema = _SCHEMAS_BY_KEY[key]
                prepared = [_prepare_policy_row(schema, row, index, actor) for index, row in enumerate(rows, 1)]
                _validate_distinct_policy_identities(schema, prepared)
                prepared_rows[key] = prepared
            released_updates = {}
            for key, rows in prepared_rows.items():
                schema = _SCHEMAS_BY_KEY[key]
                if schema.release_changed_before_write:
                    released_updates[key] = _release_changed_rows(profile, schema, rows, actor)

            stats = {}
            for section in POLICY_SECTIONS:
                if section.key not in prepared_rows:
                    continue
                schema = _SCHEMAS_BY_KEY[section.key]
                stats[section.key] = _reconcile_policy_rows(
                    profile,
                    schema,
                    prepared_rows[section.key],
                    actor,
                    released_updates.get(section.key, {}),
                )
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


def _profile_values(profile_data: dict[str, Any]) -> dict[str, Any]:
    """Return validated scalar profile values and adapter configuration."""
    accepted = {"name", "adapter_config", *_PROFILE_FIELDS}
    unknown = sorted(set(profile_data) - accepted)
    if unknown:
        raise ValueError(f"Unknown profile key(s): {', '.join(unknown)}")
    values = {field: profile_data[field] for field in _PROFILE_FIELDS if field in profile_data}
    if "adapter_config" in profile_data:
        values["adapter_config"] = profile_data["adapter_config"]
    return values


def _validate_section_applicability(profile: ImportProfile, sections: dict[str, list]) -> None:
    """Reject each supplied section that the selected Source Adapter cannot use."""
    for key in sections:
        section = policy_section(key)
        if section is None or not section.applies_to(profile.output_kinds):
            raise ValueError(f"Policy section '{key}' does not apply to source adapter '{profile.source_adapter}'.")


def _prepare_policy_row(schema: PolicyDocumentSchema, row: dict[str, Any], index: int, actor=None) -> dict[str, Any]:
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
        visible = related_model.objects if actor is None else related_model.objects.restrict(actor, "view")
        value = prepared[natural_key.name]
        try:
            prepared[natural_key.name] = visible.get(**{natural_key.lookup: value})
        except related_model.DoesNotExist as exc:
            raise ValueError(
                f"{schema.key}[{index}]: {related_model._meta.verbose_name.title()} with "
                f"{natural_key.lookup} '{value}' not found"
            ) from exc
    return prepared


def _validate_distinct_policy_identities(schema: PolicyDocumentSchema, rows: list[dict[str, Any]]) -> None:
    """Reject duplicate natural identities before any policy write."""
    seen = set()
    for row in rows:
        identity = tuple(row[name] for name in schema.identity_fields)
        if identity in seen:
            display = "/".join(str(value) for value in identity)
            raise ValueError(f"Duplicate {schema.key} identity: {display}")
        seen.add(identity)


def _release_changed_rows(
    profile: ImportProfile,
    schema: PolicyDocumentSchema,
    rows: list[dict[str, Any]],
    actor,
) -> dict[tuple[Any, ...], Any]:
    """Release target ownership while preserving logical update permissions."""
    desired = {tuple(row[name] for name in schema.identity_fields): row for row in rows}
    deleted_ids = []
    released_updates = {}
    for stored in schema.model.objects.filter(profile=profile):
        key = tuple(getattr(stored, name) for name in schema.identity_fields)
        row = desired.get(key)
        if row is None:
            deleted_ids.append(stored.pk)
        elif any(getattr(stored, name) != value for name, value in row.items()):
            enforce_saved_object_permission(stored, actor, "change")
            released_updates[key] = stored
    delete_permission_scoped_objects(actor, schema.model.objects.filter(pk__in=deleted_ids))
    schema.model.objects.filter(pk__in=[row.pk for row in released_updates.values()]).delete()
    return released_updates


def _reconcile_policy_rows(
    profile: ImportProfile,
    schema: PolicyDocumentSchema,
    rows: list[dict[str, Any]],
    actor,
    released_updates: dict[tuple[Any, ...], Any],
) -> int:
    """Create or update supplied rows and remove rows absent from the section."""
    retained_ids = []
    for row in rows:
        identity = tuple(row[name] for name in schema.identity_fields)
        lookup = {"profile": profile, **dict(zip(schema.identity_fields, identity, strict=True))}
        instance = released_updates.get(identity) or schema.model.objects.filter(**lookup).first()
        if instance is None:
            instance = schema.model(**lookup)
        for name, value in row.items():
            setattr(instance, name, value)
        _validate_instance(instance, _policy_row_label(schema, row))
        values = {name: getattr(instance, name) for name in row if name not in schema.identity_fields}
        if identity in released_updates:
            instance.save(force_insert=True)
            enforce_saved_object_permission(instance, actor, "change")
            retained_ids.append(instance.pk)
        else:
            result = save_permission_scoped_object(actor, schema.model, lookup, values)
            retained_ids.append(result.instance.pk)
    delete_permission_scoped_objects(
        actor,
        schema.model.objects.filter(profile=profile).exclude(pk__in=retained_ids),
    )
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


__all__ = ("DuplicateYamlKeyError", "apply_profile_document", "load_yaml_document", "serialize_profile")
