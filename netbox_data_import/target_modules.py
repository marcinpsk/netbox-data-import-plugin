# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The Rack, Device and Cable Target Modules, and the registry the coordinator resolves them through.

Section 2.3 gives a Target Module target-specific matching, ORM queries, permission checks,
preconditions, locking and writes. It plans against the complete relevant Source Batch and applies
one Planned Change at a time. It never commits a transaction and never calls another module.

`catalog.TargetModule` stays the static declaration a profile derives its Target Fields from. This
module is the behaviour behind that declaration.
"""

from __future__ import annotations

import datetime
import math
import re
from copy import copy
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError

from . import ip_assignment
from .cable_target import CableModule
from .catalog import OutputKind
from .contact_resolution import (
    ContactResolutionRequired,
    ContactReview,
    ContactSelection,
    DanglingProfileReference,
    PrimaryContactResolver,
)
from .device_field_review import DeviceFieldReviewer
from .device_identity import DeviceTypeIdentityResolver
from .netbox_reader import PlanningTargetUnavailable
from .object_permissions import ObjectPermissionDenied
from .plan import Diagnostic, Disposition, PlannedChange, Severity, SynchronizationUnit
from .target_runtime import ExecutionContext, PreconditionFailed, TargetModuleRuntime
from .values import (
    effective_device_name,
    identity_text,
    normalize_for_compare,
    source_position,
    source_text,
    translation_maps,
)

DEFAULT_RACK_HEIGHT = 42


def _text(value) -> str:
    """Return stripped stored or target text, empty for None."""
    return "" if value is None else str(value).strip()


def _source_text(value) -> str:
    """Return source text, including an empty value for spreadsheet null markers."""
    return source_text(value)


def _database_upper_values(values, *, collation: str | None = None) -> dict[str, str]:
    """Return PostgreSQL's case-insensitive comparison key for each distinct value."""
    from django.db import connection

    unique_values = sorted(set(values))
    if not unique_values:
        return {}
    collation_sql = f" COLLATE {connection.ops.quote_name(collation)}" if collation else ""
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT source_value, UPPER(source_value{collation_sql}) FROM unnest(%s::text[]) AS source_value",
            [unique_values],
        )
        return dict(cursor.fetchall())


def _duplicate_value_detail(label: str, value: str, other_rows: list[int]) -> str:
    """Name a duplicated identity value and every other source row that carries it."""
    where = ", ".join(f"row {number}" for number in other_rows)
    return f"Duplicate {label} '{value}' appears more than once in this import" + (
        f", also on {where}." if where else "."
    )


def _display_value(value):
    """Return detached JSON display data for a source value."""
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            return str(value)
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (datetime.date, datetime.datetime, datetime.time)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _display_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_display_value(item) for item in value]
    return str(value)


def _unit_display(row, object_type: str, name: str, rack_name: str = "") -> dict:
    """Return source and presentation facts shared by all unit dispositions."""
    source_row = {str(key): _display_value(value) for key, value in row.items()}
    return {
        "row_number": row.get("_row_number"),
        "source_id": _source_text(row.get("source_id")),
        "name": name,
        "rack_name": rack_name,
        "source_row": source_row,
        "extra_data": {
            "asset_tag": _source_text(row.get("asset_tag"))[:50],
            "candidate_values": _display_value(row.get("_candidate_values") or {}),
            "conflicts": _display_value(row.get("_conflicts") or {}),
            "extra_columns": _display_value(row.get("_extra_columns") or {}),
            "source_class": _source_text(row.get("device_class")),
            "source_make": _source_text(row.get("make")),
            "source_model": _source_text(row.get("model")),
            "source_serial": _source_text(row.get("serial")),
        },
        "object_type": object_type,
    }


def _class_mapping_display(mapping) -> dict:
    """Return the stored class policy, so its editor reopens on what the operator saved."""
    if mapping is None:
        return {"class_mapping_action": "", "class_mapping_role_slug": ""}
    if mapping.ignore:
        action = "ignore"
    elif mapping.creates_rack:
        action = "rack"
    else:
        action = "role"
    return {"class_mapping_action": action, "class_mapping_role_slug": _text(mapping.role_slug)}


def _ignored_source_ids(profile) -> frozenset[str]:
    """Return the source identities the operator has chosen to skip."""
    return frozenset(_source_text(value) for value in profile.ignored_devices.values_list("source_id", flat=True))


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


def _translate(value, table) -> str:
    """Return the NetBox value a source word names, or the word itself when it is already one."""
    text = _source_text(value).lower()
    if not text:
        return ""
    mapped = table.get(text)
    if mapped is not None:
        return mapped
    return text if text in set(table.values()) else ""


def _coerce_rack_height(value) -> int:
    """Return the rack height the row asks for, never below one unit."""
    try:
        return max(1, int(float(_source_text(value) or DEFAULT_RACK_HEIGHT)))
    except (OverflowError, TypeError, ValueError):
        return DEFAULT_RACK_HEIGHT


def rack_row_name(row) -> str:
    """Return the rack name one rack row carries."""
    return _source_text(row.get("rack_name")) or _source_text(row.get("device_name"))


def rack_unit_identity(row) -> str:
    """Return the stable Synchronization Unit identity for one rack row."""
    source_id = _source_text(row.get("source_id"))
    if source_id:
        return f"rack:source:{source_id}"
    return f"rack:name:{identity_text(rack_row_name(row))}"


def rack_duplicate_keys(rows) -> tuple[frozenset[str], frozenset[str]]:
    """Return the rack names and source IDs more than one row in this batch claims."""
    return (
        _repeated(identity_text(rack_row_name(row)) for row in rows),
        _repeated(_source_text(row.get("source_id")) for row in rows),
    )


def rack_row_rejection(row, ignored, duplicate_names, duplicate_source_ids) -> tuple[str, dict] | None:
    """Keep the rack-pass rejection order, including its empty-ID ignore match, for cutover parity."""
    name = rack_row_name(row)
    source_id = _source_text(row.get("source_id"))
    if not name:
        return "rack.missing_name", {"source_id": source_id}
    if source_id in ignored:
        return "rack.ignored", {"rack_name": name, "source_id": source_id}
    if identity_text(name) in duplicate_names:
        return "rack.duplicate_name", {"rack_name": name, "source_id": source_id}
    if source_id and source_id in duplicate_source_ids:
        return "rack.duplicate_source_id", {"rack_name": name, "source_id": source_id}
    return None


def _racks_by_comparison_name(netbox_reader) -> dict[str, list[Any]]:
    """Return visible racks at the exact import target, grouped by comparison name."""
    if netbox_reader.site is None:
        return {}
    location_filter = (
        {"location": netbox_reader.location} if netbox_reader.location is not None else {"location__isnull": True}
    )
    grouped: dict[str, list[Any]] = {}
    for rack in netbox_reader.racks().filter(site=netbox_reader.site, **location_filter):
        grouped.setdefault(identity_text(rack.name), []).append(rack)
    return grouped


