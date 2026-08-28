# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Target Module runtime protocol, and the Rack module that implements it.

Section 2.3 gives a Target Module target-specific matching, ORM queries, permission checks,
preconditions, locking and writes. It plans against the complete relevant Source Batch and applies
one Planned Change at a time. It never commits a transaction and never calls another module.

`catalog.TargetModule` stays the static declaration a profile derives its Target Fields from. This
module is the behaviour behind that declaration.
"""

from __future__ import annotations

from typing import Any, Protocol

from .catalog import OutputKind
from .plan import Diagnostic, Disposition, PlannedChange, Severity, SynchronizationUnit
from .values import normalize_for_compare

DEFAULT_RACK_HEIGHT = 42


class TargetModuleRuntime(Protocol):
    """What the coordinator may call on a Target Module."""

    key: str
    consumes: frozenset[str]

    def plan(self, source_batch, profile, catalog, netbox_reader) -> list[SynchronizationUnit]:
        """Return the Synchronization Units this module owns for the whole batch."""
        ...

    def apply(self, planned_change: PlannedChange, execution_context) -> Any:
        """Apply one Planned Change inside the coordinator's transaction."""
        ...


def _text(value) -> str:
    """Return the trimmed text of a source value, empty for None."""
    return "" if value is None else str(value).strip()


def _identity_text(value) -> str:
    """Return the case-insensitive comparison form of a name."""
    return _text(value).casefold()


def _coerce_height(value) -> int:
    """Return the rack height the row asks for, never below one unit."""
    try:
        return max(1, int(float(_text(value) or DEFAULT_RACK_HEIGHT)))
    except (TypeError, ValueError):
        return DEFAULT_RACK_HEIGHT


class RackModule:
    """Plans and applies the Racks a flat source batch describes."""

    key = "rack"
    consumes = frozenset({OutputKind.RACK_SOURCE_ROW})

    def plan(self, source_batch, profile, catalog, netbox_reader) -> list[SynchronizationUnit]:
        """Return one Synchronization Unit per rack row, with the disposition its state earns."""
        rows = self._rack_rows(source_batch, profile)
        if not rows:
            return []
        ignored = self._ignored_source_ids(profile)
        duplicate_names = self._repeated(_identity_text(row.get("rack_name")) or "" for row in rows)
        duplicate_source_ids = self._repeated(_text(row.get("source_id")) for row in rows)
        existing = self._existing_by_name(netbox_reader)
        return [
            self._unit(row, netbox_reader, ignored, duplicate_names, duplicate_source_ids, existing) for row in rows
        ]

    def _rack_rows(self, source_batch, profile) -> list[dict]:
        """Return the batch rows whose class the profile maps to a rack."""
        creates_rack = {
            mapping.source_class
            for mapping in profile.class_role_mappings.all()
            if mapping.creates_rack and not mapping.ignore
        }
        return [row for row in source_batch.rows if _text(row.get("device_class")) in creates_rack]

    @staticmethod
    def _ignored_source_ids(profile) -> frozenset[str]:
        """Return the source identities the operator has chosen to skip."""
        return frozenset(_text(value) for value in profile.ignored_devices.values_list("source_id", flat=True))

    @staticmethod
    def _repeated(values) -> frozenset[str]:
        """Return the non-empty values that appear more than once in one batch."""
        seen: set[str] = set()
        repeated: set[str] = set()
        for value in values:
            if not value:
                continue
            if value in seen:
                repeated.add(value)
            seen.add(value)
        return frozenset(repeated)

    @staticmethod
    def _existing_by_name(netbox_reader) -> dict[str, Any]:
        """Return the racks the actor may view at the import site, keyed by comparison name."""
        if netbox_reader.site is None:
            return {}
        racks = netbox_reader.racks().filter(site=netbox_reader.site)
        if netbox_reader.location is not None:
            racks = racks.filter(location=netbox_reader.location)
        return {_identity_text(rack.name): rack for rack in racks}

    @staticmethod
    def unit_identity(row) -> str:
        """Return the identity that survives replanning, which is never the row number."""
        source_id = _text(row.get("source_id"))
        if source_id:
            return f"rack:source:{source_id}"
        return f"rack:name:{_identity_text(row.get('rack_name'))}"

    def _unit(self, row, netbox_reader, ignored, duplicate_names, duplicate_source_ids, existing):
        """Return the one unit this row produces."""
        identity = self.unit_identity(row)
        name = _text(row.get("rack_name")) or _text(row.get("device_name"))
        source_id = _text(row.get("source_id"))

        if not name:
            return self._refused(identity, "rack.missing_name", {"source_id": source_id})
        if source_id and source_id in ignored:
            return SynchronizationUnit(
                identity=identity,
                disposition=Disposition.EXCLUDED,
                diagnostics=(
                    Diagnostic(
                        code="rack.ignored",
                        severity=Severity.INFO,
                        identities=(identity,),
                        display={"rack_name": name, "source_id": source_id},
                    ),
                ),
            )
        if _identity_text(name) in duplicate_names:
            return self._refused(identity, "rack.duplicate_name", {"rack_name": name, "source_id": source_id})
        if source_id and source_id in duplicate_source_ids:
            return self._refused(identity, "rack.duplicate_source_id", {"rack_name": name, "source_id": source_id})

        height = _coerce_height(row.get("u_height"))
        serial = _text(row.get("serial"))
        rack = existing.get(_identity_text(name))
        if rack is None:
            return SynchronizationUnit(
                identity=identity,
                disposition=Disposition.ACTIONABLE,
                changes=(self._change(identity, "create", name, height, serial, netbox_reader, None),),
            )

        if not self._differs(rack, height, serial):
            return SynchronizationUnit(identity=identity, disposition=Disposition.NO_OP)
        return SynchronizationUnit(
            identity=identity,
            disposition=Disposition.ACTIONABLE,
            changes=(self._change(identity, "update", name, height, serial, netbox_reader, rack),),
        )

    @staticmethod
    def _differs(rack, height: int, serial: str) -> bool:
        """Return whether the stored rack already matches what the row asks for."""
        if normalize_for_compare(rack.u_height) != normalize_for_compare(height):
            return True
        return bool(serial) and _text(rack.serial) != serial

    @staticmethod
    def _change(identity, operation, name, height, serial, netbox_reader, rack) -> PlannedChange:
        """Return the one write this unit performs, with the target state it assumed."""
        payload = {
            "name": name,
            "u_height": height,
            "serial": serial,
            "site_id": netbox_reader.site.pk if netbox_reader.site is not None else None,
            "location_id": netbox_reader.location.pk if netbox_reader.location is not None else None,
            "tenant_id": netbox_reader.tenant.pk if netbox_reader.tenant is not None else None,
        }
        preconditions = {"rack_id": rack.pk, "u_height": rack.u_height} if rack is not None else {"rack_id": None}
        return PlannedChange(
            identity=f"{identity}:{operation}",
            target_module=RackModule.key,
            operation=operation,
            payload=payload,
            preconditions=preconditions,
        )

    @staticmethod
    def _refused(identity, code, display) -> SynchronizationUnit:
        """Return an invalid unit carrying the error that refused it."""
        return SynchronizationUnit(
            identity=identity,
            disposition=Disposition.INVALID,
            diagnostics=(Diagnostic(code=code, severity=Severity.ERROR, identities=(identity,), display=display),),
        )


__all__ = ("DEFAULT_RACK_HEIGHT", "RackModule", "TargetModuleRuntime")
