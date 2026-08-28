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

from dataclasses import dataclass
from typing import Any, Protocol

from .catalog import OutputKind
from .device_identity import DeviceTypeIdentityResolver
from .plan import Diagnostic, Disposition, PlannedChange, Severity, SynchronizationUnit
from .values import normalize_for_compare, translation_maps

DEFAULT_RACK_HEIGHT = 42


class PreconditionFailed(Exception):
    """Target state moved between planning and the write, so the change no longer applies."""


@dataclass(frozen=True)
class ExecutionContext:
    """What a Target Module needs while the coordinator's transaction is open."""

    actor: Any
    reader: Any


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


def _ignored_source_ids(profile) -> frozenset[str]:
    """Return the source identities the operator has chosen to skip."""
    return frozenset(_text(value) for value in profile.ignored_devices.values_list("source_id", flat=True))


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


def _coerce_position(value):
    """Return the rack position the row asks for, or None when it names none."""
    text = _text(value)
    if not text:
        return None
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None
    return int(number) if number == int(number) else number


def _translate(value, table) -> str:
    """Return the NetBox value a source word names, or the word itself when it is already one."""
    text = _text(value).lower()
    if not text:
        return ""
    mapped = table.get(text)
    if mapped is not None:
        return mapped
    return text if text in set(table.values()) else ""


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
        ignored = _ignored_source_ids(profile)
        duplicate_names = _repeated(_identity_text(row.get("rack_name")) or "" for row in rows)
        duplicate_source_ids = _repeated(_text(row.get("source_id")) for row in rows)
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
            return _refused(identity, "rack.missing_name", {"source_id": source_id})
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
            return _refused(identity, "rack.duplicate_name", {"rack_name": name, "source_id": source_id})
        if source_id and source_id in duplicate_source_ids:
            return _refused(identity, "rack.duplicate_source_id", {"rack_name": name, "source_id": source_id})

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

    def apply(self, planned_change: PlannedChange, execution_context) -> Any:
        """Apply one rack change, having locked its row and rechecked its preconditions."""
        from dcim.models import Rack

        from .object_permissions import enforce_saved_object_permission

        payload = planned_change.payload
        rack_id = planned_change.preconditions.get("rack_id")
        if rack_id is None:
            rack = Rack(site_id=payload["site_id"], location_id=payload["location_id"])
            action = "add"
        else:
            # `of=("self",)` because NetBox's default Rack queryset outer-joins, which cannot be locked.
            rack = Rack.objects.filter(pk=rack_id).select_for_update(of=("self",)).first()
            if rack is None:
                raise PreconditionFailed(f"Rack {rack_id} is gone, so '{payload['name']}' cannot be updated.")
            if rack.u_height != planned_change.preconditions.get("u_height"):
                raise PreconditionFailed(f"Rack '{rack.name}' changed height since the plan was made.")
            action = "change"

        rack.name = payload["name"]
        rack.u_height = payload["u_height"]
        if payload["serial"]:
            rack.serial = payload["serial"]
        if payload["tenant_id"] is not None:
            rack.tenant_id = payload["tenant_id"]
        rack.full_clean()
        rack.save()
        # An ObjectPermission's constraints are only evaluated against the saved row.
        enforce_saved_object_permission(rack, execution_context.actor, action)
        return rack

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
    )


def _blocked(identity, code, display) -> SynchronizationUnit:
    """Return a unit waiting on a dependency this module does not create."""
    return SynchronizationUnit(
        identity=identity,
        disposition=Disposition.BLOCKED,
        diagnostics=(Diagnostic(code=code, severity=Severity.ERROR, identities=(identity,), display=display),),
    )


@dataclass(frozen=True)
class _Dependencies:
    """What a device row needs to already exist, or the first thing that does not."""

    device_type: Any = None
    role: Any = None
    rack: Any = None
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
class _Match:
    """The stored device a row reconciles, or the reason no automatic answer is safe."""

    device: Any = None
    ambiguous: str | None = None
    value: str = ""