class RackModule:
    """Plans and applies the Racks a flat source batch describes."""

    key = "rack"
    consumes = frozenset({OutputKind.RACK_SOURCE_ROW})

    def plan(
        self,
        source_batch,
        profile,
        catalog,
        netbox_reader,
        *,
        lock_plan_references: bool = False,
    ) -> list[SynchronizationUnit]:
        """Return one Synchronization Unit per rack row, with the disposition its state earns."""
        # Planned Change preconditions carry every target row this module depends on, so no read-only reference remains.
        del lock_plan_references
        rows = self._rack_rows(source_batch, profile)
        if not rows:
            return []
        ignored = _ignored_source_ids(profile)
        duplicate_names, duplicate_source_ids = rack_duplicate_keys(rows)
        existing = _racks_by_comparison_name(netbox_reader)
        mappings = {mapping.source_class: mapping for mapping in profile.class_role_mappings.all()}
        return [
            self._unit(
                row,
                profile,
                mappings[_source_text(row.get("device_class"))],
                netbox_reader,
                ignored,
                duplicate_names,
                duplicate_source_ids,
                existing,
            )
            for row in rows
        ]

    @staticmethod
    def _rack_rows(source_batch, profile) -> list[dict]:
        """Return the batch rows whose class the profile maps to a rack."""
        creates_rack = {
            mapping.source_class
            for mapping in profile.class_role_mappings.all()
            if mapping.creates_rack and not mapping.ignore
        }
        return [row for row in source_batch.rows if _source_text(row.get("device_class")) in creates_rack]

    @staticmethod
    def unit_identity(row) -> str:
        """Return the identity that survives replanning, which is never the row number."""
        return rack_unit_identity(row)

    def _unit(self, row, profile, mapping, netbox_reader, ignored, duplicate_names, duplicate_source_ids, existing):
        """Return the one unit this row produces."""
        identity = self.unit_identity(row)
        name = rack_row_name(row)
        source_id = _source_text(row.get("source_id"))
        if identity_text(name) in duplicate_names or (source_id and source_id in duplicate_source_ids):
            identity = f"{identity}:row:{row.get('_row_number')}"
        unit_display = _unit_display(row, self.key, name, name)
        unit_display["extra_data"].update(
            {
                "rack_type_id": mapping.rack_type_id or "",
                "rack_type_name": str(mapping.rack_type) if mapping.rack_type_id else "",
                "rack_type_set": bool(mapping.rack_type_id),
            }
        )

        rejection = rack_row_rejection(row, ignored, duplicate_names, duplicate_source_ids)
        if rejection is not None:
            code, display = rejection
            display = {**unit_display, **display}
            if code == "rack.ignored":
                return SynchronizationUnit(
                    identity=identity,
                    disposition=Disposition.EXCLUDED,
                    diagnostics=(
                        Diagnostic(code=code, severity=Severity.INFO, identities=(identity,), display=display),
                    ),
                    display=unit_display,
                )
            return _refused(identity, code, display)

        height = _coerce_rack_height(row.get("u_height"))
        serial = _source_text(row.get("serial"))
        rack_type_id = mapping.rack_type_id
        matches = existing.get(identity_text(name), ())
        if len(matches) > 1:
            return _refused(identity, "rack.ambiguous_name", unit_display)
        rack = matches[0] if matches else None
        if rack is None:
            actor = netbox_reader.actor
            if actor is not None and not actor.has_perm("dcim.add_rack"):
                return _refused(identity, "rack.add_permission", unit_display)
            validation = self._validated_candidate(
                None,
                name,
                height,
                serial,
                rack_type_id,
                netbox_reader,
                source_id=_source_text(row.get("source_id")),
            )
            if validation is not None:
                return _refused(identity, "rack.validation_failed", {**unit_display, "message": validation})
            return SynchronizationUnit(
                identity=identity,
                disposition=Disposition.ACTIONABLE,
                changes=(
                    self._change(
                        identity,
                        "create",
                        name,
                        height,
                        serial,
                        rack_type_id,
                        netbox_reader,
                        None,
                        _source_text(row.get("source_id")),
                    ),
                ),
                display=unit_display,
            )

        tenant_id = netbox_reader.tenant.pk if netbox_reader.tenant is not None else None
        existing_display = {
            **unit_display,
            "netbox_url": rack.get_absolute_url(),
            "extra_data": {
                **unit_display["extra_data"],
                "netbox_rack_id": rack.pk,
            },
        }
        update_existing = profile.adapter_settings.update_existing
        if not update_existing or not self._differs(
            rack, height, serial, rack_type_id, netbox_reader.location, tenant_id
        ):
            if update_existing:
                existing_display["extra_data"]["writes_nothing"] = True
                existing_display["detail"] = f"Rack '{name}' already exists and this row changes nothing"
            else:
                existing_display["detail"] = f"Rack '{name}' already exists (update_existing=False)"
            return SynchronizationUnit(identity=identity, disposition=Disposition.NO_OP, display=existing_display)
        validation = self._validated_candidate(
            rack,
            name,
            height,
            serial,
            rack_type_id,
            netbox_reader,
            source_id=_source_text(row.get("source_id")),
        )
        if validation is not None:
            return _refused(identity, "rack.validation_failed", {**existing_display, "message": validation})
        if netbox_reader.actor is not None and not netbox_reader.racks("change").filter(pk=rack.pk).exists():
            return _refused(identity, "rack.change_permission", existing_display)
        return SynchronizationUnit(
            identity=identity,
            disposition=Disposition.ACTIONABLE,
            changes=(
                self._change(
                    identity,
                    "update",
                    name,
                    height,
                    serial,
                    rack_type_id,
                    netbox_reader,
                    rack,
                    _source_text(row.get("source_id")),
                ),
            ),
            display=existing_display,
        )

    def apply(self, planned_change: PlannedChange, execution_context) -> Any:
        """Apply one rack change, having locked its row and rechecked its preconditions."""
        from dcim.models import Rack

        from .object_permissions import enforce_saved_object_permission

        payload = planned_change.payload
        rack_id = planned_change.preconditions.get("rack_id")
        if rack_id is None:
            existing = Rack.objects.filter(
                site_id=payload["site_id"],
                location_id=payload["location_id"],
                name__iexact=payload["name"],
            ).first()
            if existing is not None:
                raise PreconditionFailed(f"Rack '{payload['name']}' appeared after the plan was made.")
            rack = Rack(site_id=payload["site_id"], location_id=payload["location_id"])
            action = "add"
        else:
            # `of=("self",)` because NetBox's default Rack queryset outer-joins, which cannot be locked.
            rack = Rack.objects.filter(pk=rack_id).select_for_update(of=("self",)).first()
            if rack is None:
                raise PreconditionFailed(f"Rack {rack_id} is gone, so '{payload['name']}' cannot be updated.")
            current = self._precondition_state(rack)
            expected = planned_change.preconditions.get("state")
            if current != expected:
                raise PreconditionFailed(f"Rack '{rack.name}' changed after the plan was made.")
            action = "change"

        rack.name = payload["name"]
        rack.u_height = payload["u_height"]
        if payload["serial"]:
            rack.serial = payload["serial"]
        rack.rack_type_id = payload["rack_type_id"]
        if payload["location_id"] is not None:
            rack.location_id = payload["location_id"]
        if payload["tenant_id"] is not None:
            rack.tenant_id = payload["tenant_id"]
        custom_field = execution_context.profile.adapter_settings.custom_field_name
        if action == "add" and custom_field and payload["source_id"]:
            rack.custom_field_data[custom_field] = payload["source_id"]
        rack.full_clean()
        rack.save()
        # An ObjectPermission's constraints are only evaluated against the saved row.
        enforce_saved_object_permission(rack, execution_context.actor, action)
        return rack

    @staticmethod
    def _differs(rack, height: int, serial: str, rack_type_id, location, tenant_id=None) -> bool:
        """Return whether the stored rack already matches what the row asks for."""
        if normalize_for_compare(rack.u_height) != normalize_for_compare(height):
            return True
        if rack.rack_type_id != rack_type_id:
            return True
        if location is not None and rack.location_id != location.pk:
            return True
        # The write assigns the target tenant whenever the import names one.
        if tenant_id is not None and rack.tenant_id != tenant_id:
            return True
        return bool(serial) and _text(rack.serial) != serial

    @staticmethod
    def _change(
        identity, operation, name, height, serial, rack_type_id, netbox_reader, rack, source_id
    ) -> PlannedChange:
        """Return the one write this unit performs, with the target state it assumed."""
        payload = {
            "name": name,
            "u_height": height,
            "serial": serial,
            "rack_type_id": rack_type_id,
            "source_id": source_id,
            "site_id": netbox_reader.site.pk if netbox_reader.site is not None else None,
            "location_id": netbox_reader.location.pk if netbox_reader.location is not None else None,
            "tenant_id": netbox_reader.tenant.pk if netbox_reader.tenant is not None else None,
        }
        preconditions = (
            {"rack_id": rack.pk, "state": RackModule._precondition_state(rack)}
            if rack is not None
            else {"rack_id": None}
        )
        return PlannedChange(
            identity=f"{identity}:{operation}",
            target_module=RackModule.key,
            operation=operation,
            payload=payload,
            preconditions=preconditions,
        )

    @staticmethod
    def _precondition_state(rack) -> dict:
        """Return every rack field this module can overwrite."""
        return {
            "name": rack.name,
            "u_height": rack.u_height,
            "serial": rack.serial,
            "rack_type_id": rack.rack_type_id,
            "location_id": rack.location_id,
            "tenant_id": rack.tenant_id,
        }

    @staticmethod
    def _validated_candidate(rack, name, height, serial, rack_type_id, reader, source_id):
        """Return a model validation message, or None when the planned rack is valid."""
        from dcim.models import Rack

        candidate = copy(rack) if rack is not None else Rack(site=reader.site, location=reader.location)
        candidate.name = name
        candidate.u_height = height
        if serial:
            candidate.serial = serial
        candidate.rack_type_id = rack_type_id
        if reader.location is not None:
            candidate.location = reader.location
        if reader.tenant is not None:
            candidate.tenant = reader.tenant
        try:
            candidate.full_clean()
        except ValidationError as exc:
            return "; ".join(exc.messages)
        return None


def _occupied_units(position, height):
    """Return the half-unit slots a device of *height* fills from *position*."""
    from decimal import Decimal

    start = Decimal(str(position))
    step = Decimal("0.5")
    count = int(Decimal(str(height)) / step)
    return [start + step * index for index in range(count)]


def _refused(identity, code, display) -> SynchronizationUnit:
    """Return an invalid unit carrying the error that refused it."""
    return SynchronizationUnit(
        identity=identity,
        disposition=Disposition.INVALID,
        diagnostics=(Diagnostic(code=code, severity=Severity.ERROR, identities=(identity,), display=display),),
        display=display,
    )


def _with_issues(identity, issues) -> SynchronizationUnit:
    """Return one unit carrying every problem a row has, in the order the checks found them.

    The first problem decides the disposition and the display, so a unit reads exactly as it did
    when only that problem was reported. The rest tell the operator what this row still needs.
    """
    disposition, _first_code, first_display = issues[0]
    return SynchronizationUnit(
        identity=identity,
        disposition=disposition,
        diagnostics=tuple(
            Diagnostic(code=code, severity=Severity.ERROR, identities=(identity,), display=issue_display)
            for _disposition, code, issue_display in issues
        ),
        display=first_display,
    )


@dataclass(frozen=True)
class _Dependencies:
    """What a device row needs to already exist, or the first thing that does not."""

    device_type: Any = None
    role: Any = None
    rack: Any = None
    rack_identity: str | None = None
    role_slug: str = ""
    explicit_device_type: bool = False
    changes: tuple[PlannedChange, ...] = ()
    missing: tuple[str, dict] | None = None


@dataclass(frozen=True)
class _Placement:
    """Where a device row puts the device, or the reason it cannot go there."""

    position: Any = None
    face: str = ""
    airflow: str = ""
    status: str = "active"
    refused: tuple[str, dict] | None = None


@dataclass(frozen=True)
class _PlacementClaim:
    """The rack units one settled device row can reserve."""

    keys: tuple[tuple[int | str, str | None, Any], ...] = ()
    refused: tuple[str, dict] | None = None


