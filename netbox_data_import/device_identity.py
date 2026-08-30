# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Resolve NetBox Device and Device Type identities from source values."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from django.utils.text import slugify


def normalize_mapping_text(value: str) -> str:
    r"""Normalize whitespace and decode JavaScript-style \uXXXX escapes."""
    value = re.sub(r"\\u([0-9a-fA-F]{4})", lambda match: chr(int(match.group(1), 16)), value)
    return " ".join(value.split())


@dataclass(frozen=True)
class DeviceIdentityMatch:
    """One strong Device identity match, or the reason no match is safe."""

    device: Any = None
    method: str = ""
    conflict: str = ""
    value: str = ""


def resolve_strong_device_identity(devices, serial: str, asset_tag: str) -> DeviceIdentityMatch:
    """Resolve serial and asset tag to one Device, or report their conflict."""
    serial_matches = list(devices.filter(serial=serial)[:2]) if serial else []
    asset_matches = list(devices.filter(asset_tag__iexact=asset_tag)[:2]) if asset_tag else []
    if len(serial_matches) > 1:
        return DeviceIdentityMatch(conflict="device.ambiguous_serial", value=serial)
    if len(asset_matches) > 1:
        return DeviceIdentityMatch(conflict="device.ambiguous_asset_tag", value=asset_tag)

    serial_device = serial_matches[0] if serial_matches else None
    asset_device = asset_matches[0] if asset_matches else None
    if serial_device is not None and asset_device is not None and serial_device.pk != asset_device.pk:
        return DeviceIdentityMatch(
            conflict="device.conflicting_identity",
            value=f"{serial} / {asset_tag}",
        )
    if serial_device is not None:
        return DeviceIdentityMatch(device=serial_device, method="serial")
    if asset_device is not None:
        return DeviceIdentityMatch(device=asset_device, method="asset tag")
    return DeviceIdentityMatch()


class DeviceTypeIdentityResolver:
    """Resolve all profile Device Type identities from two batch-loaded indexes."""

    def __init__(self, device_type_mappings, manufacturer_mappings):
        self.device_type_mappings = tuple(device_type_mappings)
        self.manufacturer_mappings = tuple(manufacturer_mappings)
        self._device_types_exact = {}
        self._device_types_by_make = {}
        for mapping in self.device_type_mappings:
            self._device_types_exact.setdefault((mapping.source_make, mapping.source_model), mapping)
            normalized_make = normalize_mapping_text(mapping.source_make).casefold()
            self._device_types_by_make.setdefault(normalized_make, []).append(mapping)
        self._manufacturers_exact = {}
        for mapping in self.manufacturer_mappings:
            normalized_make = normalize_mapping_text(mapping.source_make).casefold()
            self._manufacturers_exact.setdefault(normalized_make, mapping)
        self.mapped_source_makes = frozenset(self._manufacturers_exact)

    @classmethod
    def for_profile(cls, profile):
        """Load both mapping tables once for one import run."""
        return cls(
            profile.device_type_mappings.all(),
            profile.manufacturer_mappings.all(),
        )

    def resolve(self, make: str, model: str) -> tuple[str, str, bool]:
        """Return manufacturer slug, Device Type slug, and explicit status."""
        normalized_make = normalize_mapping_text(make)
        normalized_model = normalize_mapping_text(model)
        mapping = self._device_types_exact.get((make, model))
        if mapping is None:
            mapping = next(
                (
                    candidate
                    for candidate in self._device_types_by_make.get(normalized_make.casefold(), ())
                    if normalize_mapping_text(candidate.source_model).casefold() == normalized_model.casefold()
                ),
                None,
            )
        if mapping is not None:
            return mapping.netbox_manufacturer_slug, mapping.netbox_device_type_slug, True

        manufacturer_mapping = self._manufacturers_exact.get(normalized_make.casefold())
        manufacturer_slug = (
            manufacturer_mapping.netbox_manufacturer_slug
            if manufacturer_mapping is not None
            else slugify(normalized_make)[:50]
        )
        return manufacturer_slug, slugify(f"{normalized_make}-{normalized_model}")[:50], False


__all__ = (
    "DeviceIdentityMatch",
    "DeviceTypeIdentityResolver",
    "normalize_mapping_text",
    "resolve_strong_device_identity",
)
