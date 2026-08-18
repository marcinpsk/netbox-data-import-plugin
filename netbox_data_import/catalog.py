# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Static target-field catalog and policy applicability.

This module is the one source of Target Field keys. Forms, REST, GraphQL, YAML, and validation
derive their choices from it and keep no local field list. It imports nothing from the plugin and
nothing from NetBox, so every other layer may depend on it.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class OutputKind:
    """The typed source items a Source Adapter can emit."""

    DEVICE_SOURCE_ROW = "device_source_row"
    RACK_SOURCE_ROW = "rack_source_row"
    SOURCE_TRACE = "source_trace"


class TargetModuleKey:
    """The target domains a Target Module writes to."""

    DEVICE = "device"
    RACK = "rack"
    CABLE = "cable"


class ValueKind:
    """How a Target Field's value is interpreted once a source supplies it."""

    TEXT = "text"
    INTEGER = "integer"
    DECIMAL = "decimal"
    CHOICE = "choice"
    IP_ADDRESS = "ip_address"
    CANDIDATE = "candidate"


@dataclass(frozen=True)
class TargetModule:
    """A Target Module declares the adapter output kinds it consumes."""

    key: str
    label: str
    consumes: frozenset[str]
    # A declared module the release does not implement yet cannot make its adapters selectable.
    implemented: bool


TARGET_MODULES: tuple[TargetModule, ...] = (
    TargetModule(
        key=TargetModuleKey.DEVICE,
        label="Device",
        consumes=frozenset({OutputKind.DEVICE_SOURCE_ROW}),
        implemented=True,
    ),
    TargetModule(
        key=TargetModuleKey.RACK,
        label="Rack",
        consumes=frozenset({OutputKind.RACK_SOURCE_ROW}),
        implemented=True,
    ),
    TargetModule(
        key=TargetModuleKey.CABLE,
        label="Cable",
        consumes=frozenset({OutputKind.SOURCE_TRACE}),
        implemented=False,
    ),
)

_MODULES_BY_KEY = {module.key: module for module in TARGET_MODULES}


@dataclass(frozen=True)
class TargetField:
    """One catalog entry with a fixed key."""

    key: str
    label: str
    value_kind: str
    output_kinds: frozenset[str]
    # A candidate target supplies review candidates rather than a written value. ColumnTransformRule
    # excludes these because a regex capture group cannot produce a candidate bundle.
    candidate_target: bool = False

    @property
    def target_modules(self) -> frozenset[str]:
        """Return the Target Modules that consume this field, derived from its output kinds."""
        return frozenset(m.key for m in TARGET_MODULES if m.consumes & self.output_kinds)


@dataclass(frozen=True)
class KeyFamily:
    """A catalog entry whose exact key is data, not a fixed choice.

    The key is the prefix plus a name. Every surface resolves a family key through this validator,
    so no surface reimplements the prefix rule.
    """

    prefix: str
    label: str
    value_kind: str
    output_kinds: frozenset[str]
    name_label: str = "Custom field"

    def matches(self, key: str) -> bool:
        """Return True when *key* carries this family's prefix."""
        return key.startswith(self.prefix)

    def name_of(self, key: str) -> str:
        """Return the name part of *key*, without the prefix."""
        return key[len(self.prefix) :]

    def is_valid(self, key: str) -> bool:
        """Return True when *key* is a well-formed member: the prefix plus a non-empty name."""
        return self.matches(key) and bool(self.name_of(key).strip())

    def display(self, key: str) -> str:
        """Return the human-readable name for a member key."""
        return f"{self.name_label}: {self.name_of(key)}"

    @property
    def target_modules(self) -> frozenset[str]:
        """Return the Target Modules that consume this family, derived from its output kinds."""
        return frozenset(m.key for m in TARGET_MODULES if m.consumes & self.output_kinds)


_FLAT_ROW_KINDS = frozenset({OutputKind.DEVICE_SOURCE_ROW, OutputKind.RACK_SOURCE_ROW})
_DEVICE_ONLY = frozenset({OutputKind.DEVICE_SOURCE_ROW})

EXTRA_JSON_PREFIX = "extra_json:"
CANDIDATE_TARGET_PREFIX = "candidate:"


@dataclass(frozen=True)
class TargetFieldCatalog:
    """The static registry of Target Fields and key families."""

    fields: tuple[TargetField, ...]
    families: tuple[KeyFamily, ...]
    _by_key: dict[str, TargetField] = field(init=False, repr=False, compare=False, default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "_by_key", {entry.key: entry for entry in self.fields})

    def entry(self, key: str) -> TargetField | None:
        """Return the fixed-key entry for *key*, or None."""
        return self._by_key.get(key)

    def family(self, key: str) -> KeyFamily | None:
        """Return the key family that claims *key*, or None."""
        for candidate in self.families:
            if candidate.matches(key):
                return candidate
        return None

    def is_valid(self, key: str, *, output_kinds: frozenset[str] | None = None, allow_candidates: bool = True) -> bool:
        """Return True when *key* is a Target Field the given output kinds can supply."""
        entry = self.entry(key)
        if entry is not None:
            if entry.candidate_target and not allow_candidates:
                return False
            return output_kinds is None or bool(entry.output_kinds & output_kinds)
        family = self.family(key)
        if family is None or not family.is_valid(key):
            return False
        return output_kinds is None or bool(family.output_kinds & output_kinds)

    def display(self, key: str) -> str:
        """Return the human-readable name for any valid key, or the key itself."""
        entry = self.entry(key)
        if entry is not None:
            return entry.label
        family = self.family(key)
        if family is not None and family.is_valid(key):
            return family.display(key)
        return key

    def choices(self, *, output_kinds: frozenset[str] | None = None, allow_candidates: bool = True):
        """Return Django choice pairs for the fixed-key entries the given output kinds can supply."""
        return [
            (entry.key, entry.label)
            for entry in self.fields
            if (allow_candidates or not entry.candidate_target)
            and (output_kinds is None or entry.output_kinds & output_kinds)
        ]

    def invalid_key_message(self, key: str) -> str:
        """Return the validation message for a rejected key."""
        prefixes = ", ".join(f"'{f.prefix}'" for f in self.families)
        return f"Value '{key}' is not a valid choice. Must be one of the standard field names or start with {prefixes}."