@dataclass(frozen=True)
class _Match:
    """The stored device a row reconciles, or the reason no automatic answer is safe."""

    device: Any = None
    ambiguous: str | None = None
    value: str = ""
    method: str = ""
    inaccessible: bool = False


@dataclass(frozen=True)
class _PlannedRole:
    """The identity of a Device Role this unit will create."""

    slug: str
    pk: None = None


_REVIEWED_PAYLOAD_FIELDS: dict[str, tuple[str, Any]] = {
    "serial": ("serial", lambda device: _text(device.serial)),
    "asset_tag": ("asset_tag", lambda device: _text(device.asset_tag)),
    "u_position": ("u_position", lambda device: source_position(device.position)),
    "face": ("face", lambda device: _text(device.face)),
    "airflow": ("airflow", lambda device: _text(device.airflow)),
    "status": ("status", lambda device: _text(device.status)),
    "device_type": ("device_type_id", lambda device: device.device_type_id),
    "role": ("role_id", lambda device: device.role_id),
    "rack_name": ("rack_id", lambda device: device.rack_id),
    "tenant": ("tenant_id", lambda device: device.tenant_id),
    "location": ("location_id", lambda device: device.location_id),
}


def _reviewed_payload(payload, review, device) -> dict:
    """Return the payload with every ignored field back at the value NetBox holds.

    The disposition and the write both read this one result, so a row whose only difference the
    operator ignored plans as a no-op and can never be written as an update.
    """
    if review is None or not review.ignored:
        return payload
    effective = dict(payload)
    effective["ip_fields"] = {
        name: value for name, value in (payload.get("ip_fields") or {}).items() if name not in review.ignored
    }
    for target_field in review.ignored:
        mapped = _REVIEWED_PAYLOAD_FIELDS.get(target_field)
        if mapped is None:
            continue
        key, stored = mapped
        effective[key] = stored(device)
        if target_field == "rack_name":
            effective["rack_name"] = None
    return effective


def _assign_ips(device, ip_fields, actor) -> dict:
    """Assign each placeable address and return the fields that remain unassigned."""
    unassigned = {}
    changed = set()
    for field, address in ip_fields.items():
        try:
            target = ip_assignment.resolve(device, field, address)
        except ip_assignment.IPAssignmentError:
            unassigned[field] = address
            continue
        if target.already_held:
            if getattr(device, f"{field}_id", None) != target.held.pk:
                setattr(device, field, target.held)
                changed.add(field)
            continue
        setattr(device, field, ip_assignment.apply(target, actor))
        changed.add(field)
    if changed:
        device.save(update_fields=sorted(changed))
    return unassigned


def _apply_contact(device, contact, execution_context) -> None:
    """Assign the reviewed primary contact to a device the write has already saved."""
    if contact is None:
        return
    PrimaryContactResolver.apply(
        device,
        execution_context.profile,
        ContactReview(
            selection=ContactSelection(values=dict(contact["values"]), contact_id=contact["contact_id"]),
            extra_columns={},
            plan=None,
            candidate_values={},
            suggestion=None,
        ),
        execution_context.actor,
    )


def _provenance_is_current(device, payload, profile) -> bool:
    """Return whether the stored provenance already records what this row would write.

    A device that holds every field but no record is still work: the record is what lets the next
    import reconcile this device instead of creating a second one beside it.
    """
    from .models import DeviceImportSource

    source_id = payload.get("source_id") or ""
    if (
        source_id
        and not profile.device_matches.filter(
            source_id=source_id,
            netbox_device_id=device.pk,
            device_name=device.name,
            source_asset_tag=payload.get("asset_tag") or "",
        ).exists()
    ):
        return False
    custom_field = profile.adapter_settings.custom_field_name
    if custom_field and source_id and device.custom_field_data.get(custom_field) != source_id:
        return False
    stored = DeviceImportSource.objects.filter(device_id=device.pk).first()
    return stored is not None and (
        stored.profile_id == profile.pk
        and stored.source_id == source_id
        and stored.extra_columns == (payload.get("extra_columns") or {})
        and not stored.unassigned_ips
    )


def _bind_source(profile, source_id, device, asset_tag) -> None:
    """Bind this source row to this device, refusing a binding that already names another."""
    existing = profile.device_matches.select_for_update().filter(source_id=source_id).first()
    if existing is None:
        profile.device_matches.create(
            source_id=source_id,
            netbox_device_id=device.pk,
            device_name=device.name,
            source_asset_tag=asset_tag,
        )
        return
    if existing.netbox_device_id != device.pk:
        raise PreconditionFailed(
            f"Source ID '{source_id}' is bound to device #{existing.netbox_device_id}, not '{device.name}'."
        )
    existing.device_name = device.name
    existing.source_asset_tag = asset_tag
    existing.save(update_fields=["device_name", "source_asset_tag"])


def _store_provenance(device, payload, unassigned, execution_context) -> None:
    """Record which source row wrote this device, so a later import reconciles it instead of copying it."""
    from .models import DeviceImportSource

    profile = execution_context.profile
    source_id = payload.get("source_id") or ""
    asset_tag = payload.get("asset_tag") or ""
    if source_id:
        _bind_source(profile, source_id, device, asset_tag)
        custom_field = profile.adapter_settings.custom_field_name
        if custom_field:
            device.custom_field_data[custom_field] = source_id
            device.save(update_fields=["custom_field_data"])
    DeviceImportSource.objects.update_or_create(
        device=device,
        defaults={
            "profile": profile,
            "source_id": source_id,
            "extra_columns": payload.get("extra_columns") or {},
            "unassigned_ips": unassigned,
        },
    )


def _contact_payload(review) -> dict | None:
    """Return the reviewed contact selection in the form the plan carries and the write replays."""
    if review is None or review.selection is None:
        return None
    return {"values": dict(review.selection.values), "contact_id": review.selection.contact_id}


def _contact_writes_nothing(review) -> bool:
    """Return whether the reviewed contact leaves the stored assignment as it stands."""
    plan = None if review is None else review.plan
    if plan is None:
        return True
    return plan["contact_action"] == "reuse" and plan["assignment_action"] == "unchanged"


