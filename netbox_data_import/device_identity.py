# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Resolve the NetBox Device Type identity from source make and model names."""

from __future__ import annotations

import re
from typing import NamedTuple

from django.utils.text import slugify

from .identity import identity_text


def _decoded_escapes(value: str) -> str:
    r"""Decode JavaScript-style \uXXXX escapes."""
    return re.sub(r"\\u([0-9a-fA-F]{4})", lambda match: chr(int(match.group(1), 16)), value)


def normalize_mapping_text(value: str) -> str:
    r"""Normalize whitespace and decode JavaScript-style \uXXXX escapes."""
    return " ".join(_decoded_escapes(value).split())


def mapping_identity(value: str) -> str:
    """Return the name identity a make or model compares under, after its escapes are decoded."""
    return identity_text(_decoded_escapes(value))


def default_identity_slugs(make: str, model: str) -> tuple[str, str]:
    """Return the Manufacturer and Device Type slugs one source make and model derive."""
    normalized_make = normalize_mapping_text(make)
    normalized_model = normalize_mapping_text(model)
    return slugify(normalized_make)[:50], slugify(f"{normalized_make}-{normalized_model}")[:50]


DEVICE_TYPE_AMBIGUOUS = "device_type"
MANUFACTURER_AMBIGUOUS = "manufacturer"


class DeviceTypeIdentity(NamedTuple):
    """The Device Type one source make and model name, or which mapping table names it two ways."""

    manufacturer_slug: str
    device_type_slug: str
    explicit: bool
    ambiguous: str = ""


def _targets_by_identity(mappings, identity, target) -> dict:
    """Return each identity's one target, or None when its mappings name more than one target."""
    found: dict = {}
    for mapping in mappings:
        found.setdefault(identity(mapping), set()).add(target(mapping))
    return {key: next(iter(targets)) if len(targets) == 1 else None for key, targets in found.items()}


class DeviceTypeIdentityResolver:
    """Resolve all profile Device Type identities from two batch-loaded indexes."""

    def __init__(self, device_type_mappings, manufacturer_mappings):
        self.device_type_mappings = tuple(device_type_mappings)
        self.manufacturer_mappings = tuple(manufacturer_mappings)
        self._device_types = _targets_by_identity(
            self.device_type_mappings,
            lambda mapping: (mapping_identity(mapping.source_make), mapping_identity(mapping.source_model)),
            lambda mapping: (mapping.netbox_manufacturer_slug, mapping.netbox_device_type_slug),
        )
        self._manufacturers = _targets_by_identity(
            self.manufacturer_mappings,
            lambda mapping: mapping_identity(mapping.source_make),
            lambda mapping: mapping.netbox_manufacturer_slug,
        )

    @classmethod
    def for_profile(cls, profile):
        """Load both mapping tables once for one import run."""
        return cls(
            profile.device_type_mappings.all(),
            profile.manufacturer_mappings.all(),
        )

    def resolve(self, make: str, model: str) -> DeviceTypeIdentity:
        """Return the Device Type slugs a make and model resolve to, or the mapping table that names two."""
        key = (mapping_identity(make), mapping_identity(model))
        if key in self._device_types:
            target = self._device_types[key]
            if target is None:
                return DeviceTypeIdentity("", "", explicit=False, ambiguous=DEVICE_TYPE_AMBIGUOUS)
            manufacturer_slug, device_type_slug = target
            return DeviceTypeIdentity(manufacturer_slug, device_type_slug, explicit=True)
        default_manufacturer_slug, default_device_type_slug = default_identity_slugs(make, model)
        manufacturer_slug = self._manufacturers.get(key[0], default_manufacturer_slug)
        if manufacturer_slug is None:
            return DeviceTypeIdentity("", "", explicit=False, ambiguous=MANUFACTURER_AMBIGUOUS)
        return DeviceTypeIdentity(manufacturer_slug, default_device_type_slug, explicit=False)


__all__ = (
    "DEVICE_TYPE_AMBIGUOUS",
    "MANUFACTURER_AMBIGUOUS",
    "DeviceTypeIdentity",
    "DeviceTypeIdentityResolver",
    "default_identity_slugs",
    "normalize_mapping_text",
)
