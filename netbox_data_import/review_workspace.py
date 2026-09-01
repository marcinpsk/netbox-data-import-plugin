# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Present an Import Plan and resolve explicit review commands."""

from __future__ import annotations

from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any

from .plan import Disposition, ImportPlan, Severity, SynchronizationUnit
from .values import (
    effective_device_name,
    has_below_rack_position,
    identity_text,
    normalize_for_compare,
    source_position,
    source_text,
    translation_maps,
)


_DIAGNOSTIC_MESSAGES = {
    "device.add_permission": "Permission denied: dcim.add_device",
    "device.already_bound": "Another source row is already linked to this device.",
    "device.ambiguous_asset_tag": "Multiple devices have this asset tag.",
    "device.ambiguous_field_review": "Saved field reviews identify more than one device.",
    "device.ambiguous_name": "Multiple devices have this name.",
    "device.ambiguous_serial": "Multiple devices have this serial number.",
    "device.ambiguous_stored_source_id": "More than one device stores this source ID.",
    "device.contact_invalid": "The primary contact selection is invalid.",
    "device.contact_resolution_required": "Choose the primary contact values before importing.",
    "device.class_ignored": "Ignored source class",
    "device.class_unmapped": "No class-to-role mapping exists for this source class.",
    "device.cross_site_match": "A strong identity matches a device at another site.",
    "device.device_type_missing": "The device type does not exist. Map or create it before importing.",
    "device.device_type_permission": "Permission denied: dcim.add_devicetype",
    "device.device_type_slug_collision": "A stored device type already uses the slug this model derives.",
    "device.manufacturer_permission": "Permission denied: dcim.add_manufacturer",
    "device.manufacturer_slug_collision": "A stored manufacturer already uses the slug this make derives.",
    "device.derived_slug_collision": "Different source identities derive the same dependency slug.",
    "device.duplicate_asset_tag": "The asset tag appears more than once in this import.",
    "device.duplicate_name": "The device name appears more than once in this import.",
    "device.duplicate_serial": "The serial number appears more than once in this import.",
    "device.duplicate_source_id": "The source ID appears more than once in this import.",
    "device.face_required": "A rack position needs a device face.",
    "device.ignored": "Ignored device",
    "device.inaccessible_match": "Permission denied: dcim.view_device",
    "device.below_rack": "Skipped because the source position is below rack unit one.",
    "device.change_permission": "Permission denied: dcim.change_device",
    "device.missing_name": "Missing device name",
    "device.name_placement_conflict": "The name matches a device at another placement.",
    "device.rack_ambiguous": "Multiple racks have this name at the import target.",
    "device.rack_missing": "The target rack does not exist.",
    "device.rack_position_claimed": "Another row claims this rack position.",
    "device.rack_position_occupied": "The rack position is occupied.",
    "device.rack_required": "A rack position needs a target rack.",
    "device.role_unconfigured": "No device role is configured for this source class.",
    "device.zero_u_review_conflict": "A saved review keeps a rack position on a 0U device type.",
    "rack.add_permission": "Permission denied: dcim.add_rack",
    "rack.change_permission": "Permission denied: dcim.change_rack",
    "rack.duplicate_name": "The rack name appears more than once in this import.",
    "rack.duplicate_source_id": "The source ID appears more than once in this import.",
    "rack.ignored": "Ignored rack",
    "rack.missing_name": "Missing rack name",
    "rack.ambiguous_name": "Multiple racks have this name at the import target.",
    "rack.validation_failed": "The planned rack does not pass NetBox validation.",
}

_IDENTITY_CONFLICTS = {
    "device.already_bound": "device_already_bound",
    "device.ambiguous_asset_tag": "ambiguous_asset_tag",
    "device.ambiguous_field_review": "ambiguous_field_review",
    "device.ambiguous_name": "ambiguous_name",
    "device.ambiguous_serial": "ambiguous_serial",
    "device.ambiguous_stored_source_id": "ambiguous_source_id",
    "device.cross_site_match": "cross_site_match",
    "device.derived_slug_collision": "derived_slug_collision",
    "device.duplicate_asset_tag": "duplicate_asset_tag",
    "device.duplicate_name": "duplicate_name",
    "device.duplicate_serial": "duplicate_serial",
    "device.duplicate_source_id": "duplicate_source_id",
    "device.inaccessible_match": "permission_denied",
    "device.name_placement_conflict": "name_placement_conflict",
    "device.rack_position_claimed": "rack_position_occupied",
    "device.rack_position_occupied": "rack_position_occupied",
    "rack.ambiguous_name": "ambiguous_rack",
    "rack.duplicate_name": "duplicate_rack",
    "rack.duplicate_source_id": "duplicate_source_id",
    "rack.validation_failed": "rack_validation_failed",
}