class _DeviceBatch:
    """The batch-wide state every device row is planned against, loaded once."""

    _CLASH_FIELDS = (
        ("source_id", "device.duplicate_source_id", False),
        ("serial", "device.duplicate_serial", False),
        ("asset_tag", "device.duplicate_asset_tag", True),
    )

    def __init__(self, source_batch, rows, profile, netbox_reader, *, lock_plan_references: bool = False):
        from dcim.models import Device

        self.profile = profile
        self.reader = netbox_reader
        self.lock_plan_references = lock_plan_references
        self.ignored = _ignored_source_ids(profile)
        self._identity = DeviceTypeIdentityResolver.for_profile(profile)
        self._reviewer = DeviceFieldReviewer.for_profile(profile)
        self._candidate_columns = PrimaryContactResolver.candidate_source_columns(profile)
        self._mappings = {mapping.source_class: mapping for mapping in profile.class_role_mappings.all()}
        self._roles = {source_class: _text(mapping.role_slug) for source_class, mapping in self._mappings.items()}
        self._device_types, self._role_objects = self._load_dependency_objects(rows)
        self._bindings = {_text(match.source_id): match.netbox_device_id for match in profile.device_matches.all()}
        self._bound_sources = {match.netbox_device_id: _text(match.source_id) for match in profile.device_matches.all()}
        identity_rows = [row for row in rows if self._is_identity_writing_row(row)]
        identity_source_ids = {
            _source_text(row.get("source_id")) for row in identity_rows if _source_text(row.get("source_id"))
        }
        self._review_device_ids = {
            source_id: self._reviewer.review_device_ids(source_id) for source_id in identity_source_ids
        }
        reviewed_device_ids = {
            self._bindings[source_id] for source_id in identity_source_ids if source_id in self._bindings
        }
        reviewed_device_ids.update(
            next(iter(device_ids)) for device_ids in self._review_device_ids.values() if len(device_ids) == 1
        )
        self._reviewed_devices = {
            device.pk: device
            for device in Device.objects.select_related(
                "device_type__manufacturer",
                "rack__location",
                "role",
                "tenant",
                "location",
                "site",
            ).filter(pk__in=reviewed_device_ids)
        }
        self._visible_reviewed_device_ids = (
            frozenset(reviewed_device_ids)
            if self.reader.actor is None
            else frozenset(self.reader.devices().filter(pk__in=reviewed_device_ids).values_list("pk", flat=True))
        )
        (
            self._devices_by_source_id,
            self._devices_by_serial,
            self._devices_by_asset_tag,
            self._devices_by_name,
            self._visible_identity_device_ids,
        ) = self._load_identity_objects(identity_rows)
        self._duplicate_names = _repeated(identity_text(effective_device_name(row)) for row in identity_rows)
        self._reserved_names = {
            identity_text(name)
            for name in netbox_reader.devices()
            .filter(
                site=netbox_reader.site,
                **({"tenant": netbox_reader.tenant} if netbox_reader.tenant is not None else {"tenant__isnull": True}),
            )
            .values_list("name", flat=True)
        }
        self._reserved_names.update(identity_text(effective_device_name(row)) for row in identity_rows)
        self._effective_identity = self._effective_identity_values(identity_rows)
        self._clashes = {
            "source_id": self._rows_by_value(source_batch.rows, "source_id", False),
            "serial": self._rows_by_effective("serial", fold=False),
            "asset_tag": self._rows_by_effective("asset_tag", fold=True),
        }
        self._racks = _racks_by_comparison_name(netbox_reader)
        self._planned_racks = self._planned_racks_by_name(source_batch, profile)
        self.side_map, self.airflow_map, self.status_map = translation_maps()
        # Row order decides who keeps a slot two rows claim, so the first row planned wins it.
        self._claimed: dict[tuple[int | str, str | None, Any], int] = {}
        self._claimed_devices: dict[int, tuple[int | None, str]] = {}

    def placement_reference(self, model, pk):
        """Return one row the placement reads, locked when the replan must hold it until the write."""
        queryset = model.objects.filter(pk=pk)
        if self.lock_plan_references:
            queryset = queryset.select_for_update(of=("self",))
        return queryset.first()

    def _load_dependency_objects(self, rows: list[dict[str, Any]]) -> tuple[dict[tuple[str, str], Any], dict[str, Any]]:
        """Load each Device Type and Device Role this batch can reference."""
        from dcim.models import DeviceRole, DeviceType

        type_keys = set()
        for row in rows:
            make = " ".join((_source_text(row.get("make")) or "Unknown").split())
            model = " ".join((_source_text(row.get("model")) or "Unknown").split())
            type_keys.add(self._identity.resolve(make, model)[:2])

        referenced_types = DeviceType.objects.select_related("manufacturer").filter(
            manufacturer__slug__in={mfg_slug for mfg_slug, _dt_slug in type_keys},
            slug__in={dt_slug for _mfg_slug, dt_slug in type_keys},
        )
        if self.lock_plan_references:
            # u_height and is_full_depth decide placement here, and Device.full_clean() reads them again.
            referenced_types = referenced_types.select_for_update(of=("self",)).order_by("pk")
        device_types = {
            (device_type.manufacturer.slug, device_type.slug): device_type for device_type in referenced_types
        }
        role_slugs = {role_slug for role_slug in self._roles.values() if role_slug}
        roles = {role.slug: role for role in DeviceRole.objects.filter(slug__in=role_slugs)}
        return device_types, roles

    def _load_identity_objects(self, rows):
        """Load the global Device identity candidates and their visibility once."""
        from dcim.models import Device
        from django.db.models.functions import Upper

        source_ids = {_source_text(row.get("source_id")) for row in rows} - {""}
        serials = {_source_text(row.get("serial")) for row in rows} - {""}
        raw_asset_tags = {_source_text(row.get("asset_tag"))[:50] for row in rows} - {""}
        raw_names = {_source_text(effective_device_name(row)) for row in rows} - {""}
        self._database_asset_tag_keys = _database_upper_values(
            raw_asset_tags,
            collation=Device._meta.get_field("asset_tag").db_collation,
        )
        self._database_name_keys = _database_upper_values(
            raw_names,
            collation=Device._meta.get_field("name").db_collation,
        )
        asset_tags = set(self._database_asset_tag_keys.values())
        names = set(self._database_name_keys.values())
        devices = Device.objects.select_related(
            "device_type__manufacturer",
            "rack__location",
            "role",
            "tenant",
            "location",
            "site",
        )
        stored_devices = list(
            devices.select_related("data_import_source").filter(
                data_import_source__profile=self.profile,
                data_import_source__source_id__in=source_ids,
            )
        )
        serial_devices = list(devices.filter(serial__in=serials))
        asset_tag_devices = list(
            devices.annotate(_identity_asset_tag=Upper("asset_tag")).filter(_identity_asset_tag__in=asset_tags)
        )
        tenant_filter = {"tenant": self.reader.tenant} if self.reader.tenant is not None else {"tenant__isnull": True}
        name_devices = (
            list(
                devices.annotate(_identity_name=Upper("name")).filter(
                    _identity_name__in=names,
                    site=self.reader.site,
                    **tenant_filter,
                )
            )
            if self.reader.site is not None
            else []
        )

        def index(items, key):
            """Group already-loaded devices by one matching value."""
            found = {}
            for device in items:
                found.setdefault(key(device), []).append(device)
            return found

        candidates = {device.pk for device in (*stored_devices, *serial_devices, *asset_tag_devices, *name_devices)}
        visible_ids = (
            frozenset(candidates)
            if self.reader.actor is None
            else frozenset(self.reader.devices().filter(pk__in=candidates).values_list("pk", flat=True))
        )
        return (
            index(stored_devices, lambda device: _source_text(device.data_import_source.source_id)),
            index(serial_devices, lambda device: _text(device.serial)),
            index(asset_tag_devices, lambda device: device._identity_asset_tag),
            index(name_devices, lambda device: device._identity_name),
            visible_ids,
        )

    def _is_identity_writing_row(self, row) -> bool:
        """Return whether a row can write Device identity fields."""
        mapping = self._mappings.get(_source_text(row.get("device_class")))
        source_id = _source_text(row.get("source_id"))
        position = source_position(row.get("u_position"))
        return bool(
            mapping
            and not mapping.creates_rack
            and not mapping.ignore
            and mapping.role_slug
            and not (source_id and source_id in self.ignored)
            and not (position is not None and position < 1)
        )

    @staticmethod
    def _rows_by_value(rows, field, fold) -> dict[str, list[int]]:
        """Return the source row numbers each non-empty value of *field* appears on."""
        found: dict[str, list[int]] = {}
        for row in rows:
            raw = _source_text(row.get(field))[:50] if field == "asset_tag" else row.get(field)
            value = identity_text(raw) if fold else _source_text(raw)
            if value:
                found.setdefault(value, []).append(row.get("_row_number"))
        return {value: numbers for value, numbers in found.items() if len(numbers) > 1}

    def _effective_identity_values(self, rows) -> dict[int, dict[str, str]]:
        """Return review-aware serial and asset-tag writes for duplicate checks."""
        values = {}
        for row in rows:
            row_number = row.get("_row_number")
            source_id = _source_text(row.get("source_id"))
            proposal = {
                "serial": _source_text(row.get("serial")),
                "asset_tag": _source_text(row.get("asset_tag"))[:50],
            }
            device_id = self._bindings.get(source_id)
            if device_id is None and source_id:
                reviewed_ids = self._review_device_ids.get(source_id, frozenset())
                if len(reviewed_ids) == 1:
                    device_id = next(iter(reviewed_ids))
            device = self._reviewed_devices.get(device_id)
            if device is not None:
                review = self._reviewer.review(source_id, device, proposal)
                effective = review.effective_proposal
                proposal = {
                    "serial": "" if "serial" in review.ignored else _source_text(effective.get("serial")),
                    "asset_tag": (
                        "" if "asset_tag" in review.ignored else _source_text(effective.get("asset_tag"))[:50]
                    ),
                }
            values[row_number] = proposal
        return values

    def _rows_by_effective(self, field, fold) -> dict[str, list[int]]:
        """Return duplicate row numbers from review-aware identity values."""
        found: dict[str, list[int]] = {}
        for row_number, values in self._effective_identity.items():
            raw = values[field]
            value = identity_text(raw) if fold else raw
            if value:
                found.setdefault(value, []).append(row_number)
        return {value: numbers for value, numbers in found.items() if len(numbers) > 1}

    def _planned_racks_by_name(self, source_batch, profile) -> dict[str, str]:
        """Return valid rack creates in this batch, keyed by comparison name."""
        rows = RackModule._rack_rows(source_batch, profile)
        ignored = _ignored_source_ids(profile)
        duplicate_names, duplicate_source_ids = rack_duplicate_keys(rows)
        planned = {}
        for row in rows:
            if rack_row_rejection(row, ignored, duplicate_names, duplicate_source_ids) is not None:
                continue
            name_key = identity_text(rack_row_name(row))
            # A rack NetBox already holds is an update, so it is there before any device change runs.
            if name_key in self._racks:
                continue
            planned[name_key] = rack_unit_identity(row)
        return planned

    def clash(self, row) -> tuple[str, str, list[int]] | None:
        """Return the first identity another row in this batch also claims."""
        for field, code, fold in self._CLASH_FIELDS:
            raw = (
                row.get(field)
                if field == "source_id"
                else self._effective_identity.get(row.get("_row_number"), {}).get(field, "")
            )
            value = _source_text(raw)
            key = identity_text(value) if fold else value
            numbers = self._clashes[field].get(key) if key else None
            if numbers:
                return code, value, numbers
        return None

    def dependencies(self, row) -> _Dependencies:
        """Return existing relation objects and planned roles, or the first unmet dependency."""
        make = " ".join((_source_text(row.get("make")) or "Unknown").split())
        model = " ".join((_source_text(row.get("model")) or "Unknown").split())
        mfg_slug, dt_slug, explicit = self._identity.resolve(make, model)
        changes = []
        actor = self.reader.actor
        device_type = self._device_types.get((mfg_slug, dt_slug))
        if device_type is None:
            return _Dependencies(
                missing=(
                    "device.device_type_missing",
                    {
                        "mfg_slug": mfg_slug,
                        "dt_slug": dt_slug,
                        "source_make": make,
                        "source_model": model,
                    },
                )
            )
        if not explicit and identity_text(device_type.model) != identity_text(model):
            return _Dependencies(
                missing=(
                    "device.device_type_slug_collision",
                    {"dt_slug": dt_slug, "source_model": model, "stored_model": device_type.model},
                )
            )

        role_slug = self._roles.get(_source_text(row.get("device_class")), "")
        if not role_slug:
            return _Dependencies(missing=("device.role_unconfigured", {"source_class": row.get("device_class")}))
        role = self._role_objects.get(role_slug)
        if role is None:
            if actor is not None and not actor.has_perm("dcim.add_devicerole"):
                return _Dependencies(missing=("device.role_permission", {"role_slug": role_slug}))
            changes.append(self._role_change(role_slug))
            role = _PlannedRole(role_slug)

        rack_name = _source_text(row.get("rack_name"))
        rack_key = identity_text(rack_name)
        rack_matches = self._racks.get(rack_key, ()) if rack_name else ()
        if len(rack_matches) > 1:
            return _Dependencies(missing=("device.rack_ambiguous", {"rack_name": rack_name}))
        rack = rack_matches[0] if rack_matches else None
        if rack is not None:
            from dcim.models import Rack

            # The name scan above cannot lock, and this rack decides the placement claim.
            rack = self.placement_reference(Rack, rack.pk)
        rack_identity = self._planned_racks.get(rack_key) if rack_name and rack is None else None
        if rack_name and rack is None and rack_identity is None:
            return _Dependencies(missing=("device.rack_missing", {"rack_name": rack_name}))
        return _Dependencies(
            device_type=device_type,
            role=role,
            rack=rack,
            rack_identity=rack_identity,
            role_slug=role_slug,
            explicit_device_type=explicit,
            changes=tuple(changes),
        )

    @staticmethod
    def _role_change(role_slug) -> PlannedChange:
        return PlannedChange(
            identity=f"device_role:{role_slug}:create",
            target_module=DeviceModule.key,
            operation="create_role",
            payload={"slug": role_slug, "name": role_slug.replace("-", " ").title(), "color": "9e9e9e"},
            preconditions={"role_id": None},
        )

    def placement(self, row, device_type, rack, rack_identity) -> _Placement:
        """Return where this row puts the device, or the first reason it cannot go there."""
        zero_u = device_type.u_height == 0
        position = None if zero_u else source_position(row.get("u_position"))
        face = "" if zero_u else _translate(row.get("face"), self.side_map)
        airflow = _translate(row.get("airflow"), self.airflow_map)
        status = _translate(row.get("status"), self.status_map) or "active"
        has_rack = rack is not None or rack_identity is not None

        if position is None:
            # NetBox refuses a rack face on a device in no rack, so the plan never asks for one.
            return _Placement(position=None, face=face if has_rack else "", airflow=airflow, status=status)
        if not has_rack:
            return _Placement(refused=("device.rack_required", {"u_position": position}))
        if not face:
            return _Placement(refused=("device.face_required", {"u_position": position}))
        return _Placement(position=position, face=face, airflow=airflow, status=status)

    def prepare_claim(self, rack, rack_identity, placement, device_type, matched) -> _PlacementClaim:
        """Return the available units this row can claim without reserving them yet."""
        rack_key = rack.pk if rack is not None else rack_identity
        if rack_key is None or placement.position is None or device_type.u_height == 0:
            return _PlacementClaim()
        rack_face = None if device_type.is_full_depth else placement.face
        claim_faces = tuple(dict.fromkeys(self.side_map.values())) if device_type.is_full_depth else (placement.face,)
        keys = tuple(
            (rack_key, claim_face, unit)
            for claim_face in claim_faces
            for unit in _occupied_units(placement.position, device_type.u_height)
        )
        for key in keys:
            claimed_by = self._claimed.get(key)
            if claimed_by is not None:
                return _PlacementClaim(
                    refused=(
                        "device.rack_position_claimed",
                        {"u_position": placement.position, "claimed_by_row": claimed_by},
                    )
                )

        if rack is not None:
            available = rack.get_available_units(
                u_height=device_type.u_height,
                rack_face=rack_face,
                exclude=[matched.pk] if matched is not None else [],
            )
            if placement.position not in available:
                return _PlacementClaim(
                    refused=(
                        "device.rack_position_occupied",
                        {"u_position": placement.position, "rack_name": rack.name},
                    )
                )
        return _PlacementClaim(keys=keys)

    def commit_claim(self, row, claim: _PlacementClaim) -> None:
        """Reserve a checked placement after its row has settled."""
        self._claimed.update({key: row.get("_row_number") for key in claim.keys})

    def match(self, row, name) -> _Match:
        """Return the stored device this row reconciles, strongest identifier first.

        Only for a row `_is_identity_writing_row` accepts: the collation keys are loaded for those.
        """
        source_id = _source_text(row.get("source_id"))

        bound_id = self._bindings.get(source_id) if source_id else None
        if bound_id is not None:
            bound = self._reviewed_devices.get(bound_id)
            if bound is not None:
                return self._visible_match(bound, "source ID link")

        stored = self._devices_by_source_id.get(source_id, ()) if source_id else ()
        if len(stored) > 1:
            return _Match(ambiguous="device.ambiguous_stored_source_id", value=source_id)
        if stored:
            return self._visible_match(stored[0], "stored source ID")

        reviewed_ids = self._review_device_ids.get(source_id, frozenset())
        if len(reviewed_ids) > 1:
            return _Match(ambiguous="device.ambiguous_field_review", value=source_id)
        if reviewed_ids:
            reviewed = self._reviewed_devices.get(next(iter(reviewed_ids)))
            if reviewed is not None:
                return self._visible_match(reviewed, "field review")

        for field, matches, code in (
            ("serial", self._devices_by_serial, "device.ambiguous_serial"),
            ("asset_tag", self._devices_by_asset_tag, "device.ambiguous_asset_tag"),
        ):
            value = _source_text(row.get(field))[:50] if field == "asset_tag" else _source_text(row.get(field))
            if not value:
                continue
            key = self._database_asset_tag_keys[value] if field == "asset_tag" else value
            found = matches.get(key, ())
            if len(found) > 1:
                return _Match(ambiguous=code, value=value)
            if found:
                return self._visible_match(found[0], field.replace("_", " "))

        if identity_text(name) in self._duplicate_names or self.reader.site is None:
            return _Match()
        name_value = _source_text(name)
        by_name = self._devices_by_name.get(self._database_name_keys[name_value], ()) if name_value else ()
        if len(by_name) > 1:
            return _Match(ambiguous="device.ambiguous_name", value=name)
        return self._visible_match(by_name[0], "name") if by_name else _Match()

    def _visible_match(self, device, method) -> _Match:
        """Return a global match with its scope and site safety attached."""
        inaccessible = (
            device.pk not in self._visible_reviewed_device_ids
            if device.pk in self._reviewed_devices
            else device.pk not in self._visible_identity_device_ids
        )
        return _Match(device=device, method=method, inaccessible=inaccessible)

    def binding_conflict(self, row, match) -> str:
        """Return the source identity that already claims a matched device."""
        source_id = _source_text(row.get("source_id"))
        bound_source = self._bound_sources.get(match.device.pk)
        if bound_source and bound_source != source_id:
            return bound_source
        previous = self._claimed_devices.get(match.device.pk)
        if previous is not None and previous[0] != row.get("_row_number"):
            return previous[1] or f"row {previous[0]}"
        return ""

    def commit_device_claim(self, row, match) -> None:
        """Reserve a matched Device for one accepted unit."""
        self._claimed_devices[match.device.pk] = (
            row.get("_row_number"),
            _source_text(row.get("source_id")),
        )

    def suggest_name(self, row) -> str:
        """Return and reserve one deterministic name for a duplicate source row."""
        name = effective_device_name(row)
        rack_name = _source_text(row.get("rack_name")) or "NO-RACK"
        position = normalize_for_compare(row.get("u_position")) or "NO-U"
        base = f"{name}-{rack_name}-U{position}" if position != "NO-U" else f"{name}-{rack_name}"
        candidate = base[:64]
        if identity_text(candidate) in self._reserved_names:
            source_suffix = _source_text(row.get("source_id")) or str(row.get("_row_number") or "ROW")
            source_suffix = re.sub(r"[^A-Za-z0-9_.-]+", "-", source_suffix).strip("-") or "ROW"
            candidate = f"{base[: max(1, 63 - len(source_suffix))]}-{source_suffix}"[:64]
        unique = candidate
        counter = 2
        while identity_text(unique) in self._reserved_names:
            suffix = f"-{counter}"
            unique = f"{candidate[: 64 - len(suffix)]}{suffix}"
            counter += 1
        self._reserved_names.add(identity_text(unique))
        return unique

    def review(self, row, device, dependencies, placement, payload):
        """Return the operator's saved review for one matched device, in the reviewer's terms."""
        device_type = dependencies.device_type
        manufacturer = device_type.manufacturer
        device_type_identity = (
            manufacturer.slug,
            device_type.slug,
            manufacturer.name,
            device_type.model,
        )
        ip_fields = payload.get("ip_fields") or {}
        proposal = {
            "device_name": payload["name"],
            "serial": payload["serial"],
            "asset_tag": payload["asset_tag"],
            "u_position": placement.position,
            "face": placement.face,
            "airflow": placement.airflow,
            "status": placement.status,
            "rack_name": _source_text(row.get("rack_name")),
            "_rack_location_id": self.reader.location.pk if self.reader.location is not None else None,
            "device_type": device_type_identity,
            "role": _text(dependencies.role.slug),
            "tenant": self.reader.tenant,
            "location": self.reader.location,
            **{name: ip_fields.get(name, "") for name in ip_assignment.IP_FIELD_FAMILY},
        }
        return self._reviewer.review(_source_text(row.get("source_id")), device, proposal)

    def contact_review(self, row, device):
        """Return the reviewed primary contact for one row, before anything is written."""
        return PrimaryContactResolver.review(
            device,
            row,
            self.profile,
            self.reader.actor,
            candidate_source_columns=self._candidate_columns,
        )


