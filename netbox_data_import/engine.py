# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""
Import engine: parse Excel files and run (or preview) imports into NetBox.

Public API
----------
parse_file(file_obj, profile)  ->  list[dict]
run_import(rows, profile, context, dry_run=True)  ->  ImportResult
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal
from io import BytesIO

from django.utils.text import slugify
import openpyxl

from .models import ImportProfile


class ParseError(Exception):
    """Raised when the source file cannot be parsed."""


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class RowResult:
    row_number: int
    source_id: str
    name: str
    action: Literal["create", "update", "skip", "error"]
    object_type: Literal["rack", "device", "manufacturer", "device_type", ""]
    detail: str
    netbox_url: str = ""

    def to_dict(self) -> dict:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, d: dict) -> "RowResult":
        return cls(**d)


@dataclass
class ImportResult:
    rows: list[RowResult] = field(default_factory=list)
    counts: dict = field(default_factory=dict)
    has_errors: bool = False

    def _recompute_counts(self):
        c: dict = {}
        for r in self.rows:
            if r.action == "error":
                c["errors"] = c.get("errors", 0) + 1
            elif r.action == "skip":
                c["skipped"] = c.get("skipped", 0) + 1
            elif r.action in ("create", "update"):
                key = f"{r.object_type}s_{r.action}d"
                c[key] = c.get(key, 0) + 1
        self.counts = c
        self.has_errors = c.get("errors", 0) > 0

    def to_session_dict(self) -> dict:
        # Store parsed rows so the execute step can re-use them
        return {
            "rows": [r.to_dict() for r in self.rows],
            "counts": self.counts,
            "has_errors": self.has_errors,
        }

    @classmethod
    def from_session_dict(cls, d: dict) -> "ImportResult":
        result = cls()
        result.rows = [RowResult.from_dict(r) for r in d.get("rows", [])]
        result.counts = d.get("counts", {})
        result.has_errors = d.get("has_errors", False)
        return result


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_file(file_obj, profile: ImportProfile) -> list[dict]:
    """
    Read the Excel file and return a list of row-dicts keyed by target_field name.
    Raises ParseError if the file or sheet is invalid.
    """
    try:
        content = file_obj.read()
        wb = openpyxl.load_workbook(BytesIO(content), data_only=True)
    except Exception as exc:
        raise ParseError(f"Cannot open Excel file: {exc}") from exc

    if profile.sheet_name not in wb.sheetnames:
        available = ", ".join(wb.sheetnames)
        raise ParseError(
            f"Sheet '{profile.sheet_name}' not found. Available sheets: {available}"
        )

    ws = wb[profile.sheet_name]

    # Build header→column-index map (first occurrence wins for duplicates)
    raw_headers: dict[str, int] = {}
    for idx, cell in enumerate(ws[1]):
        if cell.value is not None:
            header = str(cell.value).strip()
            if header not in raw_headers:
                raw_headers[header] = idx

    # Build source_column→target_field map from profile
    col_map: dict[str, str] = {
        cm.source_column: cm.target_field
        for cm in profile.column_mappings.all()
    }

    rows = []
    for row_num, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        # Skip fully empty rows
        if all(v is None for v in row):
            continue

        row_dict: dict[str, object] = {"_row_number": row_num}
        for source_col, target_field in col_map.items():
            idx = raw_headers.get(source_col)
            if idx is None:
                continue
            value = row[idx] if idx < len(row) else None
            if isinstance(value, str):
                value = value.strip()
            row_dict[target_field] = value

        rows.append(row_dict)

    return rows


# ---------------------------------------------------------------------------
# Device-type slug resolution
# ---------------------------------------------------------------------------

def _resolve_device_type_slugs(make: str, model: str, profile: ImportProfile) -> tuple[str, str, bool]:
    """
    Returns (manufacturer_slug, device_type_slug, is_explicit_mapping).
    Checks DeviceTypeMapping first; falls back to auto-slugify.
    """
    mapping = profile.device_type_mappings.filter(
        source_make=make, source_model=model
    ).first()
    if mapping:
        return mapping.netbox_manufacturer_slug, mapping.netbox_device_type_slug, True

    manufacturer_slug = slugify(make)[:50]
    device_type_slug = slugify(f"{make}-{model}")[:50]
    return manufacturer_slug, device_type_slug, False


# ---------------------------------------------------------------------------
# Main import runner
# ---------------------------------------------------------------------------

