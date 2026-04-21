# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Import engine: parse Excel files and run (or preview) imports into NetBox.

Public API
----------
parse_file(file_obj, profile)  ->  list[dict]
run_import(rows, profile, context, dry_run=True)  ->  ImportResult
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Literal
from io import BytesIO

from django.utils.text import slugify
import openpyxl

from .models import ImportProfile

logger = logging.getLogger(__name__)


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


@dataclass
class ImportContext:
    """Shared execution context passed through all import pipeline stages.

    Holds profile/site/tenant/flags so internal helpers do not need
    to accept those as separate positional arguments.  ``rack_map`` is
    populated by pass 2 and consumed by pass 3.
    """

    profile: ImportProfile
    site: object
    location: object | None
    tenant: object | None
    dry_run: bool
    result: ImportResult
    rack_map: dict = field(default_factory=dict)
    pending_device_roles: set = field(default_factory=set)
    user: object | None = None


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _build_header_index_map(ws) -> dict[str, int]:
    """Build a header-name → column-index map from the first worksheet row.

    First occurrence wins when duplicate headers exist.
    """
    raw_headers: dict[str, int] = {}
    for idx, cell in enumerate(ws[1]):
        if cell.value is not None:
            header = str(cell.value).strip()
            if header not in raw_headers:
                raw_headers[header] = idx
    return raw_headers


def _apply_transform_rules(row_dict: dict, raw_row, raw_headers: dict, transform_rules) -> None:
    """Apply column transform rules in-place to *row_dict*."""
    for rule in transform_rules:
        idx = raw_headers.get(rule.source_column)
        if idx is None:
            continue
        raw_value = raw_row[idx] if idx < len(raw_row) else None
        if raw_value is None:
            continue
        raw_str = str(raw_value).strip()
        try:
            m = re.fullmatch(rule.pattern, raw_str)
        except re.error as exc:
            raise ParseError(
                f"Invalid regex pattern '{rule.pattern}' in transform rule for column "
                f"'{rule.source_column}' (value: {raw_str!r}): {exc}"
            ) from exc
        if m and rule.group_1_target and len(m.groups()) >= 1:
            row_dict[rule.group_1_target] = m.group(1)
        if m and rule.group_2_target and len(m.groups()) >= 2:
            row_dict[rule.group_2_target] = m.group(2)


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
    raw_headers = _build_header_index_map(ws)

    # Build source_column→target_field map from profile
    col_map: dict[str, str] = {cm.source_column: cm.target_field for cm in profile.column_mappings.all()}

    # Pre-fetch transform rules for efficiency
    transform_rules = list(profile.column_transform_rules.all())

    # Pre-fetch all saved resolutions for this profile (avoids N+1 queries)
    resolutions_by_source_id: dict[str, list] = {}
    for res in profile.source_resolutions.all():
        resolutions_by_source_id.setdefault(str(res.source_id), []).append(res)

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

        _apply_transform_rules(row_dict, row, raw_headers, transform_rules)

        # Apply saved resolutions (rerere)
        source_id = row_dict.get("source_id", "")
        if source_id:
            for res in resolutions_by_source_id.get(str(source_id), []):
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

    def _normalize(s: str) -> str:
        r"""Normalize whitespace and decode JS-style \uXXXX escape sequences."""
        s = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), s)
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
# Main import runner — pass helpers
# ---------------------------------------------------------------------------

# Value-translation maps (shared across passes)
_STATUS_MAP = {
    "live": "active",
    "production": "active",
    "planned": "planned",
    "staged": "staged",
    "failed": "failed",
    "offline": "offline",
    "decommissioning": "decommissioning",
}


def _get_translation_maps():
    """Return (SIDE_MAP, AIRFLOW_MAP, STATUS_MAP) with lazy-imported choice values."""
    from dcim.choices import DeviceAirflowChoices, DeviceFaceChoices

    side = {
        "front": DeviceFaceChoices.FACE_FRONT,
        "back": DeviceFaceChoices.FACE_REAR,
        "rear": DeviceFaceChoices.FACE_REAR,
    }
    airflow = {
        "front to back": DeviceAirflowChoices.AIRFLOW_FRONT_TO_REAR,
        "back to front": DeviceAirflowChoices.AIRFLOW_REAR_TO_FRONT,
        "passive": DeviceAirflowChoices.AIRFLOW_PASSIVE,
    }
    return side, airflow, _STATUS_MAP