CATALOG = TargetFieldCatalog(
    fields=(
        TargetField("rack_name", "Rack name", ValueKind.TEXT, _FLAT_ROW_KINDS),
        TargetField("device_name", "Device name", ValueKind.TEXT, _FLAT_ROW_KINDS),
        TargetField("device_class", "Device class (maps to role/rack)", ValueKind.TEXT, _FLAT_ROW_KINDS),
        TargetField("face", "Face (Front/Back)", ValueKind.CHOICE, _DEVICE_ONLY),
        TargetField("airflow", "Airflow", ValueKind.CHOICE, _DEVICE_ONLY),
        TargetField("u_position", "U position", ValueKind.INTEGER, _DEVICE_ONLY),
        TargetField("status", "Status", ValueKind.CHOICE, _DEVICE_ONLY),
        TargetField("make", "Make (manufacturer)", ValueKind.TEXT, _DEVICE_ONLY),
        TargetField("model", "Model (device type)", ValueKind.TEXT, _DEVICE_ONLY),
        TargetField("u_height", "U height", ValueKind.DECIMAL, _FLAT_ROW_KINDS),
        TargetField("serial", "Serial number", ValueKind.TEXT, _FLAT_ROW_KINDS),
        TargetField("asset_tag", "Asset tag", ValueKind.TEXT, _DEVICE_ONLY),
        TargetField("primary_ip4", "Primary IPv4", ValueKind.IP_ADDRESS, _DEVICE_ONLY),
        TargetField("primary_ip6", "Primary IPv6", ValueKind.IP_ADDRESS, _DEVICE_ONLY),
        TargetField("oob_ip", "Out-of-band IP", ValueKind.IP_ADDRESS, _DEVICE_ONLY),
        TargetField("primary_contact", "Primary contact", ValueKind.TEXT, _DEVICE_ONLY),
        TargetField("source_id", "Source ID (stored in custom field)", ValueKind.TEXT, _FLAT_ROW_KINDS),
        TargetField(
            f"{CANDIDATE_TARGET_PREFIX}contact",
            "Candidate values: Contact fields",
            ValueKind.CANDIDATE,
            _DEVICE_ONLY,
            candidate_target=True,
        ),
    ),
    families=(
        KeyFamily(
            prefix=EXTRA_JSON_PREFIX,
            label="Custom field",
            value_kind=ValueKind.TEXT,
            output_kinds=_DEVICE_ONLY,
        ),
    ),
)


@dataclass(frozen=True)
class PolicySection:
    """A profile policy table and the adapter output kinds it applies to."""

    key: str
    label: str
    output_kinds: frozenset[str]

    def applies_to(self, output_kinds: frozenset[str]) -> bool:
        """Return True when an adapter emitting *output_kinds* can use this section."""
        return bool(self.output_kinds & output_kinds)


POLICY_SECTIONS: tuple[PolicySection, ...] = (
    PolicySection("column_mappings", "Column Mappings", _FLAT_ROW_KINDS),
    PolicySection("column_transform_rules", "Column Transform Rules", _FLAT_ROW_KINDS),
    PolicySection("class_role_mappings", "Class/Role Mappings", _FLAT_ROW_KINDS),
    PolicySection("device_type_mappings", "Device Type Mappings", _FLAT_ROW_KINDS),
    PolicySection("manufacturer_mappings", "Manufacturer Mappings", _FLAT_ROW_KINDS),
    PolicySection("ignored_devices", "Ignored Devices", _FLAT_ROW_KINDS),
    PolicySection("source_resolutions", "Source Resolutions", _DEVICE_ONLY),
    PolicySection("device_existing_matches", "Device Existing Matches", _DEVICE_ONLY),
    PolicySection("ignored_field_differences", "Ignored Field Differences", _DEVICE_ONLY),
)

_SECTIONS_BY_KEY = {section.key: section for section in POLICY_SECTIONS}


def target_module(key: str) -> TargetModule | None:
    """Return the Target Module declared under *key*."""
    return _MODULES_BY_KEY.get(key)


def policy_section(key: str) -> PolicySection | None:
    """Return the policy section declared under *key*."""
    return _SECTIONS_BY_KEY.get(key)


def consuming_modules(output_kinds: frozenset[str]) -> tuple[TargetModule, ...]:
    """Return the Target Modules that consume any of *output_kinds*."""
    return tuple(module for module in TARGET_MODULES if module.consumes & output_kinds)


def has_implemented_module(output_kinds: frozenset[str]) -> bool:
    """Return True when a Target Module this release implements consumes any of *output_kinds*."""
    return any(module.implemented for module in consuming_modules(output_kinds))


__all__ = (
    "CANDIDATE_TARGET_PREFIX",
    "CATALOG",
    "EXTRA_JSON_PREFIX",
    "POLICY_SECTIONS",
    "TARGET_MODULES",
    "KeyFamily",
    "OutputKind",
    "PolicySection",
    "TargetField",
    "TargetFieldCatalog",
    "TargetModule",
    "TargetModuleKey",
    "ValueKind",
    "consuming_modules",
    "has_implemented_module",
    "policy_section",
    "target_module",
)
