# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Resolve the NetBox Device Type identity from source make and model names."""

from __future__ import annotations

import re

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


class DeviceTypeIdentityResolver:
    """Resolve all profile Device Type identities from two batch-loaded indexes."""

    def __init__(self, device_type_mappings, manufacturer_mappings):
        self.device_type_mappings = tuple(device_type_mappings)
        self.manufacturer_mappings = tuple(manufacturer_mappings)
        self._device_types_exact = {}
        self._device_types_by_make = {}
        for mapping in self.device_type_mappings:
            self._device_types_exact.setdefault((mapping.source_make, mapping.source_model), mapping)
            self._device_types_by_make.setdefault(mapping_identity(mapping.source_make), []).append(mapping)
        self._manufacturers_exact = {}
        for mapping in self.manufacturer_mappings:
            self._manufacturers_exact.setdefault(mapping_identity(mapping.source_make), mapping)

    @classmethod
    def for_profile(cls, profile):
        """Load both mapping tables once for one import run."""
        return cls(
            profile.device_type_mappings.all(),
            profile.manufacturer_mappings.all(),
        )

    def resolve(self, make: str, model: str) -> tuple[str, str, bool]:
        """Return manufacturer slug, Device Type slug, and explicit status."""
        mapping = self._device_types_exact.get((make, model))
        if mapping is None:
            mapping = next(
                (
                    candidate
                    for candidate in self._device_types_by_make.get(mapping_identity(make), ())
                    if mapping_identity(candidate.source_model) == mapping_identity(model)
                ),
                None,
            )
        if mapping is not None:
            return mapping.netbox_manufacturer_slug, mapping.netbox_device_type_slug, True

        manufacturer_mapping = self._manufacturers_exact.get(mapping_identity(make))
        default_manufacturer_slug, default_device_type_slug = default_identity_slugs(make, model)
        manufacturer_slug = (
            manufacturer_mapping.netbox_manufacturer_slug
            if manufacturer_mapping is not None
            else default_manufacturer_slug
        )
        return manufacturer_slug, default_device_type_slug, False


__all__ = ("DeviceTypeIdentityResolver", "default_identity_slugs", "normalize_mapping_text")