def _operation(unit: SynchronizationUnit) -> str:
    """Return the preview action one unit displays."""
    if unit.disposition == Disposition.ACTIONABLE:
        return unit.changes[-1].operation if unit.changes else "update"
    if unit.disposition == Disposition.NO_OP:
        return "skip"
    if unit.disposition == Disposition.EXCLUDED:
        return "ignore"
    return "error"


def _object_type(unit: SynchronizationUnit) -> str:
    """Return the target type one unit represents."""
    if unit.changes:
        return unit.changes[-1].target_module
    return unit.identity.partition(":")[0]


def _blocking(unit: SynchronizationUnit) -> list:
    """Return the unit's error diagnostics: the first states the row, the rest are what it needs."""
    return [item for item in unit.diagnostics if item.severity == Severity.ERROR]


def _diagnostic_message(diagnostic) -> str:
    """Return the operator wording for one diagnostic."""
    return str(diagnostic.display.get("message") or "") or _DIAGNOSTIC_MESSAGES.get(diagnostic.code, diagnostic.code)


def _detail(unit: SynchronizationUnit, action: str, object_type: str, name: str) -> str:
    """Return stable operator wording for one unit."""
    if unit.diagnostics:
        blocking = _blocking(unit)
        diagnostic = blocking[0] if blocking else unit.diagnostics[0]
        message = diagnostic.display.get("message")
        if message:
            return str(message)
        return _DIAGNOSTIC_MESSAGES.get(diagnostic.code, diagnostic.code)
    if detail := unit.display.get("detail"):
        return str(detail)
    verb = {
        "create": "Would create",
        "update": "Would update",
        "skip": "No changes for",
        "ignore": "Ignored",
    }.get(action, action.title())
    return f"{verb} {object_type} '{name}'"


@dataclass(frozen=True)
class WorkspaceUnit:
    """View-supplied presentation data for one Synchronization Unit."""

    identity: str
    disposition: str
    row_number: int | None
    source_id: str
    name: str
    action: str
    object_type: str
    detail: str
    rack_name: str
    netbox_url: str
    extra_data: dict[str, Any]
    source_row: dict[str, Any]

    @classmethod
    def from_unit(cls, unit: SynchronizationUnit) -> WorkspaceUnit:
        """Build presentation data without changing the accepted plan."""
        unit_data = unit.to_dict()
        display = unit_data["display"]
        source_row = dict(display.get("source_row") or {})
        object_type = _object_type(unit)
        action = _operation(unit)
        name = str(display.get("name") or display.get("device_name") or display.get("rack_name") or "")
        rack_name = str(display.get("rack_name") or source_row.get("rack_name") or "")
        source_id = str(display.get("source_id") or source_row.get("source_id") or "")
        row_number = display.get("row_number", source_row.get("_row_number"))
        extra_data = dict(display.get("extra_data") or {})
        for key, value in source_row.items():
            if key.startswith("_"):
                continue
            extra_data.setdefault(f"source_{key}", value)
        if unit.changes:
            change = unit.changes[-1]
            change_data = change.to_dict()
            extra_data.update({key: value for key, value in change_data["payload"].items() if key not in extra_data})
            object_id = change.preconditions.get(f"{object_type}_id")
            if object_id is not None:
                extra_data.setdefault(f"netbox_{object_type}_id", object_id)
            state = change.preconditions.get("state")
            if state is not None:
                extra_data.setdefault("_identity_state", dict(state))
        if unit.diagnostics:
            diagnostic = unit.diagnostics[0]
            diagnostic_display = diagnostic.to_dict()["display"]
            structural = {"device_name", "extra_data", "message", "name", "object_type", "source_row"}
            extra_data.update({key: value for key, value in diagnostic_display.items() if key not in structural})
            if conflict := _IDENTITY_CONFLICTS.get(diagnostic.code):
                extra_data.setdefault("identity_conflict", conflict)
            rows = diagnostic_display.get("rows")
            if rows:
                row_key = {
                    "device.duplicate_asset_tag": "duplicate_asset_tag_rows",
                    "device.duplicate_serial": "duplicate_serial_rows",
                    "device.duplicate_source_id": "duplicate_source_id_rows",
                    "rack.duplicate_source_id": "duplicate_source_id_rows",
                }.get(diagnostic.code)
                if row_key:
                    extra_data[row_key] = [number for number in rows if number != row_number]
            # The first error states the row; the rest are what it still needs. A warning is neither.
            blocking = _blocking(unit)
            extra_data["other_issues"] = [
                {"code": item.code, "message": _diagnostic_message(item)} for item in blocking[1:]
            ]
            # A row action reads these facts, so a second problem is settled like a first one.
            extra_data["identity_conflicts"] = [
                conflict for item in blocking if (conflict := _IDENTITY_CONFLICTS.get(item.code))
            ]
            for item in blocking[1:]:
                secondary = item.to_dict()["display"]
                for key, value in secondary.items():
                    if key not in structural:
                        extra_data.setdefault(key, value)
                for key, value in (secondary.get("extra_data") or {}).items():
                    extra_data.setdefault(key, value)
        return cls(
            identity=unit.identity,
            disposition=unit.disposition,
            row_number=row_number,
            source_id=source_id,
            name=name,
            action=action,
            object_type=object_type,
            detail=_detail(unit, action, object_type, name),
            rack_name=rack_name,
            netbox_url=str(display.get("netbox_url") or ""),
            extra_data=extra_data,
            source_row=source_row,
        )


