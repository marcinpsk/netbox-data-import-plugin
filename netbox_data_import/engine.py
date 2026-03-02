# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Import engine: parse Excel files and run (or preview) imports into NetBox.

Public API
----------
parse_file(file_obj, profile)  ->  list[dict]
run_import(rows, profile, context, dry_run=True)  ->  ImportResult
"""

from __future__ import annotations

import re
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
    """Holds the result of processing a single source row."""

    row_number: int
    source_id: str
    name: str
    action: Literal["create", "update", "skip", "error", "ignore"]
    object_type: Literal["rack", "device", "manufacturer", "device_type", ""]
    detail: str
    netbox_url: str = ""
    rack_name: str = ""
    # Contextual metadata used by the preview template for inline quick-fix actions
    extra_data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Serialize this result to a plain dict."""
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, d: dict) -> "RowResult":
        """Deserialize a RowResult from a plain dict."""
        d = dict(d)
        d.setdefault("extra_data", {})
        return cls(**d)


@dataclass
class ImportResult:
    """Aggregates all RowResult objects and summary counts for an import run."""

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
            elif r.action == "ignore":
                c["ignored"] = c.get("ignored", 0) + 1
            elif r.action in ("create", "update"):
                key = f"{r.object_type}s_{r.action}d"
                c[key] = c.get(key, 0) + 1
        self.counts = c
        self.has_errors = c.get("errors", 0) > 0

    def to_session_dict(self) -> dict:
        """Serialize this result to a session-safe dict."""
        # Store parsed rows so the execute step can re-use them
        return {
            "rows": [r.to_dict() for r in self.rows],
            "counts": self.counts,
            "has_errors": self.has_errors,
        }

    @classmethod
    def from_session_dict(cls, d: dict) -> "ImportResult":
        """Deserialize an ImportResult from a session-stored dict."""
        result = cls()
        result.rows = [RowResult.from_dict(r) for r in d.get("rows", [])]
        result.counts = d.get("counts", {})
        result.has_errors = d.get("has_errors", False)
        return result

    @property
    def rack_groups(self) -> dict:
        """Return rows grouped by rack name for the rack view template."""
        groups: dict = {}
        for row in self.rows:
            if row.object_type == "rack":
                if row.name not in groups:
                    groups[row.name] = {"rack_row": row, "devices": []}
                else:
                    groups[row.name]["rack_row"] = row
            elif row.object_type == "device":
                rack = row.rack_name or "(No rack)"
                if rack not in groups:
                    groups[rack] = {"rack_row": None, "devices": []}
                groups[rack]["devices"].append(row)
        return groups


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_file(file_obj, profile: ImportProfile) -> list[dict]:
    """Read the Excel file and return a list of row-dicts keyed by target_field name.

    Raises ParseError if the file or sheet is invalid.
    """
    try:
        content = file_obj.read()
        wb = openpyxl.load_workbook(BytesIO(content), data_only=True)
    except Exception as exc:
        raise ParseError(f"Cannot open Excel file: {exc}") from exc

    if profile.sheet_name not in wb.sheetnames:
        available = ", ".join(wb.sheetnames)
        raise ParseError(f"Sheet '{profile.sheet_name}' not found. Available sheets: {available}")

    ws = wb[profile.sheet_name]

    # Build header→column-index map (first occurrence wins for duplicates)
    raw_headers: dict[str, int] = {}
    for idx, cell in enumerate(ws[1]):
        if cell.value is not None:
            header = str(cell.value).strip()
            if header not in raw_headers:
                raw_headers[header] = idx

    # Build source_column→target_field map from profile
    col_map: dict[str, str] = {cm.source_column: cm.target_field for cm in profile.column_mappings.all()}

    # Pre-fetch transform rules and resolutions for efficiency
    transform_rules = list(profile.column_transform_rules.all())

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

        # Apply column transform rules
        for rule in transform_rules:
            idx = raw_headers.get(rule.source_column)
            if idx is None:
                continue
            raw_value = row[idx] if idx < len(row) else None
            if raw_value is None:
                continue
            raw_str = str(raw_value).strip()
            m = re.fullmatch(rule.pattern, raw_str)
            if m and rule.group_1_target and len(m.groups()) >= 1:
                row_dict[rule.group_1_target] = m.group(1)
            if m and rule.group_2_target and len(m.groups()) >= 2:
                row_dict[rule.group_2_target] = m.group(2)

        # Apply saved resolutions (rerere)
        source_id = row_dict.get("source_id", "")
        if source_id:
            for res in profile.source_resolutions.filter(source_id=str(source_id)):
                row_dict.update(res.resolved_fields)

        rows.append(row_dict)

    return rows


