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

from django.core.exceptions import ValidationError

from . import ip_assignment
from .catalog import OutputKind
from .contact_resolution import ContactResolutionRequired, ContactReview, ContactSelection, PrimaryContactResolver
from .device_field_review import DeviceFieldReviewer
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
    profile: Any


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


_REVIEWED_PAYLOAD_FIELDS: dict[str, tuple[str, Any]] = {
    "serial": ("serial", lambda device: _text(device.serial)),
    "asset_tag": ("asset_tag", lambda device: _text(device.asset_tag)),
    "u_position": ("u_position", lambda device: device.position),
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

    def __init__(self, rows, profile, netbox_reader):
        self.profile = profile
        self.reader = netbox_reader
        self.ignored = _ignored_source_ids(profile)
        self._identity = DeviceTypeIdentityResolver.for_profile(profile)
        self._reviewer = DeviceFieldReviewer.for_profile(profile)
        self._candidate_columns = PrimaryContactResolver.candidate_source_columns(profile)
        self._stored_by_source = self._stored_sources(rows, profile, netbox_reader)
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
    def _stored_sources(rows, profile, netbox_reader) -> dict[str, list]:
        """Return the devices an earlier import of this profile wrote, by the source ID it stored."""
        source_ids = {_text(row.get("source_id")) for row in rows}
        source_ids.discard("")
        if not source_ids:
            return {}
        devices = (
            netbox_reader.devices()
            .filter(data_import_source__profile=profile, data_import_source__source_id__in=source_ids)
            .select_related("data_import_source")
        )
        stored: dict[str, list] = {}
        for device in devices:
            stored.setdefault(_text(device.data_import_source.source_id), []).append(device)
        return stored

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
            # NetBox refuses a rack face on a device in no rack, so the plan never asks for one.
            return _Placement(position=None, face=face if rack is not None else "", airflow=airflow, status=status)
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

        stored = self._stored_by_source.get(source_id, ()) if source_id else ()
        if len(stored) > 1:
            return _Match(ambiguous="device.ambiguous_stored_source_id", value=source_id)
        if stored:
            return _Match(device=stored[0])

        reviewed_ids = self._reviewer.review_device_ids(source_id) if source_id else frozenset()
        if len(reviewed_ids) > 1:
            return _Match(ambiguous="device.ambiguous_field_review", value=source_id)
        if reviewed_ids:
            reviewed = devices.filter(pk=next(iter(reviewed_ids))).first()
            if reviewed is not None:
                return _Match(device=reviewed)

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

    def review(self, row, device, dependencies, placement, payload):
        """Return the operator's saved review for one matched device, in the reviewer's terms."""
        device_type = dependencies.device_type
        manufacturer = device_type.manufacturer
        ip_fields = payload.get("ip_fields") or {}
        proposal = {
            "device_name": payload["name"],
            "serial": payload["serial"],
            "asset_tag": payload["asset_tag"],
            "u_position": placement.position,
            "face": placement.face,
            "airflow": placement.airflow,
            "status": placement.status,
            "rack_name": _text(row.get("rack_name")),
            "_rack_location_id": self.reader.location.pk if self.reader.location is not None else None,
            "device_type": (manufacturer.slug, device_type.slug, manufacturer.name, device_type.model),
            "role": _text(dependencies.role.slug),
            "tenant": self.reader.tenant,
            "location": self.reader.location,
            **{name: ip_fields.get(name, "") for name in ip_assignment.IP_FIELD_FAMILY},
        }
        return self._reviewer.review(_text(row.get("source_id")), device, proposal)

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
        try:
            contact = batch.contact_review(row, match.device)
        except ContactResolutionRequired as exc:
            return _refused(
                identity,
                "device.contact_resolution_required",
                {**display, "candidate_values": exc.candidate_values},
            )
        except ValidationError as exc:
            return _refused(identity, "device.contact_invalid", {**display, "error": "; ".join(exc.messages)})
        ip_fields, ip_diagnostics = self._ip_fields(row, identity, display)
        payload = {
            **payload,
            "contact": _contact_payload(contact),
            "source_id": source_id,
            "extra_columns": contact.extra_columns,
            "ip_fields": ip_fields,
        }
        if match.device is None:
            return SynchronizationUnit(
                identity=identity,
                disposition=Disposition.ACTIONABLE,
                changes=(self._change(identity, "create", payload, None),),
                diagnostics=ip_diagnostics,
            )
        review = batch.review(row, match.device, dependencies, placement, payload)
        payload = _reviewed_payload(payload, review, match.device)
        if (
            not self._differs(match.device, payload)
            and _contact_writes_nothing(contact)
            and _provenance_is_current(match.device, payload, batch.profile)
        ):
            return SynchronizationUnit(
                identity=identity,
                disposition=Disposition.NO_OP,
                diagnostics=ip_diagnostics,
            )
        return SynchronizationUnit(
            identity=identity,
            disposition=Disposition.ACTIONABLE,
            changes=(self._change(identity, "update", payload, match.device),),
            diagnostics=ip_diagnostics,
        )

    def apply(self, planned_change: PlannedChange, execution_context) -> Any:
        """Apply one device change, having locked its row and rechecked its preconditions."""
        from dcim.models import Device

        from .object_permissions import enforce_saved_object_permission

        payload = planned_change.payload
        device_id = planned_change.preconditions.get("device_id")
        if device_id is None:
            device = Device()
            action = "add"
        else:
            # `of=("self",)` because NetBox's default Device queryset outer-joins, which cannot be locked.
            device = Device.objects.filter(pk=device_id).select_for_update(of=("self",)).first()
            if device is None:
                raise PreconditionFailed(f"Device {device_id} is gone, so '{payload['name']}' cannot be updated.")
            if device.device_type_id != planned_change.preconditions.get("device_type_id"):
                raise PreconditionFailed(f"Device '{device.name}' changed type since the plan was made.")
            action = "change"

        if action == "add":
            device.name = payload["name"]
        device.device_type_id = payload["device_type_id"]
        device.role_id = payload["role_id"]
        device.site_id = payload["site_id"]
        device.location_id = payload["location_id"]
        device.rack_id = payload["rack_id"]
        device.position = payload["u_position"]
        device.face = payload["face"]
        device.status = payload["status"]
        if payload["tenant_id"] is not None:
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
        """Return parsed address fields and warnings for non-empty values that do not parse."""
        fields = {}
        diagnostics = []
        for name in ip_assignment.IP_FIELD_FAMILY:
            raw = _text(row.get(name))
            if not raw:
                continue
            address = ip_assignment.parse_address(raw)
            if address is not None:
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
        """Return whether the stored device already holds what the row asks for.

        The name is absent on purpose. It is how a row finds a device, and an import reconciles
        the device it matched rather than retitling it.
        """
        if device.device_type_id != payload["device_type_id"] or device.role_id != payload["role_id"]:
            return True
        if payload["rack_id"] is not None and device.rack_id != payload["rack_id"]:
            return True
        # The write assigns the target's location always and its tenant only when there is one.
        if device.location_id != payload["location_id"]:
            return True
        if payload["tenant_id"] is not None and device.tenant_id != payload["tenant_id"]:
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
        if any(
            not ip_assignment.already_assigned(device, field, address)
            for field, address in (payload.get("ip_fields") or {}).items()
        ):
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


MODULE_RUNTIMES: dict[str, Any] = {
    RackModule.key: RackModule(),
    DeviceModule.key: DeviceModule(),
}


def runtime_for(key: str) -> Any | None:
    """Return the Target Module runtime registered under *key*, or None."""
    return MODULE_RUNTIMES.get(key)


__all__ = (
    "DEFAULT_RACK_HEIGHT",
    "DeviceModule",
    "ExecutionContext",
    "MODULE_RUNTIMES",
    "PreconditionFailed",
    "RackModule",
    "TargetModuleRuntime",
    "runtime_for",
)