@dataclass(frozen=True)
class AutoMatchSummary:
    """Counts returned by the device auto-match review command."""

    matched: int = 0
    probable: int = 0
    ambiguous: int = 0
    placement_conflicts: int = 0
    already: int = 0
    skipped: int = 0

    def message(self) -> str:
        """Return the existing operator-facing summary."""
        parts = []
        if self.matched:
            parts.append(f"{self.matched} auto-matched (serial/asset_tag/name)")
        if self.probable:
            parts.append(f"{self.probable} probable name match(es): use Link button to confirm")
        if self.ambiguous:
            parts.append(f"{self.ambiguous} ambiguous (multiple devices)")
        if self.placement_conflicts:
            parts.append(f"{self.placement_conflicts} placement conflict(s)")
        if self.already:
            parts.append(f"{self.already} already matched")
        if self.skipped:
            parts.append(f"{self.skipped} skipped (permission denied or concurrent change)")
        return f"Auto-match: {', '.join(parts) or 'nothing found'}."


def _resolve_strong_identity(devices, serial: str, asset_tag: str):
    """Resolve serial and asset tag to one device, or report ambiguity."""
    serial_matches = list(devices.filter(serial=serial)[:2]) if serial else []
    asset_matches = list(devices.filter(asset_tag__iexact=asset_tag)[:2]) if asset_tag else []
    if len(serial_matches) > 1 or len(asset_matches) > 1:
        return None, None, True
    serial_device = serial_matches[0] if serial_matches else None
    asset_device = asset_matches[0] if asset_matches else None
    if serial_device is not None and asset_device is not None and serial_device.pk != asset_device.pk:
        return None, None, True
    if serial_device is not None:
        return serial_device, "serial", False
    if asset_device is not None:
        return asset_device, "asset tag", False
    return None, None, False


def _match_existing_device(device_model, visible_devices, name, serial, asset_tag, site, tenant_id):
    """Return one exact device match and its method, or report ambiguity."""
    device, method, ambiguous = _resolve_strong_identity(device_model.objects, serial, asset_tag)
    if ambiguous:
        return None, None, True
    if device is None and name:
        tenant_filter = {"tenant_id": tenant_id} if tenant_id is not None else {"tenant__isnull": True}
        matches = list(device_model.objects.filter(site=site, name__iexact=name, **tenant_filter)[:2])
        if len(matches) > 1:
            return None, None, True
        if matches:
            device, method = matches[0], "name"
    if device is not None and (device.site_id != site.pk or not visible_devices.filter(pk=device.pk).exists()):
        return None, None, True
    return device, method, False