class _DeviceBatch:
    """The batch-wide state every device row is planned against, loaded once."""

    _CLASH_FIELDS = (
        ("source_id", "device.duplicate_source_id", False),
        ("serial", "device.duplicate_serial", False),
        ("asset_tag", "device.duplicate_asset_tag", True),
    )

    def __init__(self, rows, profile, netbox_reader):
        self.profile = profile
        self.reader = netbox_reader
        self.ignored = _ignored_source_ids(profile)
        self._identity = DeviceTypeIdentityResolver.for_profile(profile)
        self._roles = {mapping.source_class: _text(mapping.role_slug) for mapping in profile.class_role_mappings.all()}
        self._clashes = {field: self._rows_by_value(rows, field, fold) for field, _code, fold in self._CLASH_FIELDS}
        self._bindings = {_text(match.source_id): match.netbox_device_id for match in profile.device_matches.all()}
        self._duplicate_names = _repeated(_identity_text(row.get("device_name")) for row in rows)
        self._racks = self._racks_by_name(netbox_reader)
        self.side_map, self.airflow_map, self.status_map = translation_maps()
        # Row order decides who keeps a slot two rows claim, so the first row planned wins it.
        self._claimed: dict[tuple[int, str | None, Any], int] = {}

    @staticmethod
    def _rows_by_value(rows, field, fold) -> dict[str, list[int]]:
        """Return the source row numbers each non-empty value of *field* appears on."""
        found: dict[str, list[int]] = {}
        for row in rows:
            value = _identity_text(row.get(field)) if fold else _text(row.get(field))
            if value:
                found.setdefault(value, []).append(row.get("_row_number"))
        return {value: numbers for value, numbers in found.items() if len(numbers) > 1}

    @staticmethod
    def _racks_by_name(netbox_reader) -> dict[str, Any]:
        """Return the racks the actor may view at the import site, keyed by comparison name."""
        if netbox_reader.site is None:
            return {}
        racks = netbox_reader.racks().filter(site=netbox_reader.site)
        if netbox_reader.location is not None:
            racks = racks.filter(location=netbox_reader.location)
        return {_identity_text(rack.name): rack for rack in racks}

    def clash(self, row) -> tuple[str, str, list[int]] | None:
        """Return the first identity another row in this batch also claims."""
        for field, code, fold in self._CLASH_FIELDS:
            value = _identity_text(row.get(field)) if fold else _text(row.get(field))
            numbers = self._clashes[field].get(value) if value else None
            if numbers:
                return code, value, numbers
        return None

    def dependencies(self, row) -> _Dependencies:
        """Return the Device Type, Role and Rack the row needs, or the first one missing."""
        from dcim.models import DeviceRole, DeviceType

        make = " ".join((_text(row.get("make")) or "Unknown").split())
        model = " ".join((_text(row.get("model")) or "Unknown").split())
        mfg_slug, dt_slug, _explicit = self._identity.resolve(make, model)
        device_type = DeviceType.objects.filter(manufacturer__slug=mfg_slug, slug=dt_slug).first()
        if device_type is None:
            return _Dependencies(missing=("device.device_type_missing", {"mfg_slug": mfg_slug, "dt_slug": dt_slug}))

        role_slug = self._roles.get(_text(row.get("device_class")), "")
        role = DeviceRole.objects.filter(slug=role_slug).first() if role_slug else None
        if role is None:
            return _Dependencies(missing=("device.role_missing", {"role_slug": role_slug}))

        rack_name = _text(row.get("rack_name"))
        rack = self._racks.get(_identity_text(rack_name)) if rack_name else None
        if rack_name and rack is None:
            return _Dependencies(missing=("device.rack_missing", {"rack_name": rack_name}))
        return _Dependencies(device_type=device_type, role=role, rack=rack)

    def placement(self, row, device_type, rack) -> _Placement:
        """Return where this row puts the device, or the first reason it cannot go there."""
        zero_u = device_type.u_height == 0
        position = None if zero_u else _coerce_position(row.get("u_position"))
        face = "" if zero_u else _translate(row.get("face"), self.side_map)
        airflow = _translate(row.get("airflow"), self.airflow_map)
        status = _translate(row.get("status"), self.status_map) or "active"

        if position is None:
            return _Placement(position=None, face=face, airflow=airflow, status=status)
        if rack is None:
            return _Placement(refused=("device.rack_required", {"u_position": position}))
        if not face:
            return _Placement(refused=("device.face_required", {"u_position": position}))
        return _Placement(position=position, face=face, airflow=airflow, status=status)

    def claim(self, row, rack, placement, device_type, matched) -> tuple[str, dict] | None:
        """Take the units this row occupies, or name what already holds them."""
        if rack is None or placement.position is None or device_type.u_height == 0:
            return None
        rack_face = None if device_type.is_full_depth else placement.face
        for unit in _occupied_units(placement.position, device_type.u_height):
            key = (rack.pk, rack_face, unit)
            claimed_by = self._claimed.get(key)
            if claimed_by is not None:
                return "device.rack_position_claimed", {"u_position": placement.position, "claimed_by_row": claimed_by}
            self._claimed[key] = row.get("_row_number")

        available = rack.get_available_units(
            u_height=device_type.u_height,
            rack_face=rack_face,
            exclude=[matched.pk] if matched is not None else [],
        )
        if placement.position not in available:
            return "device.rack_position_occupied", {"u_position": placement.position, "rack_name": rack.name}
        return None

    def match(self, row, name) -> _Match:
        """Return the stored device this row reconciles, strongest identifier first."""
        devices = self.reader.devices()
        source_id = _text(row.get("source_id"))

        bound_id = self._bindings.get(source_id) if source_id else None
        if bound_id is not None:
            bound = devices.filter(pk=bound_id).first()
            if bound is not None:
                return _Match(device=bound)

        for field, lookup, code in (
            ("serial", "serial", "device.ambiguous_serial"),
            ("asset_tag", "asset_tag__iexact", "device.ambiguous_asset_tag"),
        ):
            value = _text(row.get(field))
            if not value:
                continue
            found = list(devices.filter(**{lookup: value})[:2])
            if len(found) > 1:
                return _Match(ambiguous=code, value=value)
            if found:
                return _Match(device=found[0])

        if _identity_text(name) in self._duplicate_names or self.reader.site is None:
            return _Match()
        by_name = list(devices.filter(name__iexact=name, site=self.reader.site)[:2])
        if len(by_name) > 1:
            return _Match(ambiguous="device.ambiguous_name", value=name)
        return _Match(device=by_name[0] if by_name else None)