def run_import(rows: list[dict], profile: ImportProfile, context: dict, dry_run: bool = True) -> ImportResult:
    """
    Run (or preview) the import.

    context keys: site, location (optional), tenant (optional)
    dry_run=True  → no DB writes, returns what *would* happen
    dry_run=False → writes to DB
    """
    # Lazy imports to avoid circular imports at module load time
    from dcim.models import (
        Device, DeviceRole, DeviceType, Manufacturer, Rack,
    )
    from dcim.choices import DeviceFaceChoices, DeviceAirflowChoices

    site = context["site"]
    location = context.get("location")
    tenant = context.get("tenant")

    # Build class→role lookup from profile
    class_role_map: dict[str, object] = {
        crm.source_class: crm
        for crm in profile.class_role_mappings.all()
    }

    # Value-translation maps (same as original script)
    SIDE_MAP = {
        "front": DeviceFaceChoices.FACE_FRONT,
        "back": DeviceFaceChoices.FACE_REAR,
        "rear": DeviceFaceChoices.FACE_REAR,
    }
    AIRFLOW_MAP = {
        "front to back": DeviceAirflowChoices.AIRFLOW_FRONT_TO_REAR,
        "back to front": DeviceAirflowChoices.AIRFLOW_REAR_TO_FRONT,
        "passive": DeviceAirflowChoices.AIRFLOW_PASSIVE,
    }
    STATUS_MAP = {
        "live": "active",
        "production": "active",
        "planned": "planned",
        "staged": "staged",
        "failed": "failed",
        "offline": "offline",
        "decommissioning": "decommissioning",
    }

    result = ImportResult()

    # ------------------------------------------------------------------
    # Pass 1 – ensure Manufacturer + DeviceType + DeviceRole exist
    # ------------------------------------------------------------------
    seen_manufacturers: set[str] = set()
    seen_device_types: set[tuple] = set()
    seen_roles: set[str] = set()

    for row in rows:
        device_class = str(row.get("device_class", "")).strip()
        crm = class_role_map.get(device_class)
        if crm and crm.creates_rack:
            continue  # racks don't need device type

        make = str(row.get("make", "Unknown")).strip() or "Unknown"
        model = str(row.get("model", "Unknown")).strip() or "Unknown"
        u_height_raw = row.get("u_height", 1)

        try:
            u_height = max(1, int(float(u_height_raw)))
        except (TypeError, ValueError):
            u_height = 1

        mfg_slug, dt_slug, _ = _resolve_device_type_slugs(make, model, profile)

        if mfg_slug not in seen_manufacturers:
            seen_manufacturers.add(mfg_slug)
            if not dry_run:
                Manufacturer.objects.get_or_create(slug=mfg_slug, defaults={"name": make})
            else:
                exists = Manufacturer.objects.filter(slug=mfg_slug).exists()
                if not exists and profile.create_missing_device_types:
                    result.rows.append(RowResult(
                        row_number=row["_row_number"],
                        source_id=str(row.get("source_id", "")),
                        name=make,
                        action="create",
                        object_type="manufacturer",
                        detail=f"Would create manufacturer '{make}' (slug: {mfg_slug})",
                    ))

        dt_key = (mfg_slug, dt_slug)
        if dt_key not in seen_device_types:
            seen_device_types.add(dt_key)
            if not dry_run:
                if profile.create_missing_device_types:
                    mfg, _ = Manufacturer.objects.get_or_create(slug=mfg_slug, defaults={"name": make})
                    DeviceType.objects.get_or_create(
                        manufacturer=mfg,
                        slug=dt_slug,
                        defaults={"model": model, "u_height": u_height},
                    )
            else:
                exists = DeviceType.objects.filter(manufacturer__slug=mfg_slug, slug=dt_slug).exists()
                if not exists:
                    if profile.create_missing_device_types:
                        result.rows.append(RowResult(
                            row_number=row["_row_number"],
                            source_id=str(row.get("source_id", "")),
                            name=f"{make} / {model}",
                            action="create",
                            object_type="device_type",
                            detail=f"Would create device type '{model}' under '{make}'",
                        ))

        if crm and crm.role_slug and crm.role_slug not in seen_roles:
            seen_roles.add(crm.role_slug)
            if not dry_run:
                DeviceRole.objects.get_or_create(
                    slug=crm.role_slug,
                    defaults={"name": crm.role_slug.replace("-", " ").title(), "color": "9e9e9e"},
                )

    # ------------------------------------------------------------------
    # Pass 2 – Racks
    # ------------------------------------------------------------------
    rack_map: dict[str, object] = {}  # source rack_name → Rack (or name in dry_run)

    for row in rows:
        device_class = str(row.get("device_class", "")).strip()
        crm = class_role_map.get(device_class)
        if not (crm and crm.creates_rack):
            continue

        rack_name = str(row.get("rack_name", "")).strip()
        source_id = str(row.get("source_id", ""))
        u_height_raw = row.get("u_height", 42)
        serial = str(row.get("serial", "")).strip()

        try:
            u_height = max(1, int(float(u_height_raw)))
        except (TypeError, ValueError):
            u_height = 42

        if not rack_name:
            result.rows.append(RowResult(
                row_number=row["_row_number"],
                source_id=source_id,
                name="",
                action="error",
                object_type="rack",
                detail="Missing rack name",
            ))
            continue

        if dry_run:
            try:
                rack = Rack.objects.get(site=site, name=rack_name)
                action = "update" if profile.update_existing else "skip"
                detail = f"Rack '{rack_name}' already exists"
                rack_map[rack_name] = rack_name
            except Rack.DoesNotExist:
                action = "create"
                detail = f"Would create rack '{rack_name}' ({u_height}U) at site '{site}'"
                rack_map[rack_name] = rack_name
            result.rows.append(RowResult(
                row_number=row["_row_number"],
                source_id=source_id,
                name=rack_name,
                action=action,
                object_type="rack",
                detail=detail,
            ))
        else:
            try:
                rack = Rack.objects.get(site=site, name=rack_name)
                if profile.update_existing:
                    rack.u_height = u_height
                    rack.serial = serial or rack.serial
                    if location:
                        rack.location = location
                    if tenant:
                        rack.tenant = tenant
                    rack.save()
                    rack_map[rack_name] = rack
                    result.rows.append(RowResult(
                        row_number=row["_row_number"],
                        source_id=source_id,
                        name=rack_name,
                        action="update",
                        object_type="rack",
                        detail=f"Updated rack '{rack_name}'",
                        netbox_url=rack.get_absolute_url(),
                    ))
                else:
                    rack_map[rack_name] = rack
                    result.rows.append(RowResult(
                        row_number=row["_row_number"],
                        source_id=source_id,
                        name=rack_name,
                        action="skip",
                        object_type="rack",
                        detail=f"Rack '{rack_name}' already exists (update_existing=False)",
                    ))
            except Rack.DoesNotExist:
                rack = Rack.objects.create(
                    site=site,
                    location=location,
                    name=rack_name,
                    tenant=tenant,
                    u_height=u_height,
                    serial=serial,
                )
                _store_source_id(rack, profile, source_id)
                rack_map[rack_name] = rack
                result.rows.append(RowResult(
                    row_number=row["_row_number"],
                    source_id=source_id,
                    name=rack_name,
                    action="create",
                    object_type="rack",
                    detail=f"Created rack '{rack_name}' ({u_height}U)",
                    netbox_url=rack.get_absolute_url(),
                ))

    # ------------------------------------------------------------------
    # Pass 3 – Devices
    # ------------------------------------------------------------------
    for row in rows:
        device_class = str(row.get("device_class", "")).strip()
        crm = class_role_map.get(device_class)
        if crm and crm.creates_rack:
            continue  # already handled as rack

        source_id = str(row.get("source_id", ""))
        device_name = str(row.get("device_name", "")).strip()
        rack_name = str(row.get("rack_name", "")).strip()
        make = str(row.get("make", "Unknown")).strip() or "Unknown"
        model = str(row.get("model", "Unknown")).strip() or "Unknown"
        serial = str(row.get("serial", "")).strip()
        asset_tag_raw = str(row.get("asset_tag", "")).strip() or None
        asset_tag = asset_tag_raw[:50] if asset_tag_raw else None

        u_position_raw = row.get("u_position")
        try:
            position = int(float(u_position_raw))
            if position < 1:
                result.rows.append(RowResult(
                    row_number=row["_row_number"],
                    source_id=source_id,
                    name=device_name,
                    action="skip",
                    object_type="device",
                    detail=f"Skipped: position {position} < 1 (under-rack/blanking panel)",
                ))
                continue
        except (TypeError, ValueError):
            position = None

        status_raw = str(row.get("status", "")).strip().lower()
        device_status = STATUS_MAP.get(status_raw, "active")

        face_raw = str(row.get("face", "")).strip().lower()
        device_face = SIDE_MAP.get(face_raw)

        airflow_raw = str(row.get("airflow", "")).strip().lower()
        device_airflow = AIRFLOW_MAP.get(airflow_raw)

        if not device_name:
            result.rows.append(RowResult(
                row_number=row["_row_number"],
                source_id=source_id,
                name="",
                action="error",
                object_type="device",
                detail="Missing device name",
            ))
            continue

        if not crm:
            result.rows.append(RowResult(
                row_number=row["_row_number"],
                source_id=source_id,
                name=device_name,
                action="error",
                object_type="device",
                detail=f"No class→role mapping for class '{device_class}'",
            ))
            continue

        mfg_slug, dt_slug, _ = _resolve_device_type_slugs(make, model, profile)

        if dry_run:
            # Check if device type exists
            dt_exists = DeviceType.objects.filter(manufacturer__slug=mfg_slug, slug=dt_slug).exists()
            if not dt_exists and not profile.create_missing_device_types:
                result.rows.append(RowResult(
                    row_number=row["_row_number"],
                    source_id=source_id,
                    name=device_name,
                    action="error",
                    object_type="device",
                    detail=f"Device type not found: {make} / {model} (slug: {mfg_slug}/{dt_slug})",
                ))
                continue

            rack_label = rack_name if rack_name in rack_map else f"{rack_name} (not found)"
            try:
                Device.objects.get(site=site, name=device_name)
                action = "update" if profile.update_existing else "skip"
                detail = f"Device '{device_name}' already exists"
            except Device.DoesNotExist:
                action = "create"
                detail = f"Would create device '{device_name}' in {rack_label} U{position}"

            result.rows.append(RowResult(
                row_number=row["_row_number"],
                source_id=source_id,
                name=device_name,
                action=action,
                object_type="device",
                detail=detail,
            ))
        else:
            # Resolve device type
            try:
                device_type = DeviceType.objects.get(manufacturer__slug=mfg_slug, slug=dt_slug)
            except DeviceType.DoesNotExist:
                result.rows.append(RowResult(
                    row_number=row["_row_number"],
                    source_id=source_id,
                    name=device_name,
                    action="error",
                    object_type="device",
                    detail=f"Device type not found: {mfg_slug}/{dt_slug}",
                ))
                continue

            # Resolve role
            try:
                device_role = DeviceRole.objects.get(slug=crm.role_slug)
            except DeviceRole.DoesNotExist:
                result.rows.append(RowResult(
                    row_number=row["_row_number"],
                    source_id=source_id,
                    name=device_name,
                    action="error",
                    object_type="device",
                    detail=f"Device role not found: {crm.role_slug}",
                ))
                continue

            rack = rack_map.get(rack_name) if rack_name else None

            try:
                device = Device.objects.get(site=site, name=device_name)
                if profile.update_existing:
                    device.device_type = device_type
                    device.role = device_role
                    device.rack = rack if isinstance(rack, Rack) else None
                    device.position = position
                    device.face = device_face
                    device.airflow = device_airflow
                    device.status = device_status
                    device.serial = serial or device.serial
                    if asset_tag:
                        device.asset_tag = asset_tag
                    if tenant:
                        device.tenant = tenant
                    device.save()
                    _store_source_id(device, profile, source_id)
                    result.rows.append(RowResult(
                        row_number=row["_row_number"],
                        source_id=source_id,
                        name=device_name,
                        action="update",
                        object_type="device",
                        detail=f"Updated device '{device_name}'",
                        netbox_url=device.get_absolute_url(),
                    ))
                else:
                    result.rows.append(RowResult(
                        row_number=row["_row_number"],
                        source_id=source_id,
                        name=device_name,
                        action="skip",
                        object_type="device",
                        detail=f"Device '{device_name}' already exists (update_existing=False)",
                    ))
            except Device.DoesNotExist:
                device = Device.objects.create(
                    site=site,
                    location=location,
                    name=device_name,
                    device_type=device_type,
                    role=device_role,
                    rack=rack if isinstance(rack, Rack) else None,
                    position=position,
                    face=device_face,
                    airflow=device_airflow,
                    status=device_status,
                    serial=serial,
                    asset_tag=asset_tag,
                    tenant=tenant,
                )
                _store_source_id(device, profile, source_id)
                result.rows.append(RowResult(
                    row_number=row["_row_number"],
                    source_id=source_id,
                    name=device_name,
                    action="create",
                    object_type="device",
                    detail=f"Created device '{device_name}' in {rack_name} U{position}",
                    netbox_url=device.get_absolute_url(),
                ))

    result._recompute_counts()
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _store_source_id(obj, profile: ImportProfile, source_id: str):
    """Store source ID in the configured custom field if present."""
    if not profile.custom_field_name or not source_id:
        return
    try:
        obj.custom_field_data[profile.custom_field_name] = source_id
        obj.save(update_fields=["custom_field_data"])
    except (AttributeError, KeyError):
        pass