# ---------------------------------------------------------------------------
# Device-type slug resolution
# ---------------------------------------------------------------------------


def _resolve_device_type_slugs(make: str, model: str, profile: ImportProfile) -> tuple[str, str, bool]:
    """Return (manufacturer_slug, device_type_slug, is_explicit_mapping).

    Check DeviceTypeMapping first; fall back to auto-slugify.
    Both make and model are expected to be whitespace-normalized.
    """
    import re as _re

    def _normalize(s: str) -> str:
        r"""Normalize whitespace and decode JS-style \uXXXX escape sequences."""
        s = _re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), s)
        return " ".join(s.split())

    # Direct lookup (fast path — matches normalized stored records)
    mapping = profile.device_type_mappings.filter(source_make=make, source_model=model).first()
    # Fallback: stored records may have un-normalized whitespace or JS unicode escapes
    if not mapping:
        for m in profile.device_type_mappings.filter(source_make__iexact=make):
            if _normalize(m.source_model) == model:
                mapping = m
                break
    if mapping:
        return mapping.netbox_manufacturer_slug, mapping.netbox_device_type_slug, True

    # Check manufacturer-only mapping (maps source make to existing mfg slug)
    mfg_mapping = profile.manufacturer_mappings.filter(source_make=make).first()
    if not mfg_mapping:
        for m in profile.manufacturer_mappings.all():
            if _normalize(m.source_make) == make:
                mfg_mapping = m
                break
    manufacturer_slug = mfg_mapping.netbox_manufacturer_slug if mfg_mapping else slugify(make)[:50]
    device_type_slug = slugify(f"{make}-{model}")[:50]
    return manufacturer_slug, device_type_slug, False


# ---------------------------------------------------------------------------
# Main import runner
# ---------------------------------------------------------------------------


