# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Review exact source/NetBox value pairs for matched Device fields."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any


def _text(value: Any) -> str:
    """Return the display text used for an exact text comparison."""
    if value is None:
        return ""
    return str(value).strip()


def _number(value: Any) -> str:
    """Return a stable string for values that represent a number."""
    if value is None or value == "":
        return ""
    try:
        number = float(value)
        if number == int(number):
            return str(int(number))
        return str(number)
    except (TypeError, ValueError, OverflowError):
        return _text(value)


def _identity(value: Any) -> str:
    """Return the canonical identity for a model instance or scalar."""
    if value is None:
        return ""
    primary_key = getattr(value, "pk", None)
    return _text(primary_key if primary_key is not None else value)


def _related_display(value: Any) -> str:
    """Return a human-readable value for a related object snapshot."""
    if value is None:
        return ""
    return _text(getattr(value, "name", value))


def _device_rack_name(device) -> str:
    """Return the raw rack name currently assigned to a device."""
    return device.rack.name if getattr(device, "rack_id", None) else ""


def _device_rack_display(device) -> str:
    """Return the location-aware rack label currently assigned to a device."""
    if not getattr(device, "rack_id", None):
        return ""
    rack = device.rack
    if getattr(rack, "location_id", None):
        return f"{rack.location} / {rack.name}"
    return _text(rack.name)


def _device_rack_location_id(device):
    """Return the location that scopes a device's rack, if it has one."""
    return getattr(device.rack, "location_id", None) if getattr(device, "rack_id", None) else None


def _scope_rack_canonical(snapshot: dict[str, str], location_id) -> dict[str, str]:
    """Prefix a rack canonical value with its location so both sides compare the same way."""
    snapshot["canonical"] = f"{location_id or ''}:{snapshot['canonical']}"
    return snapshot


def _device_type_value(device):
    """Return the canonical and display data for the current DeviceType."""
    device_type = device.device_type
    manufacturer = device_type.manufacturer
    return (manufacturer.slug, device_type.slug, manufacturer.name, device_type.model)


def _device_type_display(value: Any) -> str:
    """Return a human-readable DeviceType value."""
    if isinstance(value, (tuple, list)) and len(value) >= 4:
        return f"{value[2]} / {value[3]}"
    return _text(value)


def _device_type_normalize(value: Any) -> str:
    """Return the stable manufacturer/device-type slug pair."""
    if isinstance(value, (tuple, list)) and len(value) >= 2:
        return f"{_text(value[0])}/{_text(value[1])}"
    return _text(value)


def _device_role_value(device) -> str:
    """Return the current DeviceRole slug."""
    return _text(device.role.slug) if getattr(device, "role_id", None) else ""


def _device_ip_value(target_field: str):
    """Return a reader for one of the Device's IP fields."""

    def read(device):
        current = getattr(device, target_field, None)
        return _text(current.address) if current is not None else ""

    return read


def _ip_normalize(value: Any) -> str:
    """Return an address in the one spelling both sides compare on."""
    import ipaddress

    text = _text(value)
    if not text:
        return ""
    try:
        return str(ipaddress.ip_interface(text))
    except ValueError:
        return text


@dataclass(frozen=True)
class FieldDefinition:
    """One source of truth for a Device field's review behavior."""

    target_field: str
    current_value: Callable[[Any], Any]
    normalize: Callable[[Any], str] = _text
    display: Callable[[Any], str] = _text
    provided: Callable[[Any], bool] = lambda value: True
    writable: bool = True
    reviewable: bool = True

    def snapshot(self, value: Any, display_override: str | None = None) -> dict[str, str]:
        """Return the persisted canonical and display representation."""
        return {
            "canonical": self.normalize(value),
            "display": self.display(value) if display_override is None else display_override,
        }


def _provided_nonempty(value: Any) -> bool:
    """Return whether a writer receives a value for an optional field."""
    return bool(_text(value))


def _provided_optional(value: Any) -> bool:
    """Return whether a source row explicitly supplies an optional field."""
    return value is not None and value != ""


def _provided_not_none(value: Any) -> bool:
    """Return whether a source row supplied a value, including zero and empty text."""
    return value is not None