class DeviceModule:
    """Plans the Devices a flat source batch describes.

    Section 4.4 makes a Device Type, a Device Role and a Rack dependencies of a device, not part of
    it, so a device row that names one NetBox does not hold is blocked rather than invalid. Creating
    them is another unit's work.
    """

    key = "device"
    consumes = frozenset({OutputKind.DEVICE_SOURCE_ROW})

    def plan(
        self,
        source_batch,
        profile,
        catalog,
        netbox_reader,
        *,
        lock_plan_references: bool = False,
    ) -> list[SynchronizationUnit]:
        """Return one Synchronization Unit per device row, with the disposition its state earns."""
        rows = self._device_rows(source_batch, profile)
        if not rows:
            return []
        if netbox_reader.site is None:
            raise PlanningTargetUnavailable("Device planning needs an import target site.")
        batch = _DeviceBatch(source_batch, rows, profile, netbox_reader, lock_plan_references=lock_plan_references)
        return [self._unit(row, batch) for row in rows]

    @staticmethod
    def _device_rows(source_batch, profile) -> list[dict]:
        """Return every non-rack row, including rows whose policy must be reviewed."""
        mappings = {mapping.source_class: mapping for mapping in profile.class_role_mappings.all()}
        return [
            row
            for row in source_batch.rows
            if not (
                (mapping := mappings.get(_source_text(row.get("device_class")))) is not None and mapping.creates_rack
            )
        ]

    @staticmethod
    def unit_identity(row) -> str:
        """Return the identity that survives replanning, which is never the row number."""
        source_id = _source_text(row.get("source_id"))
        if source_id:
            return f"device:source:{source_id}"
        return f"device:name:{identity_text(effective_device_name(row))}"

    def _unit(self, row, batch) -> SynchronizationUnit:  # noqa: C901
        """Return the one unit this row produces, naming every problem it can already prove.

        The checks run in one fixed order and record what they find instead of returning, so a row
        held up by its identity still reports the mapping, placement and Contact work it needs. The
        first problem stays the authoritative one, and an operator fixing them in any order sees the
        rest of the list shrink. Only the checks whose inputs are available run, and every one of
        them reads: the helpers that reserve a name, a rack unit or a device stay on the paths that
        settle a row.
        """
        identity = self.unit_identity(row)
        name = effective_device_name(row)
        source_id = _source_text(row.get("source_id"))
        if source_id in batch._clashes["source_id"] or (
            not source_id and identity_text(name) in batch._duplicate_names
        ):
            identity = f"{identity}:row:{row.get('_row_number')}"
        display = _unit_display(row, self.key, name, _source_text(row.get("rack_name")))
        display["extra_data"].update(_class_mapping_display(batch._mappings.get(_source_text(row.get("device_class")))))
        display["device_name"] = name
        display["source_id"] = source_id

        issues: list[tuple[str, str, dict]] = []

        def problem(disposition, code, extra=None) -> None:
            """Record one problem this row has, so the checks after it still run."""
            issues.append((disposition, code, {**display, **(extra or {})}))

        clash = batch.clash(row)
        if clash is not None:
            code, value, numbers = clash
            other_rows = [number for number in numbers if number != row.get("_row_number")]
            label = {
                "device.duplicate_source_id": "source ID",
                "device.duplicate_serial": "serial",
                "device.duplicate_asset_tag": "asset tag",
            }[code]
            conflict_display = {
                "message": _duplicate_value_detail(label, value, other_rows),
                "value": value,
                "rows": numbers,
            }
            if code == "device.duplicate_serial":
                conflict_display["duplicate_serial"] = value
            problem(Disposition.INVALID, code, conflict_display)

        # An excluded or below-rack row is an answer rather than a problem, so it reports no list.
        if not issues and source_id and source_id in batch.ignored:
            ignored_display = {
                **display,
                "extra_data": {**display["extra_data"], "ignore_kind": "individual"},
            }
            return SynchronizationUnit(
                identity=identity,
                disposition=Disposition.EXCLUDED,
                diagnostics=(
                    Diagnostic(
                        code="device.ignored",
                        severity=Severity.INFO,
                        identities=(identity,),
                        display=ignored_display,
                    ),
                ),
                display=ignored_display,
            )

        position = source_position(row.get("u_position"))
        if not issues and position is not None and position < 1:
            return SynchronizationUnit(
                identity=identity,
                disposition=Disposition.NO_OP,
                diagnostics=(
                    Diagnostic(
                        code="device.below_rack",
                        severity=Severity.INFO,
                        identities=(identity,),
                        display={**display, "u_position": position},
                    ),
                ),
                display=display,
            )
        if not name:
            problem(Disposition.INVALID, "device.missing_name")

        mapping = batch._mappings.get(_source_text(row.get("device_class")))
        if mapping is None:
            problem(
                Disposition.INVALID,
                "device.class_unmapped",
                {"source_class": _source_text(row.get("device_class"))},
            )
        elif mapping.ignore:
            if not issues:
                return SynchronizationUnit(
                    identity=identity,
                    disposition=Disposition.EXCLUDED,
                    diagnostics=(
                        Diagnostic(
                            code="device.class_ignored",
                            severity=Severity.INFO,
                            identities=(identity,),
                            display={
                                **display,
                                "extra_data": {**display["extra_data"], "ignore_kind": "class"},
                            },
                        ),
                    ),
                    display={**display, "extra_data": {**display["extra_data"], "ignore_kind": "class"}},
                )
            # The class says to skip this row, so nothing after it has anything to plan.
            mapping = None

        dependencies = batch.dependencies(row) if mapping is not None else None
        if dependencies is not None and dependencies.missing is not None:
            code, missing_display = dependencies.missing
            problem(Disposition.BLOCKED, code, missing_display)

        # Only an identity-writing row has its name and asset tag in the batch's collation keys.
        match = batch.match(row, name) if name and batch._is_identity_writing_row(row) else _Match()
        if match.ambiguous is not None:
            problem(Disposition.INVALID, match.ambiguous, {"value": match.value})
        elif match.inaccessible:
            problem(Disposition.INVALID, "device.inaccessible_match")
        elif match.device is not None and match.device.site_id != batch.reader.site.pk:
            problem(
                Disposition.INVALID,
                "device.cross_site_match",
                {"netbox_device_id": match.device.pk, "match_method": match.method},
            )
        elif identity_text(name) in batch._duplicate_names and match.device is None:
            problem(
                Disposition.INVALID,
                "device.duplicate_name",
                {"extra_data": {**display["extra_data"], "suggested_name": batch.suggest_name(row)}},
            )
        elif match.device is not None and (bound_source := batch.binding_conflict(row, match)):
            problem(
                Disposition.INVALID,
                "device.already_bound",
                {"bound_source_id": bound_source, "netbox_device_id": match.device.pk},
            )

        # Only a row that has settled every check so far may claim the device it matched.
        if not issues and match.device is not None:
            name_note = (
                f"; name stays '{match.device.name}' (source: '{name}')"
                if match.device.name != name
                else "; name unchanged"
            )
            display = {
                **display,
                "detail": f"Will update '{match.device.name}' (matched by {match.method}{name_note})",
                "netbox_url": match.device.get_absolute_url(),
                "extra_data": {
                    **display["extra_data"],
                    "netbox_device_id": match.device.pk,
                    "netbox_face": match.device.face or "",
                    "netbox_position": normalize_for_compare(match.device.position),
                    "netbox_rack_name": match.device.rack.name if match.device.rack_id else "",
                    # A row refused for an identity conflict states no change, so it needs this here.
                    "_placement_state": {
                        # `_placement_differs` reads location as placement, so the baseline states it.
                        "location_id": match.device.location_id,
                        "rack_id": match.device.rack_id,
                        "position": normalize_for_compare(match.device.position),
                        "face": match.device.face or "",
                    },
                },
            }
            if not batch.profile.adapter_settings.update_existing:
                display["detail"] = (
                    f"Matched to '{match.device.name}' (by {match.method}{name_note}, skip: update_existing off)"
                )
                batch.commit_device_claim(row, match)
                return SynchronizationUnit(
                    identity=identity,
                    disposition=Disposition.NO_OP,
                    display=display,
                )

        placement = None
        if dependencies is not None and dependencies.missing is None:
            placement = batch.placement(row, dependencies.device_type, dependencies.rack, dependencies.rack_identity)
            if placement.refused is not None:
                code, placement_display = placement.refused
                problem(Disposition.INVALID, code, placement_display)
                placement = None
            else:
                display = {
                    **display,
                    "extra_data": {
                        **display["extra_data"],
                        "airflow": placement.airflow,
                        "dt_slug": dependencies.device_type.slug,
                        "face": placement.face,
                        "is_explicit_mapping": dependencies.explicit_device_type,
                        "mfg_slug": dependencies.device_type.manufacturer.slug,
                        "status": placement.status,
                        "u_height": _display_value(dependencies.device_type.u_height),
                        "u_position": placement.position,
                        **({"zero_u": True} if dependencies.device_type.u_height == 0 else {}),
                    },
                }

        contact = None
        try:
            contact = batch.contact_review(row, match.device)
        except ObjectPermissionDenied as exc:
            problem(Disposition.INVALID, "device.contact_permission", {"message": str(exc)})
        except DanglingProfileReference as exc:
            problem(Disposition.BLOCKED, "profile.dangling_reference", {"message": "; ".join(exc.messages)})
        except ContactResolutionRequired as exc:
            problem(
                Disposition.INVALID,
                "device.contact_resolution_required",
                {
                    "extra_data": {
                        **display["extra_data"],
                        "candidate_values": {"contact": exc.candidate_values},
                        "contact_suggestion": exc.suggestion or {},
                    },
                },
            )
        except ValidationError as exc:
            problem(Disposition.INVALID, "device.contact_invalid", {"error": "; ".join(exc.messages)})
        else:
            display = {
                **display,
                "extra_data": {
                    **display["extra_data"],
                    "candidate_values": {"contact": contact.candidate_values} if contact.candidate_values else {},
                    "contact_suggestion": contact.suggestion or {},
                    "extra_columns": contact.extra_columns,
                },
            }

        # Everything below reads all of these, and a row that could not settle one recorded why.
        if dependencies is None or dependencies.missing is not None or placement is None or contact is None:
            return _with_issues(identity, issues)

        payload = self._payload(row, name, dependencies, placement, batch)
        ip_fields, ip_diagnostics = self._ip_fields(row, identity, display)
        payload = {
            **payload,
            "contact": _contact_payload(contact),
            "source_id": source_id,
            "extra_columns": contact.extra_columns,
            "ip_fields": ip_fields,
        }
        if match.device is None:
            claim = batch.prepare_claim(
                dependencies.rack,
                dependencies.rack_identity,
                placement,
                dependencies.device_type,
                None,
            )
            if claim.refused is not None:
                code, taken_display = claim.refused
                problem(Disposition.INVALID, code, taken_display)
            actor = batch.reader.actor
            if actor is not None and not actor.has_perm("dcim.add_device"):
                problem(Disposition.INVALID, "device.add_permission")
            if validation := self._validation_error(None, payload):
                problem(Disposition.INVALID, "device.validation_failed", {"message": validation})
            if issues:
                return _with_issues(identity, issues)
            batch.commit_claim(row, claim)
            device_change = self._change(
                identity,
                "create",
                payload,
                None,
                dependencies.rack_identity,
                dependencies.changes,
                batch.profile,
            )
            return SynchronizationUnit(
                identity=identity,
                disposition=Disposition.ACTIONABLE,
                changes=(*dependencies.changes, device_change),
                diagnostics=ip_diagnostics,
                display=display,
            )
        review = batch.review(row, match.device, dependencies, placement, payload)
        display = self._review_display(display, review)
        payload = _reviewed_payload(payload, review, match.device)
        relation_changes = () if "role" in review.ignored else dependencies.changes
        effective_type = dependencies.device_type
        if payload["device_type_id"] is not None and payload["device_type_id"] != dependencies.device_type.pk:
            from dcim.models import DeviceType

            # A retained review value sizes the placement too, so the replan must hold it.
            effective_type = batch.placement_reference(DeviceType, payload["device_type_id"])
            if effective_type is None:
                # Every check below reads the device type this row would write.
                problem(Disposition.INVALID, "device.device_type_missing")
                return _with_issues(identity, issues)
        if effective_type.u_height == 0:
            zero_u_conflicts = []
            if "u_position" in review.ignored and payload["u_position"] is not None:
                zero_u_conflicts.append("u_position")
            else:
                payload["u_position"] = None
            if "face" in review.ignored and payload["face"]:
                zero_u_conflicts.append("face")
            else:
                payload["face"] = ""
            if zero_u_conflicts:
                problem(Disposition.INVALID, "device.zero_u_review_conflict", {"fields": zero_u_conflicts})
        effective_rack = dependencies.rack
        effective_rack_identity = dependencies.rack_identity
        if payload["rack_name"] is None:
            effective_rack_identity = None
            if payload["rack_id"] != (dependencies.rack.pk if dependencies.rack is not None else None):
                from dcim.models import Rack

                effective_rack = batch.placement_reference(Rack, payload["rack_id"]) if payload["rack_id"] else None
        effective_placement = _Placement(
            position=payload["u_position"],
            face=payload["face"],
            airflow=payload["airflow"],
            status=payload["status"],
        )
        claim = batch.prepare_claim(
            effective_rack,
            effective_rack_identity,
            effective_placement,
            effective_type,
            match.device,
        )
        if claim.refused is not None:
            code, taken_display = claim.refused
            problem(Disposition.INVALID, code, taken_display)
        if not self._placement_differs(match.device, payload):
            display = {
                **display,
                "extra_data": {**display["extra_data"], "placement_sync_writes_nothing": True},
            }
        if match.method == "name" and self._placement_differs(match.device, payload):
            # The preview offers the rename for both refusals, so both state the name it would use.
            rename = {"extra_data": {**display["extra_data"], "suggested_name": batch.suggest_name(row)}}
            # A stored Device with no placement has none to sit at, so it reads as a different refusal.
            if self._device_is_unplaced(match.device):
                problem(Disposition.INVALID, "device.name_unplaced_match", rename)
            else:
                problem(Disposition.INVALID, "device.name_placement_conflict", rename)
        if (
            not issues
            and not self._differs(match.device, payload)
            and _contact_writes_nothing(contact)
            and _provenance_is_current(match.device, payload, batch.profile)
        ):
            batch.commit_claim(row, claim)
            batch.commit_device_claim(row, match)
            display = {
                **display,
                "detail": f"Device '{match.device.name}' matches this row, which writes nothing",
                "extra_data": {
                    **display["extra_data"],
                    "placement_sync_writes_nothing": not self._placement_differs(match.device, payload),
                    "writes_nothing": True,
                },
            }
            return SynchronizationUnit(
                identity=identity,
                disposition=Disposition.NO_OP,
                diagnostics=ip_diagnostics,
                display=display,
            )
        actor = batch.reader.actor
        if actor is not None and not batch.reader.devices("change").filter(pk=match.device.pk).exists():
            problem(Disposition.INVALID, "device.change_permission")
        if validation := self._validation_error(match.device, payload):
            problem(Disposition.INVALID, "device.validation_failed", {"message": validation})
        if issues:
            return _with_issues(identity, issues)
        batch.commit_claim(row, claim)
        batch.commit_device_claim(row, match)
        device_change = self._change(
            identity,
            "update",
            payload,
            match.device,
            dependencies.rack_identity,
            relation_changes,
            batch.profile,
        )
        return SynchronizationUnit(
            identity=identity,
            disposition=Disposition.ACTIONABLE,
            changes=(*relation_changes, device_change),
            diagnostics=ip_diagnostics,
            display=display,
        )

    @staticmethod
    def _placement_differs(device, payload) -> bool:
        """Return whether a name-only match would move the stored Device."""
        return (
            payload["rack_name"] is not None
            or device.location_id != payload["location_id"]
            or device.rack_id != payload["rack_id"]
            or normalize_for_compare(device.position) != normalize_for_compare(payload["u_position"])
            or (device.face or "") != payload["face"]
        )

    @staticmethod
    def _device_is_unplaced(device) -> bool:
        """Return whether the stored Device records no placement for the source to move it from."""
        return device.location_id is None and device.rack_id is None and device.position is None and not device.face

    @staticmethod
    def _validation_error(device, payload) -> str:
        """Return a model validation message for a fully resolvable Device change."""
        from dcim.models import Device

        if payload["role_id"] is None or payload["rack_name"] is not None:
            return ""
        candidate = copy(device) if device is not None else Device(name=payload["name"])
        candidate.device_type_id = payload["device_type_id"]
        candidate.role_id = payload["role_id"]
        candidate.site_id = payload["site_id"]
        candidate.location_id = payload["location_id"]
        candidate.rack_id = payload["rack_id"]
        candidate.position = payload["u_position"]
        candidate.face = payload["face"]
        candidate.status = payload["status"]
        candidate.tenant_id = payload["tenant_id"]
        if payload["airflow"]:
            candidate.airflow = payload["airflow"]
        for field in ("serial", "asset_tag"):
            if payload[field]:
                setattr(candidate, field, payload[field])
        try:
            candidate.full_clean()
        except ValidationError as exc:
            if hasattr(exc, "message_dict"):
                return "; ".join(f"{field}: {', '.join(errors)}" for field, errors in exc.message_dict.items())
            return "; ".join(exc.messages)
        return ""

    @staticmethod
    def _review_display(display, review) -> dict:
        """Attach the matched Device review state as display-only plan data."""
        snapshots = {
            field: {"file": file_snapshot, "netbox": netbox_snapshot}
            for field, (file_snapshot, netbox_snapshot) in review.snapshots.items()
        }
        return {
            **display,
            "extra_data": {
                **display["extra_data"],
                "field_diff": review.differing,
                "field_ignored": review.ignored,
                "field_informational": review.informational,
                "field_review_snapshots": snapshots,
                "field_non_writable": sorted(DeviceFieldReviewer.non_writable_fields()),
            },
        }

    def apply(self, planned_change: PlannedChange, execution_context) -> Any:  # noqa: C901
        """Apply one device change, having locked its row and rechecked its preconditions."""
        from dcim.models import Device, DeviceRole, Rack

        from .object_permissions import enforce_saved_object_permission

        payload = planned_change.payload
        if planned_change.operation == "create_role":
            if DeviceRole.objects.filter(slug=payload["slug"]).exists():
                raise PreconditionFailed(f"Device Role slug '{payload['slug']}' appeared after planning.")
            role = DeviceRole(name=payload["name"], slug=payload["slug"], color=payload["color"])
            role.full_clean()
            role.save()
            enforce_saved_object_permission(role, execution_context.actor, "add")
            return role

        device_id = planned_change.preconditions.get("device_id")
        if device_id is None:
            conflict = self._create_identity_conflict(payload, execution_context.profile)
            if conflict:
                raise PreconditionFailed(f"{conflict} appeared after planning.")
            device = Device()
            action = "add"
        else:
            # `of=("self",)` because NetBox's default Device queryset outer-joins, which cannot be locked.
            device = Device.objects.filter(pk=device_id).select_for_update(of=("self",)).first()
            if device is None:
                raise PreconditionFailed(f"Device {device_id} is gone, so '{payload['name']}' cannot be updated.")
            current = self._precondition_state(
                device,
                execution_context.profile,
                payload.get("source_id") or "",
            )
            if current != planned_change.preconditions.get("state"):
                raise PreconditionFailed(f"Device '{device.name}' changed after the plan was made.")
            action = "change"

        role_id = payload["role_id"]
        if role_id is None:
            role = DeviceRole.objects.filter(slug=payload["role_slug"]).first()
            if role is None:
                raise PreconditionFailed("The planned Device Role dependency is still absent.")
            role_id = role.pk

        rack_id = payload["rack_id"]
        rack_name = payload["rack_name"]
        if rack_id is None and rack_name:
            rack = (
                Rack.objects.filter(
                    site_id=payload["site_id"],
                    location_id=payload["location_id"],
                    name__iexact=rack_name,
                )
                .select_for_update(of=("self",))
                .first()
            )
            if rack is None:
                raise PreconditionFailed(
                    f"Rack '{rack_name}' is still absent, so '{payload['name']}' cannot be placed."
                )
            rack_id = rack.pk

        if action == "add":
            device.name = payload["name"]
        device.device_type_id = payload["device_type_id"]
        device.role_id = role_id
        device.site_id = payload["site_id"]
        device.location_id = payload["location_id"]
        device.rack_id = rack_id
        device.position = payload["u_position"]
        device.face = payload["face"]
        device.status = payload["status"]
        device.tenant_id = payload["tenant_id"]
        if payload["airflow"]:
            device.airflow = payload["airflow"]
        for field in ("serial", "asset_tag"):
            if payload[field]:
                setattr(device, field, payload[field])
        device.full_clean()
        device.save()
        unassigned = _assign_ips(device, payload.get("ip_fields") or {}, execution_context.actor)
        _apply_contact(device, payload.get("contact"), execution_context)
        _store_provenance(device, payload, unassigned, execution_context)
        # Constraints are only evaluated against the saved row, so this reads the state the row leaves.
        enforce_saved_object_permission(device, execution_context.actor, action)
        return device

    @staticmethod
    def _ip_fields(row, identity, display) -> tuple[dict, tuple[Diagnostic, ...]]:
        """Return addresses in their declared families and warn about invalid values."""
        fields = {}
        diagnostics = []
        for name in ip_assignment.IP_FIELD_FAMILY:
            raw = _source_text(row.get(name))
            if not raw:
                continue
            try:
                address = ip_assignment.normalized_address(name, raw)
            except ip_assignment.IPAssignmentError:
                pass
            else:
                fields[name] = address
                continue
            diagnostics.append(
                Diagnostic(
                    code="device.unparseable_ip",
                    severity=Severity.WARNING,
                    identities=(identity,),
                    display={**display, "field": name, "value": raw},
                )
            )
        return fields, tuple(diagnostics)

    @staticmethod
    def _payload(row, name, dependencies, placement, batch) -> dict:
        """Return the device state this row asks for, resolved to what a write needs."""
        return {
            "name": name,
            "serial": _source_text(row.get("serial")),
            "asset_tag": _source_text(row.get("asset_tag"))[:50],
            "u_position": placement.position,
            "face": placement.face,
            "airflow": placement.airflow,
            "status": placement.status,
            "device_type_id": dependencies.device_type.pk,
            "role_id": dependencies.role.pk,
            "role_slug": dependencies.role_slug,
            "rack_id": dependencies.rack.pk if dependencies.rack is not None else None,
            "rack_name": _source_text(row.get("rack_name")) if dependencies.rack_identity is not None else None,
            "site_id": batch.reader.site.pk if batch.reader.site is not None else None,
            "location_id": batch.reader.location.pk if batch.reader.location is not None else None,
            "tenant_id": batch.reader.tenant.pk if batch.reader.tenant is not None else None,
        }

    @staticmethod
    def _differs(device, payload) -> bool:
        """Return whether the stored device already holds what the row asks for.

        The name is absent on purpose. It is how a row finds a device, and an import reconciles
        the device it matched rather than retitling it.
        """
        if device.device_type_id != payload["device_type_id"] or device.role_id != payload["role_id"]:
            return True
        if payload["rack_name"] is not None:
            return True
        if device.rack_id != payload["rack_id"]:
            return True
        # The target context assigns both fields, including a blank value that clears one.
        if device.location_id != payload["location_id"]:
            return True
        if device.tenant_id != payload["tenant_id"]:
            return True
        if normalize_for_compare(device.position) != normalize_for_compare(payload["u_position"]):
            return True
        if _text(device.status) != payload["status"]:
            return True
        if _text(device.face) != payload["face"]:
            return True
        if payload["airflow"] and _text(device.airflow) != payload["airflow"]:
            return True
        for field in ("serial", "asset_tag"):
            if payload[field] and _text(getattr(device, field)) != payload[field]:
                return True
        if any(
            not ip_assignment.already_assigned(device, field, address)
            for field, address in (payload.get("ip_fields") or {}).items()
        ):
            return True
        return False

    @staticmethod
    def _change(
        identity,
        operation,
        payload,
        device,
        rack_identity=None,
        relation_changes=(),
        profile=None,
    ) -> PlannedChange:
        """Return the one write this unit performs, with the target state it assumed."""
        if device is None:
            preconditions: dict = {"device_id": None}
        else:
            preconditions = {
                "device_id": device.pk,
                "state": DeviceModule._precondition_state(device, profile, payload.get("source_id") or ""),
            }
        dependencies = [change.identity for change in relation_changes]
        if rack_identity is not None and payload["rack_name"] is not None:
            dependencies.append(f"{rack_identity}:create")
        return PlannedChange(
            identity=f"{identity}:{operation}",
            target_module=DeviceModule.key,
            operation=operation,
            payload=payload,
            dependencies=tuple(dependencies),
            preconditions=preconditions,
        )

    @staticmethod
    def _precondition_state(device, profile=None, source_id="") -> dict:
        """Return every Device field and relation this module can overwrite."""
        state = {
            "name": device.name,
            "device_type_id": device.device_type_id,
            "role_id": device.role_id,
            "site_id": device.site_id,
            "location_id": device.location_id,
            "rack_id": device.rack_id,
            "position": normalize_for_compare(device.position),
            "face": device.face or "",
            "status": device.status,
            "tenant_id": device.tenant_id,
            "airflow": device.airflow or "",
            "serial": device.serial or "",
            "asset_tag": device.asset_tag or "",
            "primary_ip4_id": device.primary_ip4_id,
            "primary_ip6_id": device.primary_ip6_id,
            "oob_ip_id": device.oob_ip_id,
        }
        if profile is None:
            return state
        custom_field = profile.adapter_settings.custom_field_name
        state["source_id_custom_field"] = (
            custom_field,
            device.custom_field_data.get(custom_field) if custom_field else None,
        )
        from .models import DeviceImportSource

        stored = DeviceImportSource.objects.filter(device_id=device.pk).first()
        state["provenance"] = (
            {
                "profile_id": stored.profile_id,
                "source_id": stored.source_id,
                "extra_columns": stored.extra_columns,
                "unassigned_ips": stored.unassigned_ips,
            }
            if stored is not None
            else None
        )
        state["source_binding"] = tuple(
            tuple(values)
            for values in profile.device_matches.filter(source_id=source_id).values_list(
                "source_id", "netbox_device_id", "device_name", "source_asset_tag"
            )
        )
        state["device_bindings"] = tuple(
            tuple(values)
            for values in profile.device_matches.filter(netbox_device_id=device.pk).values_list(
                "source_id", "netbox_device_id", "device_name", "source_asset_tag"
            )
        )
        return state

    @staticmethod
    def _create_identity_conflict(payload, profile) -> str:
        """Return the first target identity that would turn a planned create into a duplicate."""
        from dcim.models import Device

        source_id = payload.get("source_id") or ""
        if source_id and profile.device_matches.filter(source_id=source_id).exists():
            return f"A Device link for source ID '{source_id}'"
        if (
            source_id
            and Device.objects.filter(
                data_import_source__profile=profile,
                data_import_source__source_id=source_id,
            ).exists()
        ):
            return f"A Device with stored source ID '{source_id}'"
        serial = payload.get("serial") or ""
        if serial and Device.objects.filter(serial=serial).exists():
            return f"A Device with serial '{serial}'"
        asset_tag = payload.get("asset_tag") or ""
        if asset_tag and Device.objects.filter(asset_tag__iexact=asset_tag).exists():
            return f"A Device with asset tag '{asset_tag}'"
        tenant_filter = (
            {"tenant_id": payload["tenant_id"]} if payload.get("tenant_id") is not None else {"tenant__isnull": True}
        )
        if Device.objects.filter(
            site_id=payload["site_id"],
            name__iexact=payload["name"],
            **tenant_filter,
        ).exists():
            return f"A Device named '{payload['name']}' at the target site and tenant"
        return ""


MODULE_RUNTIMES: dict[str, Any] = {
    RackModule.key: RackModule(),
    DeviceModule.key: DeviceModule(),
    CableModule.key: CableModule(),
}


def runtime_for(key: str) -> Any | None:
    """Return the Target Module runtime registered under *key*, or None."""
    return MODULE_RUNTIMES.get(key)


__all__ = (
    "DEFAULT_RACK_HEIGHT",
    "CableModule",
    "DeviceModule",
    "ExecutionContext",
    "MODULE_RUNTIMES",
    "PreconditionFailed",
    "RackModule",
    "TargetModuleRuntime",
    "rack_duplicate_keys",
    "rack_row_name",
    "rack_row_rejection",
    "rack_unit_identity",
    "runtime_for",
)