def _device_placement_differs(device, source_location_id, rack_name, position, face) -> bool:
    """Return whether source placement differs from one matched NetBox device."""
    device_rack_name = device.rack.name if device.rack_id else ""
    device_rack_location_id = device.rack.location_id if device.rack_id else None
    return (
        device.location_id != source_location_id
        or (device.rack_id is not None and device_rack_location_id != source_location_id)
        or identity_text(device_rack_name) != identity_text(rack_name)
        or normalize_for_compare(device.position) != normalize_for_compare(position)
        or (face is not None and (device.face or None) != face)
    )


class ReviewWorkspace:
    """Read-only presentation of the accepted Import Plan."""

    def __init__(self, plan: ImportPlan):
        self.plan = plan
        self.units = tuple(WorkspaceUnit.from_unit(unit) for unit in plan.units)

    @classmethod
    def from_dict(cls, data: dict) -> ReviewWorkspace:
        """Restore a workspace from the session's serialized Import Plan."""
        return cls(ImportPlan.from_dict(data))

    @property
    def counts(self) -> MappingProxyType:
        """Return preview counts in the existing template vocabulary."""
        counts: dict[str, int] = {}
        for unit in self.units:
            if unit.action == "error":
                key = "errors"
            elif unit.action == "skip":
                key = "skipped"
            elif unit.action == "ignore":
                key = "ignored"
            elif unit.action in {"create", "update"}:
                key = f"{unit.object_type}s_{unit.action}d"
            else:
                continue
            counts[key] = counts.get(key, 0) + 1
        return MappingProxyType(counts)

    @property
    def has_errors(self) -> bool:
        """Return whether any unit cannot execute."""
        return any(unit.action == "error" for unit in self.units)

    @property
    def rack_groups(self) -> dict:
        """Group Rack and Device units for the existing rack-card view."""
        groups: dict[str, dict[str, Any]] = {}
        for unit in self.units:
            if unit.object_type == "rack":
                if not unit.name:
                    continue
                groups.setdefault(unit.name, {"rack_row": None, "devices": []})["rack_row"] = unit
            elif unit.object_type == "device":
                groups.setdefault(unit.rack_name or "(No rack)", {"rack_row": None, "devices": []})["devices"].append(
                    unit
                )

        def placement_sort_key(unit):
            position = source_position(unit.extra_data.get("u_position"))
            return position is None, position or 0

        for group in groups.values():
            group["devices"].sort(key=placement_sort_key)
        return groups

    @property
    def source_rows(self) -> list[dict[str, Any]]:
        """Return the source rows carried as display data, once per row number."""
        rows: dict[int, dict[str, Any]] = {}
        for unit in self.units:
            if unit.row_number is not None and unit.source_row:
                rows.setdefault(unit.row_number, dict(unit.source_row))
        return [rows[number] for number in sorted(rows)]

    @property
    def unused_columns(self) -> list[dict[str, Any]]:
        """Return unmapped source columns carried by plan diagnostics."""
        columns = [
            dict(diagnostic.display)
            for diagnostic in self.plan.diagnostics
            if diagnostic.code.endswith(".unused_column")
        ]
        columns.sort(key=lambda column: -int(column.get("count") or 0))
        return columns

    def auto_match_devices(self, profile, actor, target) -> AutoMatchSummary:  # noqa: C901
        """Save safe exact device matches for every eligible plan source row."""
        from django.core.exceptions import ValidationError
        from django.db import IntegrityError
        from dcim.models import Device

        from .models import DeviceExistingMatch
        from .object_permissions import ObjectPermissionDenied, save_permission_scoped_object

        site = target["site"]
        tenant_id = target["tenant"].pk if target["tenant"] else None
        visible_devices = Device.objects.restrict(actor, "view")
        ignored_source_ids = set(profile.ignored_devices.values_list("source_id", flat=True))
        class_mappings = {mapping.source_class: mapping for mapping in profile.class_role_mappings.all()}
        eligible_rows = []
        for row in self.source_rows:
            source_id = source_text(row.get("source_id"))
            mapping = class_mappings.get(source_text(row.get("device_class")))
            if (
                mapping is None
                or mapping.creates_rack
                or mapping.ignore
                or source_id in ignored_source_ids
                or has_below_rack_position(row)
            ):
                continue
            eligible_rows.append(row)

        bound_device_by_source = dict(profile.device_matches.values_list("source_id", "netbox_device_id"))
        bound_source_by_device = {device_id: source for source, device_id in bound_device_by_source.items()}
        source_counts: dict[str, int] = {}
        name_counts: dict[str, int] = {}
        serial_counts: dict[str, int] = {}
        asset_tag_counts: dict[str, int] = {}
        for row in eligible_rows:
            values = (
                (source_text(row.get("source_id")), source_counts),
                (identity_text(effective_device_name(row)), name_counts),
                (source_text(row.get("serial")), serial_counts),
                (identity_text(source_text(row.get("asset_tag"))[:50]), asset_tag_counts),
            )
            for value, counts in values:
                if value:
                    counts[value] = counts.get(value, 0) + 1

        counts = {
            "matched": 0,
            "probable": 0,
            "ambiguous": 0,
            "placement_conflicts": 0,
            "already": 0,
            "skipped": 0,
        }
        side_map, _, _ = translation_maps()
        for row in eligible_rows:
            source_id = source_text(row.get("source_id"))
            name = effective_device_name(row)
            serial = source_text(row.get("serial"))
            asset_tag = source_text(row.get("asset_tag"))[:50]
            if not source_id:
                continue
            if source_counts.get(source_id, 0) > 1:
                counts["ambiguous"] += 1
                continue
            if source_id in bound_device_by_source:
                counts["already"] += 1
                continue

            device, method, ambiguous = _match_existing_device(
                Device,
                visible_devices,
                name if name_counts.get(identity_text(name), 0) == 1 else "",
                serial if serial_counts.get(serial, 0) == 1 else "",
                asset_tag if asset_tag_counts.get(identity_text(asset_tag), 0) == 1 else "",
                site,
                tenant_id,
            )
            if ambiguous:
                counts["ambiguous"] += 1
                continue
            if device is not None and method == "name":
                face = side_map.get(source_text(row.get("face")).lower())
                if _device_placement_differs(
                    device,
                    target["location"].pk if target["location"] else None,
                    source_text(row.get("rack_name")),
                    source_position(row.get("u_position")),
                    face,
                ):
                    counts["placement_conflicts"] += 1
                    continue
            if device is not None:
                bound_source = bound_source_by_device.get(device.pk)
                if bound_source is not None and bound_source != source_id:
                    counts["ambiguous"] += 1
                    continue
                try:
                    save_permission_scoped_object(
                        actor,
                        DeviceExistingMatch,
                        {"profile": profile, "source_id": source_id},
                        {
                            "netbox_device_id": device.pk,
                            "device_name": device.name,
                            "source_asset_tag": asset_tag,
                        },
                        on_existing="reject",
                    )
                except (ValidationError, IntegrityError, ObjectPermissionDenied):
                    counts["skipped"] += 1
                    continue
                bound_device_by_source[source_id] = device.pk
                bound_source_by_device[device.pk] = source_id
                counts["matched"] += 1
                continue
            if name:
                short_name = name.split(" - ")[-1].strip() if " - " in name else name
                tenant_filter = {"tenant_id": tenant_id} if tenant_id is not None else {"tenant__isnull": True}
                if visible_devices.filter(site=site, name__icontains=short_name, **tenant_filter).exists():
                    counts["probable"] += 1
        return AutoMatchSummary(**counts)

    def with_units(self, units) -> ReviewWorkspace:
        """Return a presentation-only copy with replaced units."""
        workspace = object.__new__(type(self))
        workspace.plan = self.plan
        workspace.units = tuple(units)
        return workspace

    @staticmethod
    def replace_unit(unit: WorkspaceUnit, **values) -> WorkspaceUnit:
        """Return a presentation-only copy of one unit."""
        return replace(unit, **values)


__all__ = ("AutoMatchSummary", "ReviewWorkspace", "WorkspaceUnit")