# Keep this registry private. Callers use DeviceFieldReviewer instead of
# reimplementing normalization, comparison, display, or write semantics.
_FIELD_DEFINITIONS: tuple[FieldDefinition, ...] = (
    FieldDefinition("rack_name", _device_rack_name, display=_text),
    FieldDefinition("u_position", lambda device: device.position, normalize=_number, display=_number),
    FieldDefinition("face", lambda device: device.face or "", provided=_provided_optional),
    FieldDefinition("airflow", lambda device: device.airflow or "", provided=_provided_nonempty),
    FieldDefinition("status", lambda device: device.status or ""),
    FieldDefinition("serial", lambda device: device.serial or "", provided=_provided_nonempty),
    FieldDefinition("asset_tag", lambda device: device.asset_tag or "", provided=_provided_nonempty),
    FieldDefinition(
        "device_type",
        _device_type_value,
        normalize=_device_type_normalize,
        display=_device_type_display,
    ),
    FieldDefinition("role", _device_role_value),
    # The writer assigns these, so they are differences the preview has to report.
    FieldDefinition(
        "primary_ip4", _device_ip_value("primary_ip4"), normalize=_ip_normalize, provided=_provided_nonempty
    ),
    FieldDefinition(
        "primary_ip6", _device_ip_value("primary_ip6"), normalize=_ip_normalize, provided=_provided_nonempty
    ),
    FieldDefinition("oob_ip", _device_ip_value("oob_ip"), normalize=_ip_normalize, provided=_provided_nonempty),
    FieldDefinition("tenant", lambda device: device.tenant, normalize=_identity, display=_related_display),
    FieldDefinition("location", lambda device: device.location, normalize=_identity, display=_related_display),
    # These values are shown by the legacy field-diff helper, but the Device
    # writer does not assign them. Keep them reviewable and non-writable so an
    # ignored review never claims that a write was suppressed.
    FieldDefinition("device_name", lambda device: device.name, writable=False),
    FieldDefinition(
        "u_height",
        lambda device: device.device_type.u_height if getattr(device, "device_type_id", None) else None,
        normalize=_number,
        display=_number,
        provided=_provided_not_none,
        writable=False,
    ),
)

_DEFINITIONS_BY_FIELD = {definition.target_field: definition for definition in _FIELD_DEFINITIONS}


@dataclass(frozen=True)
class DeviceFieldReview:
    """The review state and effective proposal for one matched Device."""

    differing: dict[str, dict[str, str]]
    ignored: dict[str, dict[str, str]]
    informational: dict[str, dict[str, str]]
    effective_proposal: dict[str, Any]
    snapshots: dict[str, tuple[dict[str, str], dict[str, str]]]


