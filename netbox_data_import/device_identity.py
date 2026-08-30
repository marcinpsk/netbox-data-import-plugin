# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Resolve the NetBox Device Type identity from source make and model names."""

from __future__ import annotations

import re

from django.utils.text import slugify


def normalize_mapping_text(value: str) -> str:
    r"""Normalize whitespace and decode JavaScript-style \uXXXX escapes."""
    value = re.sub(r"\\u([0-9a-fA-F]{4})", lambda match: chr(int(match.group(1), 16)), value)
    return " ".join(value.split())


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


__all__ = ("DeviceTypeIdentityResolver", "normalize_mapping_text")