def run_import(rows: list[dict], profile: ImportProfile, context: dict, dry_run: bool = True) -> ImportResult:
    """Run (or preview) the import.

    context keys: site, location (optional), tenant (optional)
    dry_run=True  → no DB writes, returns what *would* happen
    dry_run=False → writes to DB
    """
    # Lazy imports to avoid circular imports at module load time
    from dcim.models import (
        Device,
        DeviceRole,
        DeviceType,
        Manufacturer,
        Rack,
    )
    from dcim.choices import DeviceFaceChoices, DeviceAirflowChoices

    site = context["site"]
    location = context.get("location")
    tenant = context.get("tenant")

    # Build class→role lookup from profile
    class_role_map: dict[str, object] = {crm.source_class: crm for crm in profile.class_role_mappings.all()}

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
        if crm and (crm.creates_rack or crm.ignore):
            continue  # racks and ignored classes don't need device type

        make = " ".join(str(row.get("make", "Unknown")).split()) or "Unknown"
        model = " ".join(str(row.get("model", "Unknown")).split()) or "Unknown"
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
                    result.rows.append(
                        RowResult(
                            row_number=row["_row_number"],
                            source_id=str(row.get("source_id", "")),
                            name=make,
                            action="create",
                            object_type="manufacturer",
                            detail=f"Would create manufacturer '{make}' (slug: {mfg_slug})",
                            extra_data={"source_make": make, "mfg_slug": mfg_slug},
                        )
                    )

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
                        result.rows.append(
                            RowResult(
                                row_number=row["_row_number"],
                                source_id=str(row.get("source_id", "")),
                                name=f"{make} / {model}",
                                action="create",
                                object_type="device_type",
                                detail=f"Would create device type '{model}' under '{make}'",
                                extra_data={
                                    "source_make": make,
                                    "source_model": model,
                                    "mfg_slug": mfg_slug,
                                    "dt_slug": dt_slug,
                                    "u_height": u_height,
                                },
                            )
                        )
                    else:
                        result.rows.append(
                            RowResult(
                                row_number=row["_row_number"],
                                source_id=str(row.get("source_id", "")),
                                name=f"{make} / {model}",
                                action="error",
                                object_type="device_type",
                                detail=f"Device type not found: {make} / {model} — add a mapping or enable 'Create missing device types'",
                                extra_data={
                                    "source_make": make,
                                    "source_model": model,
                                    "mfg_slug": mfg_slug,
                                    "dt_slug": dt_slug,
                                    "u_height": u_height,
                                },
                            )
                        )

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
            result.rows.append(
                RowResult(
                    row_number=row["_row_number"],
                    source_id=source_id,
                    name="",
                    action="error",
                    object_type="rack",
                    detail="Missing rack name",
                )
            )
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
            result.rows.append(
                RowResult(
                    row_number=row["_row_number"],
                    source_id=source_id,
                    name=rack_name,
                    action=action,
                    object_type="rack",
                    detail=detail,
                )
            )
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
                    result.rows.append(
                        RowResult(
                            row_number=row["_row_number"],
                            source_id=source_id,
                            name=rack_name,
                            action="update",
                            object_type="rack",
                            detail=f"Updated rack '{rack_name}'",
                            netbox_url=rack.get_absolute_url(),
                        )
                    )
                else:
                    rack_map[rack_name] = rack
                    result.rows.append(
                        RowResult(
                            row_number=row["_row_number"],
                            source_id=source_id,
                            name=rack_name,
                            action="skip",
                            object_type="rack",
                            detail=f"Rack '{rack_name}' already exists (update_existing=False)",
                        )
                    )
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
                result.rows.append(
                    RowResult(
                        row_number=row["_row_number"],
                        source_id=source_id,
                        name=rack_name,
                        action="create",
                        object_type="rack",
                        detail=f"Created rack '{rack_name}' ({u_height}U)",
                        netbox_url=rack.get_absolute_url(),
                    )
                )

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
        make = " ".join(str(row.get("make", "Unknown")).split()) or "Unknown"
        model = " ".join(str(row.get("model", "Unknown")).split()) or "Unknown"
        serial = str(row.get("serial", "")).strip()
        asset_tag_raw = str(row.get("asset_tag", "")).strip() or None
        asset_tag = asset_tag_raw[:50] if asset_tag_raw else None

        u_position_raw = row.get("u_position")
        try:
            position = int(float(u_position_raw))
            if position < 1:
                result.rows.append(
                    RowResult(
                        row_number=row["_row_number"],
                        source_id=source_id,
                        name=device_name,
                        action="skip",
                        object_type="device",
                        detail=f"Skipped: position {position} < 1 (under-rack/blanking panel)",
                        rack_name=rack_name,
                    )
                )
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
            result.rows.append(
                RowResult(
                    row_number=row["_row_number"],
                    source_id=source_id,
                    name="",
                    action="error",
                    object_type="device",
                    detail="Missing device name",
                )
            )
            continue

        # Check per-device ignore list
        from .models import IgnoredDevice

        if IgnoredDevice.objects.filter(profile=profile, source_id=source_id).exists():
            result.rows.append(
                RowResult(
                    row_number=row["_row_number"],
                    source_id=source_id,
                    name=device_name,
                    action="ignore",
                    object_type="device",
                    detail="Ignored device",
                    rack_name=rack_name,
                )
            )
            continue

        if not crm:
            result.rows.append(
                RowResult(
                    row_number=row["_row_number"],
                    source_id=source_id,
                    name=device_name,
                    action="error",
                    object_type="device",
                    detail=f"No class→role mapping for class '{device_class}'",
                    extra_data={
                        "source_class": device_class,
                        "profile_id": profile.pk,
                        "source_make": make,
                        "source_model": model,
                        "asset_tag": asset_tag or "",
                    },
                )
            )
            continue

        # Check class-level ignore
        if crm.ignore:
            result.rows.append(
                RowResult(
                    row_number=row["_row_number"],
                    source_id=source_id,
                    name=device_name,
                    action="ignore",
                    object_type="device",
                    detail=f"Ignored: class '{device_class}'",
                    rack_name=rack_name,
                )
            )
            continue

        mfg_slug, dt_slug, _ = _resolve_device_type_slugs(make, model, profile)

        if dry_run:
            # Check if device type exists
            dt_exists = DeviceType.objects.filter(manufacturer__slug=mfg_slug, slug=dt_slug).exists()
            if not dt_exists and not profile.create_missing_device_types:
                result.rows.append(
                    RowResult(
                        row_number=row["_row_number"],
                        source_id=source_id,
                        name=device_name,
                        action="error",
                        object_type="device",
                        detail=f"Device type not found: {make} / {model} (slug: {mfg_slug}/{dt_slug})",
                    )
                )
                continue

            rack_label = rack_name if rack_name in rack_map else f"{rack_name} (not found)"
            try:
                Device.objects.get(site=site, name=device_name)
                action = "update" if profile.update_existing else "skip"
                detail = f"Device '{device_name}' already exists"
            except Device.DoesNotExist:
                # 1. Check manual source-ID link
                existing_match = profile.device_matches.filter(source_id=source_id).first()
                matched_device = None
                match_method = None
                if existing_match:
                    try:
                        matched_device = Device.objects.get(pk=existing_match.netbox_device_id)
                        match_method = "source ID link"
                    except Device.DoesNotExist:
                        pass  # stale — will fall through to serial/asset_tag/create

                # 2. Try serial match
                if matched_device is None and serial:
                    try:
                        matched_device = Device.objects.get(serial=serial)
                        match_method = "serial"
                    except (Device.DoesNotExist, Device.MultipleObjectsReturned):
                        pass

                # 3. Try asset_tag match
                if matched_device is None and asset_tag:
                    try:
                        matched_device = Device.objects.get(asset_tag=asset_tag)
                        match_method = "asset tag"
                    except (Device.DoesNotExist, Device.MultipleObjectsReturned):
                        pass

                if matched_device is not None:
                    action = "update"
                    detail = f"Matched to existing device '{matched_device.name}' (by {match_method})"
                else:
                    action = "create"
                    detail = f"Would create device '{device_name}' in {rack_label} U{position}"

            result.rows.append(
                RowResult(
                    row_number=row["_row_number"],
                    source_id=source_id,
                    name=device_name,
                    action=action,
                    object_type="device",
                    detail=detail,
                    rack_name=rack_name,
                    extra_data={"source_make": make, "source_model": model, "asset_tag": asset_tag or ""},
                )
            )
        else:
            # Resolve device type
            try:
                device_type = DeviceType.objects.get(manufacturer__slug=mfg_slug, slug=dt_slug)
            except DeviceType.DoesNotExist:
                result.rows.append(
                    RowResult(
                        row_number=row["_row_number"],
                        source_id=source_id,
                        name=device_name,
                        action="error",
                        object_type="device",
                        detail=f"Device type not found: {mfg_slug}/{dt_slug}",
                    )
                )
                continue

            # Resolve role
            try:
                device_role = DeviceRole.objects.get(slug=crm.role_slug)
            except DeviceRole.DoesNotExist:
                result.rows.append(
                    RowResult(
                        row_number=row["_row_number"],
                        source_id=source_id,
                        name=device_name,
                        action="error",
                        object_type="device",
                        detail=f"Device role not found: {crm.role_slug}",
                    )
                )
                continue

            rack = rack_map.get(rack_name) if rack_name else None

            # Try to find an existing device by name, then serial, then asset_tag
            device = None
            match_method = "name"
            try:
                device = Device.objects.get(site=site, name=device_name)
            except Device.DoesNotExist:
                if serial:
                    device = Device.objects.filter(serial=serial).first()
                    if device:
                        match_method = "serial"
                if device is None and asset_tag:
                    device = Device.objects.filter(asset_tag=asset_tag).first()
                    if device:
                        match_method = "asset tag"

            if device is not None:
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
                    result.rows.append(
                        RowResult(
                            row_number=row["_row_number"],
                            source_id=source_id,
                            name=device_name,
                            action="update",
                            object_type="device",
                            detail=f"Updated device '{device.name}' (matched by {match_method})",
                            netbox_url=device.get_absolute_url(),
                            rack_name=rack_name,
                            extra_data={"source_make": make, "source_model": model, "asset_tag": asset_tag or ""},
                        )
                    )
                else:
                    result.rows.append(
                        RowResult(
                            row_number=row["_row_number"],
                            source_id=source_id,
                            name=device_name,
                            action="skip",
                            object_type="device",
                            detail=f"Device '{device.name}' already exists (update_existing=False)",
                            rack_name=rack_name,
                            extra_data={"source_make": make, "source_model": model, "asset_tag": asset_tag or ""},
                        )
                    )
            else:
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
                result.rows.append(
                    RowResult(
                        row_number=row["_row_number"],
                        source_id=source_id,
                        name=device_name,
                        action="create",
                        object_type="device",
                        detail=f"Created device '{device_name}' in {rack_name} U{position}",
                        netbox_url=device.get_absolute_url(),
                        rack_name=rack_name,
                        extra_data={"source_make": make, "source_model": model, "asset_tag": asset_tag or ""},
                    )
                )

    result._recompute_counts()
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _store_source_id(obj, profile: ImportProfile, source_id: str):
    """Store source ID in the configured custom field and in the plugin's JSON metadata field."""
    changed = False

    # Per-profile custom field (e.g. cans_id → plain string)
    if profile.custom_field_name and source_id:
        try:
            obj.custom_field_data[profile.custom_field_name] = source_id
            changed = True
        except (AttributeError, KeyError):
            pass

    # Plugin-managed JSON field: data_import_source
    try:
        obj.custom_field_data["data_import_source"] = {
            "source_id": source_id or "",
            "profile_id": profile.pk,
            "profile_name": profile.name,
        }
        changed = True
    except (AttributeError, KeyError):
        pass

    if changed:
        try:
            obj.save(update_fields=["custom_field_data"])
        except Exception:
            pass