class DeviceFieldReviewer:
    """Apply exact persisted reviews to a matched Device proposal."""

    def __init__(self, profile, ignored_records: Mapping[tuple[str, int, str], Any] | None = None):
        self.profile = profile
        self._ignored_records = dict(ignored_records or {})
        review_device_ids: dict[str, set[int]] = {}
        for source_id, device_id, _target_field in self._ignored_records:
            review_device_ids.setdefault(source_id, set()).add(device_id)
        self._review_device_ids = {
            source_id: frozenset(device_ids) for source_id, device_ids in review_device_ids.items()
        }

    @classmethod
    def for_profile(cls, profile):
        """Load the profile's current review records once for an import run."""
        from .models import IgnoredFieldDifference

        records = IgnoredFieldDifference.objects.filter(profile=profile)
        return cls(
            profile,
            {(_text(record.source_id), record.netbox_device_id, record.target_field): record for record in records},
        )

    @staticmethod
    def definition(target_field: str) -> FieldDefinition | None:
        """Return the registered definition for one target field."""
        return _DEFINITIONS_BY_FIELD.get(target_field)

    @staticmethod
    def reviewable_fields() -> frozenset[str]:
        """Return target fields that can be reviewed and ignored."""
        return frozenset(d.target_field for d in _FIELD_DEFINITIONS if d.reviewable)

    @staticmethod
    def non_writable_fields() -> frozenset[str]:
        """Return fields shown for information but not assigned by the writer."""
        return frozenset(d.target_field for d in _FIELD_DEFINITIONS if not d.writable)

    @staticmethod
    def current_snapshot(matched_device, target_field: str) -> dict[str, str] | None:
        """Return the current canonical NetBox snapshot for one registered field."""
        definition = _DEFINITIONS_BY_FIELD.get(target_field)
        if definition is None:
            return None
        value = definition.current_value(matched_device)
        if target_field != "rack_name":
            return definition.snapshot(value)
        snapshot = definition.snapshot(value, _device_rack_display(matched_device))
        return _scope_rack_canonical(snapshot, _device_rack_location_id(matched_device))

    def review_device_ids(self, source_id: str) -> frozenset[int]:
        """Return unique Device IDs bound to reviews for one source row."""
        return self._review_device_ids.get(_text(source_id), frozenset())

    @staticmethod
    def field_differences(
        matched_device,
        proposal: Mapping[str, Any],
        *,
        display_overrides: Mapping[str, str] | None = None,
    ):
        """Return the writable differences and the reported-only ones as two maps."""
        differing, informational, _ = DeviceFieldReviewer._compare(
            matched_device,
            proposal,
            display_overrides=display_overrides,
        )
        return differing, informational

    @staticmethod
    def field_diff(
        matched_device,
        proposal: Mapping[str, Any],
        *,
        include_informational: bool = False,
        display_overrides: Mapping[str, str] | None = None,
    ):
        """Return current differences without loading persisted review records."""
        differing, informational = DeviceFieldReviewer.field_differences(
            matched_device,
            proposal,
            display_overrides=display_overrides,
        )
        if include_informational:
            return {**differing, **informational}
        return differing

    def review(
        self,
        source_id: str,
        matched_device,
        proposal: Mapping[str, Any],
        *,
        display_overrides: Mapping[str, str] | None = None,
    ) -> DeviceFieldReview:
        """Return differing, ignored, and write-safe values for one Device."""
        differing, informational, snapshots = self._compare(
            matched_device,
            proposal,
            display_overrides=display_overrides,
        )
        effective = dict(proposal)
        ignored = {}
        for target_field, (file_snapshot, netbox_snapshot) in snapshots.items():
            record = self._ignored_records.get((_text(source_id), matched_device.pk, target_field))
            if record is None:
                continue
            if (
                record.file_snapshot.get("canonical") == file_snapshot["canonical"]
                and record.netbox_snapshot.get("canonical") == netbox_snapshot["canonical"]
            ):
                ignored[target_field] = {
                    "netbox": netbox_snapshot["display"],
                    "file": file_snapshot["display"],
                }
                definition = _DEFINITIONS_BY_FIELD[target_field]
                effective[target_field] = definition.current_value(matched_device)
                differing.pop(target_field, None)
                informational.pop(target_field, None)
        return DeviceFieldReview(
            differing=differing,
            ignored=ignored,
            informational=informational,
            effective_proposal=effective,
            snapshots=snapshots,
        )

    @staticmethod
    def _compare(
        matched_device,
        proposal: Mapping[str, Any],
        *,
        display_overrides: Mapping[str, str] | None = None,
    ):
        """Compare a proposal to a Device using the private field registry."""
        display_overrides = display_overrides or {}
        differing: dict[str, dict[str, str]] = {}
        informational: dict[str, dict[str, str]] = {}
        snapshots: dict[str, tuple[dict[str, str], dict[str, str]]] = {}
        for definition in _FIELD_DEFINITIONS:
            if definition.target_field not in proposal or not definition.provided(proposal[definition.target_field]):
                continue
            file_value = proposal[definition.target_field]
            netbox_value = definition.current_value(matched_device)
            file_snapshot = definition.snapshot(file_value, display_overrides.get(definition.target_field))
            netbox_override = None
            if definition.target_field == "rack_name":
                netbox_override = _device_rack_display(matched_device)
                _scope_rack_canonical(file_snapshot, proposal.get("_rack_location_id"))
            netbox_snapshot = definition.snapshot(netbox_value, netbox_override)
            if definition.target_field == "rack_name":
                _scope_rack_canonical(netbox_snapshot, _device_rack_location_id(matched_device))
            if file_snapshot["canonical"] == netbox_snapshot["canonical"]:
                continue
            snapshots[definition.target_field] = (file_snapshot, netbox_snapshot)
            values = {"netbox": netbox_snapshot["display"], "file": file_snapshot["display"]}
            if definition.writable:
                differing[definition.target_field] = values
            else:
                informational[definition.target_field] = values
        return differing, informational, snapshots