class DeviceModule:
    """Plans the Devices a flat source batch describes.

    Section 4.4 makes a Device Type, a Device Role and a Rack dependencies of a device, not part of
    it, so a device row that names one NetBox does not hold is blocked rather than invalid. Creating
    them is another unit's work.
    """

    key = "device"
    consumes = frozenset({OutputKind.DEVICE_SOURCE_ROW})

    def plan(self, source_batch, profile, catalog, netbox_reader) -> list[SynchronizationUnit]:
        """Return one Synchronization Unit per device row, with the disposition its state earns."""
        rows = self._device_rows(source_batch, profile)
        if not rows:
            return []
        batch = _DeviceBatch(rows, profile, netbox_reader)
        return [self._unit(row, batch) for row in rows]

    @staticmethod
    def _device_rows(source_batch, profile) -> list[dict]:
        """Return the batch rows whose class the profile maps to a device."""
        device_classes = {
            mapping.source_class: mapping
            for mapping in profile.class_role_mappings.all()
            if not mapping.creates_rack and not mapping.ignore
        }
        return [row for row in source_batch.rows if _text(row.get("device_class")) in device_classes]

    @staticmethod
    def unit_identity(row) -> str:
        """Return the identity that survives replanning, which is never the row number."""
        source_id = _text(row.get("source_id"))
        if source_id:
            return f"device:source:{source_id}"
        return f"device:name:{_identity_text(row.get('device_name'))}"

    def _unit(self, row, batch) -> SynchronizationUnit:
        """Return the one unit this row produces."""
        identity = self.unit_identity(row)
        name = _text(row.get("device_name"))
        source_id = _text(row.get("source_id"))
        display = {"device_name": name, "source_id": source_id}

        if not name:
            return _refused(identity, "device.missing_name", display)
        if source_id and source_id in batch.ignored:
            return SynchronizationUnit(
                identity=identity,
                disposition=Disposition.EXCLUDED,
                diagnostics=(
                    Diagnostic(
                        code="device.ignored",
                        severity=Severity.INFO,
                        identities=(identity,),
                        display=display,
                    ),
                ),
            )

        clash = batch.clash(row)
        if clash is not None:
            code, value, numbers = clash
            return _refused(identity, code, {**display, "value": value, "rows": numbers})

        dependencies = batch.dependencies(row)
        if dependencies.missing is not None:
            code, missing_display = dependencies.missing
            return _blocked(identity, code, {**display, **missing_display})

        match = batch.match(row, name)
        if match.ambiguous is not None:
            return _refused(identity, match.ambiguous, {**display, "value": match.value})

        placement = batch.placement(row, dependencies.device_type, dependencies.rack)
        if placement.refused is not None:
            code, placement_display = placement.refused
            return _refused(identity, code, {**display, **placement_display})
        taken = batch.claim(row, dependencies.rack, placement, dependencies.device_type, match.device)
        if taken is not None:
            code, taken_display = taken
            return _refused(identity, code, {**display, **taken_display})

        payload = self._payload(row, name, dependencies, placement, batch)
        if match.device is None:
            return SynchronizationUnit(
                identity=identity,
                disposition=Disposition.ACTIONABLE,
                changes=(self._change(identity, "create", payload, None),),
            )
        if not self._differs(match.device, payload):
            return SynchronizationUnit(identity=identity, disposition=Disposition.NO_OP)
        return SynchronizationUnit(
            identity=identity,
            disposition=Disposition.ACTIONABLE,
            changes=(self._change(identity, "update", payload, match.device),),
        )

    @staticmethod
    def _payload(row, name, dependencies, placement, batch) -> dict:
        """Return the device state this row asks for, resolved to what a write needs."""
        return {
            "name": name,
            "serial": _text(row.get("serial")),
            "asset_tag": _text(row.get("asset_tag")),
            "u_position": placement.position,
            "face": placement.face,
            "airflow": placement.airflow,
            "status": placement.status,
            "device_type_id": dependencies.device_type.pk,
            "role_id": dependencies.role.pk,
            "rack_id": dependencies.rack.pk if dependencies.rack is not None else None,
            "site_id": batch.reader.site.pk if batch.reader.site is not None else None,
            "location_id": batch.reader.location.pk if batch.reader.location is not None else None,
            "tenant_id": batch.reader.tenant.pk if batch.reader.tenant is not None else None,
        }

    @staticmethod
    def _differs(device, payload) -> bool:
        """Return whether the stored device already holds what the row asks for."""
        if _identity_text(device.name) != _identity_text(payload["name"]):
            return True
        if device.device_type_id != payload["device_type_id"] or device.role_id != payload["role_id"]:
            return True
        if payload["rack_id"] is not None and device.rack_id != payload["rack_id"]:
            return True
        if normalize_for_compare(device.position) != normalize_for_compare(payload["u_position"]):
            return True
        if _text(device.status) != payload["status"]:
            return True
        for field, stored in (("face", device.face), ("airflow", device.airflow)):
            if payload[field] and _text(stored) != payload[field]:
                return True
        for field in ("serial", "asset_tag"):
            if payload[field] and _text(getattr(device, field)) != payload[field]:
                return True
        return False

    @staticmethod
    def _change(identity, operation, payload, device) -> PlannedChange:
        """Return the one write this unit performs, with the target state it assumed."""
        if device is None:
            preconditions: dict = {"device_id": None}
        else:
            preconditions = {"device_id": device.pk, "device_type_id": device.device_type_id}
        return PlannedChange(
            identity=f"{identity}:{operation}",
            target_module=DeviceModule.key,
            operation=operation,
            payload=payload,
            preconditions=preconditions,
        )


__all__ = (
    "DEFAULT_RACK_HEIGHT",
    "DeviceModule",
    "ExecutionContext",
    "PreconditionFailed",
    "RackModule",
    "TargetModuleRuntime",
)