def _perm_denied_row(perm: str, row: dict, name: str, object_type: str) -> RowResult:
    """Return a permission-denied RowResult for a write operation the user may not perform."""
    return RowResult(
        row_number=row["_row_number"],
        source_id=str(row.get("source_id", "")),
        name=name,
        action="error",
        object_type=object_type,
        detail=f"Permission denied: {perm}",
    )


def _ensure_manufacturer(mfg_slug, make, seen_manufacturers, ctx, row, Manufacturer):
    """Create (or preview-create) a manufacturer if not yet seen."""
    if mfg_slug in seen_manufacturers:
        return
    seen_manufacturers.add(mfg_slug)
    if not ctx.dry_run:
        if ctx.profile.create_missing_device_types:
            if not Manufacturer.objects.filter(slug=mfg_slug).exists():
                if ctx.user is not None and not ctx.user.has_perm("dcim.add_manufacturer"):
                    ctx.result.rows.append(_perm_denied_row("dcim.add_manufacturer", row, make, "manufacturer"))
                    return
            Manufacturer.objects.get_or_create(slug=mfg_slug, defaults={"name": make})
    elif not Manufacturer.objects.filter(slug=mfg_slug).exists() and ctx.profile.create_missing_device_types:
        if ctx.user is not None and not ctx.user.has_perm("dcim.add_manufacturer"):
            ctx.result.rows.append(_perm_denied_row("dcim.add_manufacturer", row, make, "manufacturer"))
        else:
            ctx.result.rows.append(
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


def _ensure_device_type(
    mfg_slug, dt_slug, make, model, u_height, seen_device_types, ctx, row, Manufacturer, DeviceType
):
    """Create (or preview-create) a device type if not yet seen."""
    dt_key = (mfg_slug, dt_slug)
    if dt_key in seen_device_types:
        return
    seen_device_types.add(dt_key)
    if not ctx.dry_run:
        if ctx.profile.create_missing_device_types:
            if not DeviceType.objects.filter(manufacturer__slug=mfg_slug, slug=dt_slug).exists():
                if not Manufacturer.objects.filter(slug=mfg_slug).exists():
                    if ctx.user is not None and not ctx.user.has_perm("dcim.add_manufacturer"):
                        ctx.result.rows.append(
                            _perm_denied_row("dcim.add_manufacturer", row, f"{make} / {model}", "device_type")
                        )
                        return
                if ctx.user is not None and not ctx.user.has_perm("dcim.add_devicetype"):
                    ctx.result.rows.append(
                        _perm_denied_row("dcim.add_devicetype", row, f"{make} / {model}", "device_type")
                    )
                    return
            mfg, _ = Manufacturer.objects.get_or_create(slug=mfg_slug, defaults={"name": make})
            DeviceType.objects.get_or_create(
                manufacturer=mfg, slug=dt_slug, defaults={"model": model, "u_height": u_height}
            )
        return
    exists = DeviceType.objects.filter(manufacturer__slug=mfg_slug, slug=dt_slug).exists()
    if exists:
        return
    if ctx.profile.create_missing_device_types:
        if ctx.user is not None and not ctx.user.has_perm("dcim.add_devicetype"):
            ctx.result.rows.append(_perm_denied_row("dcim.add_devicetype", row, f"{make} / {model}", "device_type"))
        else:
            ctx.result.rows.append(
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
        ctx.result.rows.append(
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


def _ensure_device_role(crm, seen_roles, ctx, DeviceRole):
    """Create a device role if not yet seen, respecting user permissions."""
    if not (crm and crm.role_slug and crm.role_slug not in seen_roles):
        return
    seen_roles.add(crm.role_slug)
    if ctx.dry_run:
        if ctx.user is None or ctx.user.has_perm("dcim.add_devicerole"):
            ctx.pending_device_roles.add(crm.role_slug)
    else:
        if ctx.user is not None and not ctx.user.has_perm("dcim.add_devicerole"):
            return
        DeviceRole.objects.get_or_create(
            slug=crm.role_slug,
            defaults={"name": crm.role_slug.replace("-", " ").title(), "color": "9e9e9e"},
        )


def _pass1_ensure_types(rows, ctx, class_role_map):
    """Pass 1: ensure Manufacturer, DeviceType, and DeviceRole objects exist."""
    from dcim.models import DeviceRole, DeviceType, Manufacturer

    seen_manufacturers: set[str] = set()
    seen_device_types: set[tuple] = set()
    seen_roles: set[str] = set()

    for row in rows:
        device_class = str(row.get("device_class", "")).strip()
        crm = class_role_map.get(device_class)
        if crm is None or crm.creates_rack or crm.ignore:
            continue

        make = " ".join(str(row.get("make", "Unknown")).split()) or "Unknown"
        model = " ".join(str(row.get("model", "Unknown")).split()) or "Unknown"
        u_height_raw = row.get("u_height", 1)
        try:
            u_height = max(1, int(float(u_height_raw)))
        except (TypeError, ValueError):
            u_height = 1

        mfg_slug, dt_slug, _ = _resolve_device_type_slugs(make, model, ctx.profile)
        _ensure_manufacturer(mfg_slug, make, seen_manufacturers, ctx, row, Manufacturer)
        _ensure_device_type(
            mfg_slug,
            dt_slug,
            make,
            model,
            u_height,
            seen_device_types,
            ctx,
            row,
            Manufacturer,
            DeviceType,
        )
        _ensure_device_role(crm, seen_roles, ctx, DeviceRole)


def _write_rack_to_db(rack_name, u_height, serial, source_id, row, ctx, Rack, rack_type=None):
    """Write or update a rack in the database and record the result."""
    try:
        rack = Rack.objects.get(site=ctx.site, name=rack_name)
        if ctx.profile.update_existing:
            if ctx.user is not None and not ctx.user.has_perm("dcim.change_rack"):
                ctx.result.rows.append(_perm_denied_row("dcim.change_rack", row, rack_name, "rack"))
                ctx.rack_map[rack_name] = rack
                return
            rack.u_height = u_height
            rack.serial = serial or rack.serial
            if rack_type is not None:
                rack.rack_type = rack_type
            if ctx.location:
                rack.location = ctx.location
            if ctx.tenant:
                rack.tenant = ctx.tenant
            rack.save()
            ctx.rack_map[rack_name] = rack
            ctx.result.rows.append(
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
            ctx.rack_map[rack_name] = rack
            ctx.result.rows.append(
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
        if ctx.user is not None and not ctx.user.has_perm("dcim.add_rack"):
            ctx.result.rows.append(_perm_denied_row("dcim.add_rack", row, rack_name, "rack"))
            return
        rack = Rack.objects.create(
            site=ctx.site,
            location=ctx.location,
            name=rack_name,
            tenant=ctx.tenant,
            u_height=u_height,
            serial=serial,
            rack_type=rack_type,
        )
        _store_source_id(rack, ctx.profile, source_id)
        ctx.rack_map[rack_name] = rack
        ctx.result.rows.append(
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


def _pass2_process_racks(rows, ctx, class_role_map):
    """Pass 2: create or update Rack objects. Populates ctx.rack_map in place."""
    from dcim.models import Rack

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
            ctx.result.rows.append(
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

        if ctx.dry_run:
            rack_type_label = f", type={crm.rack_type}" if crm.rack_type_id else ""
            try:
                Rack.objects.get(site=ctx.site, name=rack_name)
                action = "update" if ctx.profile.update_existing else "skip"
                detail = f"Rack '{rack_name}' already exists"
            except Rack.DoesNotExist:
                action = "create"
                detail = f"Would create rack '{rack_name}' ({u_height}U{rack_type_label}) at site '{ctx.site}'"
            ctx.rack_map[rack_name] = rack_name
            ctx.result.rows.append(
                RowResult(
                    row_number=row["_row_number"],
                    source_id=source_id,
                    name=rack_name,
                    action=action,
                    object_type="rack",
                    detail=detail,
                    extra_data={"source_class": device_class, "rack_type_set": bool(crm.rack_type_id)},
                )
            )
        else:
            _write_rack_to_db(rack_name, u_height, serial, source_id, row, ctx, Rack, rack_type=crm.rack_type)


def _find_existing_device(profile, source_id, site, device_name, serial, asset_tag, Device):
    """Look up a pre-existing NetBox device by source-ID link, serial, asset_tag, or name.

    Returns (device, match_method) or (None, None).
    Matching priority: source-ID link → serial → asset_tag → name.
    When *site* is provided the name lookup is scoped to that site; without a
    site the name is matched globally (any site).
    """
    existing_match = profile.device_matches.filter(source_id=source_id).first() if source_id else None
    matched_device = None
    match_method = None
    if existing_match:
        try:
            matched_device = Device.objects.get(pk=existing_match.netbox_device_id)
            match_method = "source ID link"
        except Device.DoesNotExist:
            pass

    if matched_device is None and serial:
        try:
            matched_device = Device.objects.get(serial=serial)
            match_method = "serial"
        except Device.DoesNotExist:
            pass
        except Device.MultipleObjectsReturned:
            logger.warning("Ambiguous serial match for serial=%r; skipping auto-match", serial)

    if matched_device is None and asset_tag:
        try:
            matched_device = Device.objects.get(asset_tag=asset_tag)
            match_method = "asset tag"
        except Device.DoesNotExist:
            pass
        except Device.MultipleObjectsReturned:
            logger.warning("Ambiguous asset_tag match for asset_tag=%r; skipping auto-match", asset_tag)

    if matched_device is None and device_name:
        name_filter = {"name": device_name}
        if site is not None:
            name_filter["site"] = site
        try:
            matched_device = Device.objects.get(**name_filter)
            match_method = "name"
        except Device.DoesNotExist:
            pass
        except Device.MultipleObjectsReturned:
            logger.warning("Ambiguous name match for device_name=%r; skipping auto-match", device_name)

    return matched_device, match_method


def _preview_device_row(
    row,
    ctx,
    make,
    model,
    mfg_slug,
    dt_slug,
    source_id,
    device_name,
    serial,
    asset_tag,
    DeviceType,
    Device,
    Rack,
):
    """Return a RowResult for *dry_run* mode (no DB writes)."""
    dt_exists = DeviceType.objects.filter(manufacturer__slug=mfg_slug, slug=dt_slug).exists()
    if not dt_exists and not ctx.profile.create_missing_device_types:
        return RowResult(
            row_number=row["_row_number"],
            source_id=source_id,
            name=device_name,
            action="error",
            object_type="device",
            detail=f"Device type not found: {make} / {model} (slug: {mfg_slug}/{dt_slug})",
        )

    rack_name = str(row.get("rack_name", "")).strip()
    if rack_name:
        if rack_name in ctx.rack_map:
            rack_label = rack_name
        elif Rack.objects.filter(site=ctx.site, name=rack_name).exists():
            ctx.rack_map[rack_name] = rack_name  # cache to avoid repeated queries
            rack_label = rack_name
        else:
            rack_label = f"{rack_name} (not found)"
    else:
        rack_label = "(no rack)"
    raw_position = row.get("u_position")
    try:
        position = int(float(raw_position)) if raw_position is not None and str(raw_position).strip() != "" else None
    except (TypeError, ValueError):
        position = None

    # _find_existing_device checks DeviceExistingMatch → serial → asset_tag → name in that order,
    # ensuring explicit operator mappings always take precedence over coincidental name matches.
    matched_device, match_method = _find_existing_device(
        ctx.profile, source_id, ctx.site, device_name, serial, asset_tag, Device
    )
    if matched_device is not None:
        action = "update" if ctx.profile.update_existing else "skip"
        if match_method == "name":
            detail = f"Device '{device_name}' already exists"
        else:
            detail = f"Matched to existing device '{matched_device.name}' (by {match_method})"
    else:
        action = "create"
        _pos_label = f" U{position}" if position is not None else ""
        detail = f"Would create device '{device_name}' in {rack_label}{_pos_label}"

    return RowResult(
        row_number=row["_row_number"],
        source_id=source_id,
        name=device_name,
        action=action,
        object_type="device",
        detail=detail,
        rack_name=rack_name,
        extra_data={"source_make": make, "source_model": model, "asset_tag": asset_tag or ""},
    )


def _write_device_row(
    row,
    ctx,
    make,
    model,
    crm,
    mfg_slug,
    dt_slug,
    source_id,
    device_name,
    serial,
    asset_tag,
    position,
    face,
    airflow,
    status,
    DeviceType,
    DeviceRole,
    Rack,
    Device,
):
    """Write or update a device in the DB and return a RowResult."""
    rack_name = str(row.get("rack_name", "")).strip()
    try:
        device_type = DeviceType.objects.get(manufacturer__slug=mfg_slug, slug=dt_slug)
    except DeviceType.DoesNotExist:
        return RowResult(
            row_number=row["_row_number"],
            source_id=source_id,
            name=device_name,
            action="error",
            object_type="device",
            detail=f"Device type not found: {mfg_slug}/{dt_slug}",
        )

    try:
        device_role = DeviceRole.objects.get(slug=crm.role_slug)
    except DeviceRole.DoesNotExist:
        return RowResult(
            row_number=row["_row_number"],
            source_id=source_id,
            name=device_name,
            action="error",
            object_type="device",
            detail=f"Device role not found: {crm.role_slug}",
        )

    rack = ctx.rack_map.get(rack_name) if rack_name else None
    if rack_name and rack is None:
        rack = Rack.objects.filter(site=ctx.site, name=rack_name).first()

    # _find_existing_device checks DeviceExistingMatch → serial → asset_tag → name in that order,
    # ensuring explicit operator mappings always take precedence over coincidental name matches.
    device, match_method = _find_existing_device(
        ctx.profile, source_id, ctx.site, device_name, serial, asset_tag, Device
    )

    if device is not None:
        if ctx.profile.update_existing:
            if ctx.user is not None and not ctx.user.has_perm("dcim.change_device"):
                return _perm_denied_row("dcim.change_device", row, device_name, "device")
            device.device_type = device_type
            device.role = device_role
            device.rack = rack if isinstance(rack, Rack) else None
            device.position = position
            device.face = face
            device.airflow = airflow
            device.status = status
            device.serial = serial or device.serial
            if asset_tag:
                device.asset_tag = asset_tag
            if ctx.tenant:
                device.tenant = ctx.tenant
            device.save()
            _store_source_id(device, ctx.profile, source_id)
            return RowResult(
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
        return RowResult(
            row_number=row["_row_number"],
            source_id=source_id,
            name=device_name,
            action="skip",
            object_type="device",
            detail=f"Device '{device.name}' already exists (update_existing=False)",
            rack_name=rack_name,
            extra_data={"source_make": make, "source_model": model, "asset_tag": asset_tag or ""},
        )

    if ctx.user is not None and not ctx.user.has_perm("dcim.add_device"):
        return _perm_denied_row("dcim.add_device", row, device_name, "device")
    device = Device.objects.create(
        site=ctx.site,
        location=ctx.location,
        name=device_name,
        device_type=device_type,
        role=device_role,
        rack=rack if isinstance(rack, Rack) else None,
        position=position,
        face=face,
        airflow=airflow,
        status=status,
        serial=serial,
        asset_tag=asset_tag,
        tenant=ctx.tenant,
    )
    _store_source_id(device, ctx.profile, source_id)
    _rack_label = rack_name if rack_name else "(no rack)"
    _pos_label = f" U{position}" if position is not None else ""
    return RowResult(
        row_number=row["_row_number"],
        source_id=source_id,
        name=device_name,
        action="create",
        object_type="device",
        detail=f"Created device '{device_name}' in {_rack_label}{_pos_label}",
        netbox_url=device.get_absolute_url(),
        rack_name=rack_name,
        extra_data={"source_make": make, "source_model": model, "asset_tag": asset_tag or ""},
    )


def _pass3_process_devices(rows, ctx, class_role_map):
    """Pass 3: create or update Device objects."""
    from dcim.models import Device, DeviceRole, DeviceType, Rack
    from .models import IgnoredDevice

    side_map, airflow_map, status_map = _get_translation_maps()

    # Pre-fetch ignored source IDs to avoid per-row queries
    ignored_source_ids = set(IgnoredDevice.objects.filter(profile=ctx.profile).values_list("source_id", flat=True))

    for row in rows:
        device_class = str(row.get("device_class", "")).strip()
        crm = class_role_map.get(device_class)
        if crm and crm.creates_rack:
            continue

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
                ctx.result.rows.append(
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

        if not device_name:
            ctx.result.rows.append(
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

        if source_id in ignored_source_ids:
            ctx.result.rows.append(
                RowResult(
                    row_number=row["_row_number"],
                    source_id=source_id,
                    name=device_name,
                    action="ignore",
                    object_type="device",
                    detail="Ignored device",
                    rack_name=rack_name,
                    extra_data={"ignore_kind": "individual"},
                )
            )
            continue

        if not crm:
            ctx.result.rows.append(
                RowResult(
                    row_number=row["_row_number"],
                    source_id=source_id,
                    name=device_name,
                    action="error",
                    object_type="device",
                    detail=f"No class→role mapping for class '{device_class}'",
                    extra_data={
                        "source_class": device_class,
                        "profile_id": ctx.profile.pk,
                        "source_make": make,
                        "source_model": model,
                        "asset_tag": asset_tag or "",
                    },
                )
            )
            continue

        if crm.ignore:
            ctx.result.rows.append(
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

        mfg_slug, dt_slug, _ = _resolve_device_type_slugs(make, model, ctx.profile)
        device_status = status_map.get(str(row.get("status", "")).strip().lower(), "active")
        device_face = side_map.get(str(row.get("face", "")).strip().lower())
        device_airflow = airflow_map.get(str(row.get("airflow", "")).strip().lower())

        if ctx.dry_run:
            row_result = _preview_device_row(
                row,
                ctx,
                make,
                model,
                mfg_slug,
                dt_slug,
                source_id,
                device_name,
                serial,
                asset_tag,
                DeviceType,
                Device,
                Rack,
            )
        else:
            row_result = _write_device_row(
                row,
                ctx,
                make,
                model,
                crm,
                mfg_slug,
                dt_slug,
                source_id,
                device_name,
                serial,
                asset_tag,
                position,
                device_face,
                device_airflow,
                device_status,
                DeviceType,
                DeviceRole,
                Rack,
                Device,
            )
        ctx.result.rows.append(row_result)


# ---------------------------------------------------------------------------
# Main import runner
# ---------------------------------------------------------------------------


def run_import(
    rows: list[dict], profile: ImportProfile, context: dict, dry_run: bool = True, user: object | None = None
) -> ImportResult:
    """Run (or preview) the import.

    context keys: site, location (optional), tenant (optional)
    dry_run=True  → no DB writes, returns what *would* happen
    dry_run=False → writes to DB; pass user to enforce DCIM object permissions
    """
    ctx = ImportContext(
        profile=profile,
        site=context["site"],
        location=context.get("location"),
        tenant=context.get("tenant"),
        dry_run=dry_run,
        result=ImportResult(),
        user=user,
    )

    class_role_map: dict[str, object] = {crm.source_class: crm for crm in profile.class_role_mappings.all()}

    _pass1_ensure_types(rows, ctx, class_role_map)
    _pass2_process_racks(rows, ctx, class_role_map)
    _pass3_process_devices(rows, ctx, class_role_map)

    ctx.result._recompute_counts()
    if ctx.pending_device_roles:
        ctx.result.counts["device_roles_pending"] = len(ctx.pending_device_roles)
    return ctx.result


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
        except (AttributeError, KeyError):  # pragma: no cover
            logger.warning("Failed to set custom field '%s' on %s", profile.custom_field_name, obj)

    # Plugin-managed JSON field: data_import_source
    try:
        obj.custom_field_data["data_import_source"] = {
            "source_id": source_id or "",
            "profile_id": profile.pk,
            "profile_name": profile.name,
        }
        changed = True
    except (AttributeError, KeyError):  # pragma: no cover
        logger.warning("Failed to set data_import_source on %s", obj)

    if changed:
        try:
            obj.save(update_fields=["custom_field_data"])
        except Exception:  # pragma: no cover
            logger.exception("Failed to save custom_field_data on %s (source_id=%s)", obj, source_id)
