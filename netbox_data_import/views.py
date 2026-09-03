# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
import difflib
import logging
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import NamedTuple
from urllib.parse import parse_qs, urlsplit

from django.contrib import messages
from django.contrib.auth.mixins import PermissionRequiredMixin
from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError, transaction
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from netbox.views import generic
from utilities.permissions import get_permission_for_model
from utilities.views import ConditionalLoginRequiredMixin

from .filters import ImportProfileFilterSet
from .forms import (
    ClassRoleMappingForm,
    ColumnMappingForm,
    ColumnTransformRuleForm,
    DeviceTypeMappingForm,
    ImportProfileBulkEditForm,
    ImportProfileForm,
    ImportProfileImportForm,
    ImportSetupForm,
)
from .catalog import CANDIDATE_TARGET_PREFIX, CATALOG
from .values import (
    effective_device_name,
    identity_text,
    normalize_for_compare,
    source_position,
    source_text,
    status_map,
    translation_maps,
)
from . import __version__ as _plugin_version
from .models import (
    locked_profile_policy,
    locked_resolution_policy,
    ClassRoleMapping,
    ColumnMapping,
    ColumnTransformRule,
    DeviceExistingMatch,
    DeviceTypeMapping,
    IgnoredFieldDifference,
    ImportExecution,
    ImportProfile,
    ManufacturerMapping,
    SourceDocument,
    SourceResolution,
    stored_import_source,
    validate_contact_candidate_resolution,
    validate_registered_adapter,
    validate_source_resolution_fields,
)
from .tables import (
    ClassRoleMappingTable,
    ColumnMappingTable,
    ColumnTransformRuleTable,
    DeviceTypeMappingTable,
    ImportExecutionTable,
    ImportProfileTable,
)
from . import adapters, ip_assignment
from .contact_resolution import PrimaryContactResolver, contact_identity, suggest_contact_roles
from .device_field_review import DeviceFieldReviewer
from .object_permissions import (
    ObjectPermissionDenied,
    delete_permission_scoped_objects,
    save_or_refetch,
    save_permission_scoped_object,
)
from .preview_row_actions import (
    PREVIEW_DIRTY_SESSION_KEY,
    PREVIEW_PLAN_SESSION_KEY,
    PREVIEW_USE_MATERIALIZED_ONCE_SESSION_KEY,
    PreviewActionInvalid,
    current_preview_revision,
    load_cached_preview,
    mark_preview_dirty,
    pending_preview_payload,
    record_recalculated_preview,
    retire_preview_revision,
)
from .import_engine import (
    ImportEngine,
    PreconditionFailed,
    SelectionError,
    StalePlan,
    StaleSourceDocument,
    operator_failure_message,
)
from .netbox_reader import PlanningTargetUnavailable
from .plan import ImportPlan, PlanError
from .review_workspace import ReviewWorkspace


def _safe_next_url(request, fallback: str) -> str:
    """Return a validated same-host redirect URL from POST or the fallback view name."""
    url = request.POST.get("next", "")
    if url and url_has_allowed_host_and_scheme(
        url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return url
    return reverse(fallback)


def _navigation_response(request, url):
    """Send an HTMX caller through a real page load, or redirect a standard browser."""
    if request.headers.get("HX-Request") == "true":
        response = HttpResponse(status=204)
        response["HX-Redirect"] = url
        return response
    return redirect(url)


def _name_resolution_response(request, url):
    """Return an updated preview for HTMX or redirect a standard browser."""
    if request.headers.get("HX-Request") == "true":
        preview_path = reverse("plugins:netbox_data_import:import_preview")
        if urlsplit(url).path == preview_path:
            preview_response = ImportPreviewView().render_preview(request, url)
            if not 300 <= preview_response.status_code < 400:
                return preview_response
            url = preview_response.headers["Location"]
    return _navigation_response(request, url)


def _parse_posted_profile_id(request):
    """Return the posted integer profile ID, or None when it is invalid."""
    try:
        return int(request.POST.get("profile_id", ""))
    except (TypeError, ValueError):
        return None


def _candidate_values(extra_data):
    """Return candidate values after validating the serialized display shape."""
    candidate_values = extra_data.get("candidate_values", {})
    if not isinstance(candidate_values, Mapping) or any(
        not isinstance(candidates, Mapping) for candidates in candidate_values.values()
    ):
        raise ValidationError("The active Import Plan has invalid candidate values.")
    return candidate_values


def _contact_candidate_context(request, profile_id, source_id):
    """Return Contact candidates and row state for one active preview row."""
    plan_data = request.session.get(PREVIEW_PLAN_SESSION_KEY) or {}
    try:
        workspace = ReviewWorkspace.from_dict(plan_data)
    except PlanError as exc:
        raise ValidationError("The active Import Plan is no longer readable.") from exc
    result_rows = [
        row for row in workspace.units if str(row.source_id) == str(source_id) and row.object_type == "device"
    ]
    context = request.session.get("import_context") or {}
    source_rows = [
        row for row in (request.session.get("import_rows") or []) if str(row.get("source_id")) == str(source_id)
    ]
    if str(context.get("profile_id")) != str(profile_id) or len(source_rows) != 1 or len(result_rows) != 1:
        raise ValidationError("The candidate resolution does not identify one active preview row.")

    candidates = _candidate_values(result_rows[0].extra_data).get("contact", {})
    if not isinstance(candidates, Mapping) or not candidates:
        raise ValidationError("The active preview row has no Contact candidate values.")
    return (
        {str(source_column): str(value) for source_column, value in candidates.items()},
        source_rows[0],
        result_rows[0],
    )


def _store_contact_for_unmatched_row(profile, resolved_fields, candidates, user):
    """Store the Contact a row names while it still has no Device to assign it to.

    Returns the resolved fields to save, which name the stored Contact, the sentence the operator
    is told about it, and the stored Contact itself. A decision that names no Contact stores nothing.
    """
    selection = PrimaryContactResolver.selection_for_resolution(profile, resolved_fields, candidates)
    if selection is None:
        return resolved_fields, "", None
    contact, created = PrimaryContactResolver.create_contact(profile, selection, user)
    note = (
        f" Contact '{contact.name}' was created in NetBox."
        if created
        else f" Contact '{contact.name}' already existed in NetBox."
    )
    return {**resolved_fields, "contact_id": contact.pk}, note, contact_identity(contact)


def _planned_device_id(result_row):
    """Return the Device a row plans to write to, or None when the import refused the row.

    A refused row still carries the Device it matched, so the identifier alone does not mean the
    import accepted the match.
    """
    if result_row.action != "update":
        return None
    return result_row.extra_data.get("netbox_device_id")


@dataclass(frozen=True)
class _ContactWrite:
    """What one decided Contact changed on the Device a row already matched."""

    assignment_changed: bool
    contact_created: bool
    contact: dict


def _assign_contact_to_matched_device(profile, resolved_fields, source_row, device_id, user) -> _ContactWrite | None:
    """Apply the decided Contact to the Device this row already matched.

    Returns what the write changed, or None when the decision names no Contact. `apply` creates the
    Contact even when the assignment itself is unchanged, so the two are reported separately.
    """
    from dcim.models import Device

    device = Device.objects.restrict(user, "change").filter(pk=device_id).first()
    if device is None:
        raise ObjectPermissionDenied("dcim.change_device")
    resolved_row = dict(source_row)
    resolved_row.update(resolved_fields)
    review = PrimaryContactResolver.review(device, resolved_row, profile, user)
    plan = PrimaryContactResolver.apply(device, profile, review, user)
    if plan is None:
        return None
    return _ContactWrite(
        assignment_changed=plan["assignment_action"] != "unchanged",
        contact_created=plan["contact_id"] is None,
        contact=plan["saved_contact"],
    )


@dataclass(frozen=True)
class _ContactDecision:
    """One saved Contact decision: the fields to store, and what writing it changed."""

    resolved_fields: Mapping
    write: _ContactWrite | None = None
    note: str = ""
    contact: dict | None = None


def _persist_contact_decision(profile, resolved_fields, candidates, contact_context, user) -> _ContactDecision:
    """Write the Contact a decision names, before the decision itself is stored.

    A row with a planned Device has the Contact assigned to it. A row without one stores the Contact
    alone. Either way the returned fields name the Contact that was persisted, so the stored decision
    links it instead of the null the page posted.
    """
    if contact_context is None:
        return _ContactDecision(resolved_fields)
    source_row, result_row = contact_context
    device_id = _planned_device_id(result_row)
    if not device_id:
        # No Device to assign to yet, so the Contact itself is stored now.
        fields, note, contact = _store_contact_for_unmatched_row(profile, resolved_fields, candidates, user)
        return _ContactDecision(fields, note=note, contact=contact)
    write = _assign_contact_to_matched_device(profile, resolved_fields, source_row, device_id, user)
    if write is None:
        return _ContactDecision(resolved_fields)
    return _ContactDecision(
        {**resolved_fields, "contact_id": write.contact["id"]},
        write=write,
        contact=write.contact,
    )


def _saved_resolution_report(contact_write, contact_note):
    """Return the sentence and the write detail one saved Contact decision reports.

    An assignment that did not move is not a Device Contact update, and a Contact this save created
    is reported whether or not the assignment moved.
    """
    if contact_write is None:
        return "Resolution saved. Recalculate the preview to apply it." + contact_note, contact_note.strip()
    detail = (
        f"Contact '{contact_write.contact['name']}' was created in NetBox."
        if contact_write.contact_created
        else contact_note.strip()
    )
    message = (
        "Resolution saved and the linked Device Contact was updated."
        if contact_write.assignment_changed
        else "Resolution saved. The Device Contact already stood as decided."
    )
    return (f"{message} {detail}" if detail else message), detail


def _ensure_field_review_device_match(user, profile, source_id, device, source_asset_tag=""):
    """Persist the confirmed source-to-device identity for a field review."""
    existing_match = (
        DeviceExistingMatch.objects.select_for_update().filter(profile=profile, source_id=source_id).first()
    )
    if existing_match is not None and existing_match.netbox_device_id != device.pk:
        return False, "conflict"
    conflicting_match = (
        DeviceExistingMatch.objects.select_for_update()
        .filter(profile=profile, netbox_device_id=device.pk)
        .exclude(source_id=source_id)
        .first()
    )
    if conflicting_match is not None:
        return False, "conflict"
    if existing_match is not None and existing_match.netbox_device_id == device.pk:
        return True, ""
    try:
        save_permission_scoped_object(
            user,
            DeviceExistingMatch,
            {"profile": profile, "source_id": source_id},
            {
                "netbox_device_id": device.pk,
                "device_name": device.name,
                "source_asset_tag": source_asset_tag,
            },
        )
    except ObjectPermissionDenied:
        return False, "permission"
    return True, ""


# ---------------------------------------------------------------------------
# Fuzzy matching: source column name → NetBox target field canonical name
# ---------------------------------------------------------------------------

_ALIAS_TO_CANONICAL: dict[str, str] = {
    # rack_name
    "rack": "rack_name",
    "rack_name": "rack_name",
    "rack name": "rack_name",
    # device_name
    "name": "device_name",
    "device_name": "device_name",
    "device name": "device_name",
    "hostname": "device_name",
    "host": "device_name",
    # make
    "make": "make",
    "manufacturer": "make",
    "vendor": "make",
    "brand": "make",
    # model
    "model": "model",
    "device_type": "model",
    "device type": "model",
    "product": "model",
    # serial
    "serial": "serial",
    "serial_number": "serial",
    "serial number": "serial",
    "sn": "serial",
    # asset_tag
    "asset_tag": "asset_tag",
    "asset tag": "asset_tag",
    "asset": "asset_tag",
    "tag": "asset_tag",
    # source_id
    "source_id": "source_id",
    "source id": "source_id",
    "id": "source_id",
    "uid": "source_id",
    # u_position
    "u_position": "u_position",
    "u position": "u_position",
    "position": "u_position",
    "unit": "u_position",
    "u": "u_position",
    # u_height
    "u_height": "u_height",
    "u height": "u_height",
    "height": "u_height",
    "size": "u_height",
    # face
    "face": "face",
    "side": "face",
    # airflow
    "airflow": "airflow",
    "air_flow": "airflow",
    # status
    "status": "status",
    "state": "status",
    # device_class
    "device_class": "device_class",
    "device class": "device_class",
    "class": "device_class",
    "type": "device_class",
    "role": "device_class",
}


def _fuzzy_match_netbox_field(column_name: str) -> str | None:
    """Return the best-matching canonical target field name for a source column, or None."""
    normalised = column_name.strip().lower()
    if normalised in _ALIAS_TO_CANONICAL:
        return _ALIAS_TO_CANONICAL[normalised]
    matches = difflib.get_close_matches(normalised, _ALIAS_TO_CANONICAL.keys(), n=1, cutoff=0.6)
    if matches:
        return _ALIAS_TO_CANONICAL[matches[0]]
    return None


# ---------------------------------------------------------------------------
# ImportProfile
# ---------------------------------------------------------------------------


logger = logging.getLogger(__name__)


class ImportProfileListView(generic.ObjectListView):
    """List all import profiles with their mapping counts."""

    queryset = ImportProfile.objects.prefetch_related("column_mappings", "class_role_mappings", "device_type_mappings")
    table = ImportProfileTable
    filterset = ImportProfileFilterSet
    template_name = "netbox_data_import/importprofile_list.html"


class ImportProfileView(generic.ObjectView):
    """Detail view for a single import profile, with inline mapping tables."""

    queryset = ImportProfile.objects.prefetch_related("column_mappings", "class_role_mappings", "device_type_mappings")

    def get_extra_context(self, request, instance):
        """Inject inline mapping tables into the template context."""
        column_table = ColumnMappingTable(instance.column_mappings.all())
        class_role_table = ClassRoleMappingTable(instance.class_role_mappings.all())
        device_type_table = DeviceTypeMappingTable(instance.device_type_mappings.all())
        transform_table = ColumnTransformRuleTable(instance.column_transform_rules.all())
        return {
            "column_table": column_table,
            "class_role_table": class_role_table,
            "device_type_table": device_type_table,
            "transform_table": transform_table,
        }


class ImportProfileEditView(generic.ObjectEditView):
    """Create or edit an ImportProfile."""

    queryset = ImportProfile.objects.all()
    form = ImportProfileForm


class ImportProfileDeleteView(generic.ObjectDeleteView):
    """Delete an ImportProfile and all its child mappings."""

    queryset = ImportProfile.objects.all()


class ImportProfileBulkEditView(generic.BulkEditView):
    """Bulk-edit selected ImportProfiles."""

    queryset = ImportProfile.objects.all()
    filterset = ImportProfileFilterSet
    table = ImportProfileTable
    form = ImportProfileBulkEditForm


class ImportProfileBulkDeleteView(generic.BulkDeleteView):
    """Bulk-delete selected ImportProfiles."""

    queryset = ImportProfile.objects.all()
    table = ImportProfileTable


class ImportProfileChangeLogView(generic.ObjectChangeLogView):
    """Display the change log for one ImportProfile."""

    queryset = ImportProfile.objects.all()


# Scalar profile fields handled by _apply_profile_yaml_data.
# 'tags' (M2M) is intentionally excluded — use the edit UI or the flat import path.
_PROFILE_FIELDS = ("description", "source_adapter")


def _validate_model_instance(instance, label):
    """Call full_clean() and surface ValidationErrors as ValueError so the atomic block rolls back."""
    from django.core.exceptions import ValidationError as DjangoValidationError

    try:
        instance.full_clean(validate_unique=False)
    except DjangoValidationError as exc:
        if hasattr(exc, "message_dict"):
            msg = "; ".join(f"{f}: {', '.join(es)}" for f, es in exc.message_dict.items())
        else:
            msg = "; ".join(exc.messages)
        raise PreviewActionInvalid(f"Validation error in {label}: {msg}") from exc


def _legacy_adapter_config(profile_data):
    """Return the top-level `profile` keys releases up to 1.5.2 exported, as adapter configuration."""
    from .adapter_forms import FlatWorkbookConfigForm

    # The legacy keys are exactly the flat-workbook adapter's own settings.
    legacy_keys = set(FlatWorkbookConfigForm.base_fields) & set(profile_data)
    if not legacy_keys:
        return None
    conflicting = sorted({"adapter_config", "source_adapter"} & set(profile_data))
    if conflicting:
        raise ValueError(
            f"Profile key(s) {', '.join(sorted(legacy_keys))} belong to a release before the adapter "
            f"cutover and cannot be combined with {', '.join(conflicting)}."
        )
    config = {key: profile_data[key] for key in legacy_keys}
    # The legacy file names the Contact Role by slug; adapter_config stores its name.
    slug = config.get("primary_contact_role")
    if slug:
        from tenancy.models import ContactRole

        role = ContactRole.objects.filter(slug=slug).first()
        if role is None:
            raise ValueError(f"No Contact Role matches the primary_contact_role slug '{slug}'.")
        config["primary_contact_role"] = role.name
    return config


def _profile_defaults_from_yaml(profile_data):
    """Resolve the scalar profile values and the adapter configuration from YAML."""
    legacy_config = _legacy_adapter_config(profile_data)
    accepted = {"name", "adapter_config", *_PROFILE_FIELDS}
    if legacy_config is not None:
        accepted |= set(legacy_config)
    unknown = sorted(set(profile_data) - accepted)
    if unknown:
        raise ValueError(f"Unknown profile key(s): {', '.join(unknown)}")
    profile_defaults = {field: profile_data[field] for field in _PROFILE_FIELDS if field in profile_data}
    if legacy_config is not None:
        from .adapters import FlatWorkbookAdapter

        # Pinned, not DEFAULT_ADAPTER_KEY: a legacy file is a flat workbook whatever the default becomes.
        profile_defaults["source_adapter"] = FlatWorkbookAdapter.key
        profile_defaults["adapter_config"] = legacy_config
    elif "adapter_config" in profile_data:
        profile_defaults["adapter_config"] = profile_data["adapter_config"]
    return profile_defaults


def _get_or_init(model_class, **lookup):
    """Return the existing persisted instance matching *lookup*, or a new unsaved one.

    This enables validate-before-save semantics: callers can set fields on the
    returned instance, call ``_validate_model_instance``, and only then call
    ``instance.save()``.  DB-level errors (e.g. overlength strings) are thus
    caught by Django's field validators before any write reaches the database.
    """
    return model_class.objects.filter(**lookup).first() or model_class(**lookup)


def _set_if_present(instance, data, fields):
    """Set attributes on *instance* only when the corresponding key exists in *data*."""
    for name in fields:
        if name in data:
            setattr(instance, name, data[name])


def _save_or_refetch(instance, model_class, **lookup):
    """Persist *instance*, or return the row that won the concurrent insert."""
    resolved, _saved = save_or_refetch(instance, model_class, lookup)
    return resolved


def _iter_yaml_section(data, section_name, required_keys=()):
    """Yield mapping items for a named section in a parsed YAML dict.

    - Absent key → yields nothing (caller skips reconciliation).
    - Explicit null or non-list value → raises ValueError.
    - Explicit empty list → yields nothing (caller reconcile-deletes all).
    - Item missing a required key → raises ValueError with index and key name(s),
      preventing a bare KeyError from bubbling up with no context.
    """
    if section_name not in data:
        return
    section = data[section_name]
    if section is None or not isinstance(section, list):
        raise ValueError(
            f"'{section_name}' must be a list of mappings; "
            f"use [] to explicitly remove all entries, got {type(section).__name__}."
        )
    for idx, item in enumerate(section, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"'{section_name}[{idx}]' must be a mapping, got {type(item).__name__}.")
        missing = [k for k in required_keys if k not in item]
        if missing:
            raise ValueError(f"'{section_name}[{idx}]' missing required key(s): {', '.join(missing)}")
        yield item


def _delete_stale_device_type_mappings(profile, keep_keys):
    """Delete DeviceTypeMapping rows whose (source_make, source_model) is not in *keep_keys*.

    Uses a single DB-level exclusion via Q objects, consistent with how other sections
    handle reconcile-deletes, and avoids loading all existing rows into Python.
    """
    from django.db.models import Q

    qs = DeviceTypeMapping.objects.filter(profile=profile)
    if keep_keys:
        keep_q = Q()
        for make, model in keep_keys:
            keep_q |= Q(source_make=make, source_model=model)
        qs = qs.exclude(keep_q)
    qs.delete()


def _import_class_role_mappings(data, profile, stats):
    """Import class_role_mappings from YAML data into the given profile."""
    crm_source_classes = []
    for m in _iter_yaml_section(data, "class_role_mappings", ("source_class",)):
        instance = _get_or_init(ClassRoleMapping, profile=profile, source_class=m["source_class"])
        _set_if_present(instance, m, ("creates_rack", "role_slug", "ignore"))
        if "rack_type" in m and m["rack_type"]:
            from dcim.models import RackType

            try:
                instance.rack_type = RackType.objects.get(slug=m["rack_type"])
            except RackType.DoesNotExist as exc:
                raise ValueError(
                    f"class_role_mappings[{m['source_class']}]: RackType with slug '{m['rack_type']}' not found"
                ) from exc
        elif "rack_type" in m:
            instance.rack_type = None
        _validate_model_instance(instance, f"class_role_mappings[{m['source_class']}]")
        _save_or_refetch(instance, ClassRoleMapping, profile=profile, source_class=m["source_class"])
        crm_source_classes.append(m["source_class"])
        stats["class_role_mappings"] = stats.get("class_role_mappings", 0) + 1
    if "class_role_mappings" in data:
        ClassRoleMapping.objects.filter(profile=profile).exclude(source_class__in=crm_source_classes).delete()


def _release_replaced_column_policy_rows(profile, mapping_rows, transform_rows):
    """Remove rows that leave or change target ownership before validating their replacements."""
    if mapping_rows is not None:
        retained_mappings = {(row["source_column"], row["target_field"]) for row in mapping_rows}
        stale_mapping_ids = [
            mapping.pk
            for mapping in profile.column_mappings.only("pk", "source_column", "target_field")
            if (mapping.source_column, mapping.target_field) not in retained_mappings
        ]
        ColumnMapping.objects.filter(pk__in=stale_mapping_ids).delete()

    if transform_rows is None:
        return
    desired_by_source = {row["source_column"]: row for row in transform_rows}
    stale_transform_ids = []
    for rule in profile.column_transform_rules.only(
        "pk", "source_column", "pattern", "group_1_target", "group_2_target"
    ):
        desired = desired_by_source.get(rule.source_column)
        if desired is None or any(
            getattr(rule, field) != desired.get(field, getattr(rule, field))
            for field in ("pattern", "group_1_target", "group_2_target")
        ):
            stale_transform_ids.append(rule.pk)
    profile.column_transform_rules.filter(pk__in=stale_transform_ids).delete()


def _apply_profile_yaml_data(data):
    """Create or update an ImportProfile and all its nested mappings from parsed YAML data.

    ``data`` must be a dict with a top-level ``profile`` key (the format
    produced by :class:`ExportProfileYamlView`).

    Returns ``(profile, stats)`` where *stats* is a ``{section: count}`` dict.
    Raises ``ValueError`` with a descriptive message on invalid input.
    """
    from django.db import transaction

    from .models import ColumnTransformRule

    if not isinstance(data, dict) or "profile" not in data:
        raise ValueError("YAML must contain a top-level 'profile' key.")

    pdata = data["profile"]
    if not isinstance(pdata, dict):
        raise ValueError("The 'profile' value must be a mapping (dict), not a scalar or list.")
    if not pdata.get("name"):
        raise ValueError("Profile YAML must include a 'name' field.")

    mapping_rows = (
        list(_iter_yaml_section(data, "column_mappings", ("target_field", "source_column")))
        if "column_mappings" in data
        else None
    )
    transform_rows = (
        list(_iter_yaml_section(data, "column_transform_rules", ("source_column", "pattern")))
        if "column_transform_rules" in data
        else None
    )

    with transaction.atomic():
        # Only include fields that are explicitly present in the YAML so that a
        # partial reimport (e.g. just trimming child sections) does not silently
        # reset unrelated profile settings back to hard-coded defaults.
        profile_defaults = _profile_defaults_from_yaml(pdata)
        profile = _get_or_init(ImportProfile, name=pdata["name"])
        for field, value in profile_defaults.items():
            setattr(profile, field, value)
        _validate_model_instance(profile, "profile")
        profile = _save_or_refetch(profile, ImportProfile, name=pdata["name"])

        stats = {}
        _release_replaced_column_policy_rows(profile, mapping_rows, transform_rows)

        cm_ids = []
        for cm in mapping_rows or ():
            mapping_key = {
                "profile": profile,
                "source_column": cm["source_column"],
                "target_field": cm["target_field"],
            }
            instance = _get_or_init(ColumnMapping, **mapping_key)
            _validate_model_instance(instance, f"column_mappings[{cm['source_column']}->{cm['target_field']}]")
            instance = _save_or_refetch(instance, ColumnMapping, **mapping_key)
            cm_ids.append(instance.pk)
            stats["column_mappings"] = stats.get("column_mappings", 0) + 1
        if "column_mappings" in data:
            ColumnMapping.objects.filter(profile=profile).exclude(pk__in=cm_ids).delete()

        _import_class_role_mappings(data, profile, stats)

        dtm_keys = []
        for m in _iter_yaml_section(
            data,
            "device_type_mappings",
            ("source_make", "source_model", "netbox_manufacturer_slug", "netbox_device_type_slug"),
        ):
            instance = _get_or_init(
                DeviceTypeMapping, profile=profile, source_make=m["source_make"], source_model=m["source_model"]
            )
            instance.netbox_manufacturer_slug = m["netbox_manufacturer_slug"]
            instance.netbox_device_type_slug = m["netbox_device_type_slug"]
            _validate_model_instance(instance, f"device_type_mappings[{m['source_make']}/{m['source_model']}]")
            _save_or_refetch(
                instance,
                DeviceTypeMapping,
                profile=profile,
                source_make=m["source_make"],
                source_model=m["source_model"],
            )
            dtm_keys.append((m["source_make"], m["source_model"]))
            stats["device_type_mappings"] = stats.get("device_type_mappings", 0) + 1
        if "device_type_mappings" in data:
            _delete_stale_device_type_mappings(profile, dtm_keys)

        mm_source_makes = []
        for m in _iter_yaml_section(data, "manufacturer_mappings", ("source_make", "netbox_manufacturer_slug")):
            instance = _get_or_init(ManufacturerMapping, profile=profile, source_make=m["source_make"])
            instance.netbox_manufacturer_slug = m["netbox_manufacturer_slug"]
            _validate_model_instance(instance, f"manufacturer_mappings[{m['source_make']}]")
            _save_or_refetch(instance, ManufacturerMapping, profile=profile, source_make=m["source_make"])
            mm_source_makes.append(m["source_make"])
            stats["manufacturer_mappings"] = stats.get("manufacturer_mappings", 0) + 1
        if "manufacturer_mappings" in data:
            ManufacturerMapping.objects.filter(profile=profile).exclude(source_make__in=mm_source_makes).delete()

        ctr_source_columns = []
        for r in transform_rows or ():
            instance = _get_or_init(ColumnTransformRule, profile=profile, source_column=r["source_column"])
            instance.pattern = r["pattern"]
            _set_if_present(instance, r, ("group_1_target", "group_2_target"))
            _validate_model_instance(instance, f"column_transform_rules[{r['source_column']}]")
            _save_or_refetch(instance, ColumnTransformRule, profile=profile, source_column=r["source_column"])
            ctr_source_columns.append(r["source_column"])
            stats["column_transform_rules"] = stats.get("column_transform_rules", 0) + 1
        if "column_transform_rules" in data:
            ColumnTransformRule.objects.filter(profile=profile).exclude(source_column__in=ctr_source_columns).delete()

    return profile, stats


class ImportProfileBulkImportView(generic.BulkImportView):
    """Import ImportProfile objects via NetBox's built-in import UI.

    Supports two formats from the same text area / file upload:

    * **Hierarchical YAML** – the format produced by the "Export YAML" button
      (top-level keys: ``profile``, ``column_mappings``, ``class_role_mappings``,
      ``device_type_mappings``, ``manufacturer_mappings``,
      ``column_transform_rules``).  All nested mappings are created/updated.
    * **Flat CSV/YAML** – one record per profile, plain metadata fields only
      (name, description, sheet_name, …).  Falls back to NetBox's standard
      bulk-import logic.
    """

    queryset = ImportProfile.objects.all()
    model_form = ImportProfileImportForm

    def post(self, request):
        """Detect format and apply hierarchical YAML or delegate to flat bulk import."""
        import yaml

        # Read the raw input from the file upload or the text area.
        upload = request.FILES.get("upload_file")
        if upload:
            try:
                raw = upload.read().decode("utf-8-sig")
            except Exception as exc:  # pragma: no cover
                messages.error(request, f"Could not read uploaded file: {exc}")
                return redirect(reverse("plugins:netbox_data_import:importprofile_bulk_import"))
        else:
            raw = request.POST.get("data", "").strip()

        if not raw:
            messages.error(request, "No data provided.")
            return redirect(reverse("plugins:netbox_data_import:importprofile_bulk_import"))

        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError:
            # Input failed YAML parsing — let NetBox's BulkImportView handle it
            # (covers CSV and flat formats with YAML-invalid characters).
            if upload:
                upload.seek(0)
            return super().post(request)

        # Hierarchical format: delegate to shared helper.
        if isinstance(data, dict) and "profile" in data:
            try:
                profile, stats = _apply_profile_yaml_data(data)
            except ValueError as exc:  # KeyError no longer escapes since _iter_yaml_section validates required_keys
                messages.error(request, str(exc))
                return redirect(reverse("plugins:netbox_data_import:importprofile_bulk_import"))
            summary = ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in stats.items())
            messages.success(request, f"Profile '{profile.name}' imported/updated. {summary}.")
            return redirect(profile.get_absolute_url())

        # Flat format: let NetBox's BulkImportView handle it.
        # Rewind the file stream so the parent handler receives the full content.
        if upload:
            upload.seek(0)
        return super().post(request)


# ---------------------------------------------------------------------------
# Shared base views for ImportProfile child objects
# ---------------------------------------------------------------------------


def _profile_pk_for_policy_write(view, url_kwargs):
    """Return the ImportProfile whose policy this write changes.

    An edit or delete reads it off the row, through the already-scoped queryset. An add names it in
    the URL and reads it through the same scope. Either way a target outside the operator's grant
    raises here, which `post()` reaches before it enters the lock, so it never holds a row the
    operator cannot see.
    """
    if "pk" in url_kwargs:
        return get_object_or_404(view.queryset, pk=url_kwargs["pk"]).profile_id
    profile_pk = url_kwargs.get("profile_pk")
    if profile_pk is None:
        return None
    return get_object_or_404(ImportProfile.objects.restrict(view.request.user, "view"), pk=profile_pk).pk


class _ProfileChildEditView(generic.ObjectEditView):
    """Base add/edit view for objects that belong to an ImportProfile.

    Assigns ``profile`` on add from the ``profile_pk`` URL kwarg, and redirects back to the
    parent profile detail page after a successful save. The forms carry no ``profile`` field,
    so a posted one is ignored.

    Object scoping comes from NetBox's ``ObjectPermissionRequiredMixin``. Django's
    ``PermissionRequiredMixin`` must not sit ahead of it: that shadows the ``restrict()`` call.

    Override ``get_required_permission`` so that add-URLs (which carry
    ``profile_pk`` but not ``pk``) are not misidentified as edit-URLs by
    NetBox's generic ``dispatch`` hook.
    """

    def get_required_permission(self):
        action = "change" if "pk" in self.kwargs else "add"
        return get_permission_for_model(self.queryset.model, action)

    def get_object(self, **kwargs):
        """Filter only by ``pk`` — ignore ``profile_pk`` URL kwarg.

        NetBox's ``ObjectEditView.get()`` passes all URL kwargs to
        ``get_object_or_404``.  ``profile_pk`` is not a field on child
        models, so we must strip it before the ORM lookup.
        """
        if "pk" in kwargs:
            return get_object_or_404(self.queryset, pk=kwargs["pk"])
        return self.queryset.model()

    def alter_object(self, obj, request, url_args, url_kwargs):
        if not obj.pk and "profile_pk" in url_kwargs:
            # The URL names the parent, so the add scope has to cover the profile as well as the row.
            obj.profile = get_object_or_404(
                ImportProfile.objects.restrict(request.user, "view"), pk=url_kwargs["profile_pk"]
            )
        return obj

    def get_return_url(self, request, obj=None):
        if obj is not None and getattr(obj, "profile", None):
            return obj.profile.get_absolute_url()
        return super().get_return_url(request, obj)

    def get_extra_context(self, request, instance):
        if instance.pk:
            return {"profile": instance.profile}
        profile_pk = self.kwargs.get("profile_pk")
        if profile_pk:
            return {"profile": get_object_or_404(ImportProfile.objects.restrict(request.user, "view"), pk=profile_pk)}
        return {}

    def post(self, request, *args, **kwargs):
        """Write under the profile policy lock, so a replan cannot commit against stale policy."""
        try:
            with locked_profile_policy(_profile_pk_for_policy_write(self, kwargs)):
                # atomic-exit-safe: locked-policy-write-committed
                return super().post(request, *args, **kwargs)
        except ImportProfile.DoesNotExist:
            # The URL names a profile that is gone, which is the 404 its own fetch would give.
            raise Http404 from None


class _ProfileChildDeleteView(generic.ObjectDeleteView):
    """Base delete view for objects that belong to an ImportProfile.

    Redirects to the parent profile detail page after successful deletion. Object scoping comes from
    NetBox's ``ObjectPermissionRequiredMixin``, which Django's must not shadow.
    """

    def get_return_url(self, request, obj=None):
        if obj is not None and getattr(obj, "profile", None):
            return obj.profile.get_absolute_url()
        return super().get_return_url(request, obj)

    def post(self, request, *args, **kwargs):
        """Delete under the profile policy lock, for the same reason the edit view takes it."""
        try:
            with locked_profile_policy(_profile_pk_for_policy_write(self, kwargs)):
                # atomic-exit-safe: locked-policy-delete-committed
                return super().post(request, *args, **kwargs)
        except ImportProfile.DoesNotExist:
            raise Http404 from None


# ---------------------------------------------------------------------------
# ColumnMapping CRUD
# ---------------------------------------------------------------------------


class ColumnMappingAddView(_ProfileChildEditView):
    """Add a column mapping to an existing ImportProfile."""

    queryset = ColumnMapping.objects.all()
    form = ColumnMappingForm
    template_name = "netbox_data_import/columnmapping_edit.html"


class ColumnMappingEditView(_ProfileChildEditView):
    """Edit an existing column mapping."""

    queryset = ColumnMapping.objects.all()
    form = ColumnMappingForm
    template_name = "netbox_data_import/columnmapping_edit.html"


class ColumnMappingDeleteView(_ProfileChildDeleteView):
    """Delete a column mapping."""

    queryset = ColumnMapping.objects.all()


# ---------------------------------------------------------------------------
# ClassRoleMapping CRUD
# ---------------------------------------------------------------------------


class ClassRoleMappingAddView(_ProfileChildEditView):
    """Add a class→role mapping to an existing ImportProfile."""

    queryset = ClassRoleMapping.objects.all()
    form = ClassRoleMappingForm
    template_name = "netbox_data_import/classrolemapping_edit.html"


class ClassRoleMappingEditView(_ProfileChildEditView):
    """Edit an existing class→role mapping."""

    queryset = ClassRoleMapping.objects.all()
    form = ClassRoleMappingForm
    template_name = "netbox_data_import/classrolemapping_edit.html"


class ClassRoleMappingDeleteView(_ProfileChildDeleteView):
    """Delete a class→role mapping."""

    queryset = ClassRoleMapping.objects.all()


# ---------------------------------------------------------------------------
# DeviceTypeMapping CRUD
# ---------------------------------------------------------------------------


class DeviceTypeMappingAddView(_ProfileChildEditView):
    """Add a device type mapping to an existing ImportProfile."""

    queryset = DeviceTypeMapping.objects.all()
    form = DeviceTypeMappingForm
    template_name = "netbox_data_import/devicetypemapping_edit.html"


class DeviceTypeMappingEditView(_ProfileChildEditView):
    """Edit an existing device type mapping."""

    queryset = DeviceTypeMapping.objects.all()
    form = DeviceTypeMappingForm
    template_name = "netbox_data_import/devicetypemapping_edit.html"


class DeviceTypeMappingDeleteView(_ProfileChildDeleteView):
    """Delete a device type mapping."""

    queryset = DeviceTypeMapping.objects.all()


# ---------------------------------------------------------------------------
# Import Wizard — Phase 2 (setup + preview)
# ---------------------------------------------------------------------------

# These views intentionally use raw django.views.View rather than a NetBox
# generic view base.  The wizard is a three-step, session-backed state machine
# (setup → preview → run → results) that does not correspond to any single
# NetBox generic view pattern (ObjectEditView, ObjectListView, etc.).  Using a
# raw View keeps the control flow explicit and avoids fighting ObjectEditView's
# form-save lifecycle, queryset requirements, and redirect conventions.


class ImportSetupView(PermissionRequiredMixin, View):
    """Step 1: select profile, upload file, choose site/location/tenant."""

    permission_required = "netbox_data_import.change_importprofile"

    def get(self, request):
        """Render the import setup form."""
        initial = {}
        if profile_pk := request.GET.get("profile"):
            initial["profile"] = profile_pk
        form = ImportSetupForm(initial=initial, user=request.user)
        return render(request, "netbox_data_import/import_setup.html", _import_setup_context(request, form))

    def post(self, request):
        """Store the uploaded file, plan it, and redirect to the preview step."""
        form = ImportSetupForm(request.POST, request.FILES, user=request.user)
        if not form.is_valid():
            return render(request, "netbox_data_import/import_setup.html", _import_setup_context(request, form))

        profile = form.cleaned_data["profile"]
        excel_file = form.cleaned_data["excel_file"]
        site = form.cleaned_data["site"]
        location = form.cleaned_data.get("location")
        tenant = form.cleaned_data.get("tenant")

        context_data = {
            "profile_id": profile.pk,
            "site_id": site.pk,
            "location_id": location.pk if location else None,
            "tenant_id": tenant.pk if tenant else None,
            "filename": excel_file.name,
        }
        document = SourceDocument.store(
            profile=profile,
            content=excel_file.read(),
            filename=excel_file.name,
            uploaded_by=request.user,
        )
        context_data["source_document_id"] = document.pk
        planning_context = {
            "site_id": context_data["site_id"],
            "location_id": context_data["location_id"],
            "tenant_id": context_data["tenant_id"],
        }
        try:
            plan = ImportEngine.plan(profile, document, request.user, planning_context)
        except (adapters.SourceUnreadable, adapters.UnknownSourceAdapter, PlanningTargetUnavailable, PlanError) as exc:
            document.delete()
            messages.error(request, f"Failed to parse file: {exc}")
            return render(request, "netbox_data_import/import_setup.html", _import_setup_context(request, form))

        workspace = ReviewWorkspace(plan)
        record_recalculated_preview(request.session, plan)
        request.session["import_rows"] = workspace.source_rows
        request.session["import_context"] = context_data
        request.session["import_preview_pending"] = True
        request.session[PREVIEW_USE_MATERIALIZED_ONCE_SESSION_KEY] = True
        request.session.pop("import_preview_source_job_id", None)
        _clear_restored_import_job(request)
        request.session["import_unused_columns"] = {
            column["name"]: {"count": column["count"], "samples": column["samples"]}
            for column in workspace.unused_columns
        }
        return redirect(reverse("plugins:netbox_data_import:import_preview"))


_DEVICE_CONFLICT_ROW_LIST_KEYS = (
    "duplicate_serial_rows",
    "duplicate_asset_tag_rows",
)


def _other_conflict_row_identities(row, source_object_types_by_number):
    """Return the row number and object type named by one preview error."""
    identities = [
        (row_number, source_object_types_by_number.get(row_number, row.object_type))
        for row_number in row.extra_data.get("duplicate_source_id_rows", ())
    ]
    for key in _DEVICE_CONFLICT_ROW_LIST_KEYS:
        identities.extend((row_number, row.object_type) for row_number in row.extra_data.get(key, ()))
    conflict_row_number = row.extra_data.get("conflict_row_number")
    if conflict_row_number is not None:
        identities.append((conflict_row_number, row.object_type))
    return tuple(dict.fromkeys(identity for identity in identities if identity != (row.row_number, row.object_type)))


def _conflict_comparison_row(row, source_rows_by_number, *, is_current):
    """Return the source facts that the conflict comparison shows for one result row."""
    source_row = source_rows_by_number.get(row.row_number, {})
    extra_data = row.extra_data
    serial = extra_data.get("source_serial", source_row.get("serial", ""))
    asset_tag = extra_data.get("asset_tag", source_row.get("asset_tag", ""))
    rack_name = row.rack_name or source_row.get("rack_name", "")
    if not rack_name and row.object_type == "rack":
        rack_name = row.name
    return {
        "row_number": row.row_number,
        "name": row.name,
        "source_id": row.source_id,
        "serial": serial,
        "asset_tag": asset_tag,
        "rack_name": rack_name,
        "u_position": extra_data.get("u_position", source_row.get("u_position")),
        "face": extra_data.get("face", source_row.get("face", "")),
        "action": row.action,
        "detail": row.detail,
        # The comparison offers the same action the row column does, so it carries the same facts.
        "identity_conflict": extra_data.get("identity_conflict", ""),
        "identity_conflicts": extra_data.get("identity_conflicts", []),
        "duplicate_serial": extra_data.get("duplicate_serial", ""),
        "is_current": is_current,
    }


def _preview_rows_with_conflict_comparisons(workspace, source_rows, profile):
    """Copy preview rows and attach comparisons for each within-import row conflict."""
    result_rows_by_identity = {(row.row_number, row.object_type): row for row in workspace.units}
    source_rows_by_number = {row.get("_row_number"): row for row in source_rows}
    object_types_by_class = {
        mapping.source_class: "rack" if mapping.creates_rack else "device"
        for mapping in profile.class_role_mappings.all()
    }
    source_object_types_by_number = {}
    for source_row in source_rows:
        source_class = source_text(source_row.get("device_class"))
        if source_class in object_types_by_class:
            source_object_types_by_number[source_row.get("_row_number")] = object_types_by_class[source_class]
    conflict_rows_by_row = {}
    for row in workspace.units:
        other_rows = [
            result_rows_by_identity.get(identity)
            for identity in _other_conflict_row_identities(row, source_object_types_by_number)
        ]
        other_rows = [other_row for other_row in other_rows if other_row is not None]
        if not other_rows:
            continue
        conflict_rows_by_row[(row.row_number, row.object_type)] = [
            _conflict_comparison_row(row, source_rows_by_number, is_current=True),
            *(_conflict_comparison_row(other_row, source_rows_by_number, is_current=False) for other_row in other_rows),
        ]

    preview_rows = []
    for row in workspace.units:
        preview_rows.append(
            replace(
                row,
                extra_data={
                    **row.extra_data,
                    "conflict_rows": conflict_rows_by_row.get((row.row_number, row.object_type), []),
                },
            )
        )
    return preview_rows


class ImportPreviewView(PermissionRequiredMixin, View):
    """Step 2: show dry-run results, let user confirm or go back."""

    permission_required = "netbox_data_import.change_importprofile"

    def get(self, request):
        """Render the current preview URL."""
        use_materialized_result = request.session.pop(PREVIEW_USE_MATERIALIZED_ONCE_SESSION_KEY, False) is True
        return self.render_preview(
            request,
            request.get_full_path(),
            use_materialized_result=use_materialized_result,
        )

    def render_preview(self, request, preview_url, *, use_materialized_result=False):
        """Replan the stored source and render the Review Workspace."""
        ctx = request.session.get("import_context", {})
        if not ctx:
            messages.warning(request, "No import in progress. Please start a new import.")
            return redirect(reverse("plugins:netbox_data_import:import_setup"))

        profile = ImportProfile.objects.restrict(request.user, "change").filter(pk=ctx.get("profile_id")).first()
        if not profile:
            _discard_import_preview(request)
            messages.warning(request, "Import profile not found.")
            return redirect(reverse("plugins:netbox_data_import:import_setup"))

        # The session outlives an upgrade, so the stored profile can name a retired adapter.
        try:
            validate_registered_adapter(profile)
        except ValidationError as exc:
            _discard_import_preview(request)
            messages.error(request, "; ".join(exc.messages))
            return redirect(reverse("plugins:netbox_data_import:import_setup"))

        document = SourceDocument.objects.filter(pk=ctx.get("source_document_id"), profile=profile).first()
        if document is None:
            _discard_import_preview(request)
            messages.warning(request, "The stored source is no longer available. Upload it again.")
            return redirect(reverse("plugins:netbox_data_import:import_setup"))

        stored_plan = request.session.get(PREVIEW_PLAN_SESSION_KEY)
        if use_materialized_result and isinstance(stored_plan, dict):
            try:
                plan = ImportPlan.from_dict(stored_plan)
            except PlanError as exc:
                _discard_import_preview(request)
                messages.error(request, str(exc))
                return redirect(reverse("plugins:netbox_data_import:import_setup"))
        else:
            planning_context = {
                "site_id": ctx.get("site_id"),
                "location_id": ctx.get("location_id"),
                "tenant_id": ctx.get("tenant_id"),
            }
            try:
                plan = ImportEngine.plan(profile, document, request.user, planning_context)
            except PlanningTargetUnavailable:
                _discard_import_preview(request)
                messages.warning(request, "The saved import target is no longer available. Start a new preview.")
                return redirect(reverse("plugins:netbox_data_import:import_setup"))
            record_recalculated_preview(request.session, plan)
        result = ReviewWorkspace(plan)
        rows = result.source_rows
        request.session["import_rows"] = rows

        # Build existing resolutions map for the split-name modal preview
        import json as _json

        from .models import SourceResolution

        existing_resolutions = {}
        for res in SourceResolution.objects.filter(profile=profile):
            existing_resolutions.setdefault(str(res.source_id), {})[res.source_column] = {
                "original_value": res.original_value,
                "resolved_fields": res.resolved_fields,
            }

        # Build device matching context for template
        device_matches = DeviceExistingMatch.objects.filter(profile=profile)
        device_match_source_ids = [m.source_id for m in device_matches]
        device_match_info = {}

        # Fetch device serial numbers from NetBox Device objects
        from dcim.models import Device

        netbox_device_ids = [m.netbox_device_id for m in device_matches]
        devices_by_id = {
            d.id: d for d in Device.objects.restrict(request.user, "view").filter(id__in=netbox_device_ids)
        }

        for match in device_matches:
            device = devices_by_id.get(match.netbox_device_id)
            # Bindings to devices outside the user's view scope carry no target metadata.
            if device is None:
                continue
            device_match_info[match.source_id] = {
                "device_id": match.netbox_device_id,
                "device_name": match.device_name,
                "device_serial": device.serial,
            }

        view_mode = parse_qs(urlsplit(preview_url).query).get("view", [profile.adapter_settings.preview_view_mode])[-1]

        # Build unused columns list: filter out any that are now mapped
        mapped_source_cols = set(profile.column_mappings.values_list("source_column", flat=True))
        raw_unused = {column["name"]: column for column in result.unused_columns}
        unused_columns = [
            {
                "name": col,
                "count": int(stats.get("count") or 0),
                "samples": stats.get("samples") or [],
                "suggested_field": _fuzzy_match_netbox_field(col),
            }
            for col, stats in raw_unused.items()
            if isinstance(stats, dict) and col not in mapped_source_cols
        ]
        unused_columns.sort(key=lambda x: -x["count"])
        conflicts_by_row = {
            str(r.row_number): r.extra_data.get("conflicts", {}) for r in result.units if r.extra_data.get("conflicts")
        }
        # The modal names a field for the operator; the catalog is where those names live.
        target_field_labels = {key: CATALOG.display(key) for key, _label in CATALOG.choices()}
        candidate_values_by_row = {}
        try:
            for row in result.units:
                candidate_values = _candidate_values(row.extra_data)
                if candidate_values:
                    candidate_values_by_row[str(row.row_number)] = candidate_values
        except ValidationError as exc:
            _discard_import_preview(request)
            messages.error(request, "; ".join(exc.messages))
            return redirect(reverse("plugins:netbox_data_import:import_setup"))
        contact_suggestions_by_row = {
            str(r.row_number): r.extra_data["contact_suggestion"]
            for r in result.units
            if r.extra_data.get("contact_suggestion")
        }
        contact_role_suggestions_by_row = {
            row_number: suggest_contact_roles(candidates["contact"])
            for row_number, candidates in candidate_values_by_row.items()
            if candidates.get("contact")
        }
        extra_columns_by_row = {
            str(r.row_number): r.extra_data.get("extra_columns", {})
            for r in result.units
            if r.extra_data.get("extra_columns")
        }
        split_field_values_by_source_id = {
            r.source_id: {
                "device_name": r.name or "",
                "asset_tag": r.extra_data.get("asset_tag", ""),
                "serial": r.extra_data.get("source_serial", ""),
                "make": r.extra_data.get("source_make", ""),
                "model": r.extra_data.get("source_model", ""),
                "rack_name": r.rack_name or "",
                "source_id": r.source_id,
            }
            for r in result.units
            if r.object_type == "device" and r.source_id
        }
        preview_rows = _preview_rows_with_conflict_comparisons(result, rows, profile)

        non_card_error_rows = [
            r
            for r in result.units
            if r.action == "error" and not (r.object_type == "device" or (r.object_type == "rack" and r.name))
        ]

        return render(
            request,
            "netbox_data_import/import_preview.html",
            {
                "result": result,
                "preview_rows": preview_rows,
                "filename": ctx.get("filename", ""),
                "profile_id": ctx.get("profile_id"),
                "profile": profile,
                "preview_url": preview_url,
                "view_mode": view_mode,
                "existing_resolutions_json": _json.dumps(existing_resolutions).translate(
                    {ord("<"): "\\u003C", ord(">"): "\\u003E", ord("&"): "\\u0026"}
                ),
                "existing_resolutions": existing_resolutions,
                "plugin_version": _plugin_version,
                "resolved_contact_source_ids": [
                    source_id for source_id, columns in existing_resolutions.items() if "candidate:contact" in columns
                ],
                "can_create_role": request.user.has_perm("dcim.add_devicerole"),
                "unused_columns": unused_columns,
                "target_field_choices": CATALOG.choices(output_kinds=profile.output_kinds),
                "syncable_fields": SyncDeviceFieldView._ALLOWED_FIELDS,
                "reviewable_fields": DeviceFieldReviewer.reviewable_fields(),
                "device_match_source_ids": device_match_source_ids,
                "device_match_info": device_match_info,
                "conflicts_by_row": conflicts_by_row,
                "target_field_labels": target_field_labels,
                "candidate_values_by_row": candidate_values_by_row,
                "contact_suggestions_by_row": contact_suggestions_by_row,
                "contact_role_suggestions_by_row": contact_role_suggestions_by_row,
                "extra_columns_by_row": extra_columns_by_row,
                "split_field_values_by_source_id": split_field_values_by_source_id,
                "non_card_error_rows": non_card_error_rows,
                "preview_revision": current_preview_revision(request.session),
            },
        )


def _user_import_jobs(request):
    """Return native data-import Jobs owned by the current user."""
    from .jobs import ImportJobRunner

    return ImportJobRunner.get_jobs().filter(
        user=request.user,
        data__job_type=ImportJobRunner.job_type,
    )


def _import_setup_context(request, form):
    """Return the setup form and the most relevant resumable import state."""
    resume_job = _resume_import_job(request)
    preview_rows = request.session.get("import_rows")
    preview_context = request.session.get("import_context")
    resume_preview = (
        request.session.get("import_preview_pending") is True
        and isinstance(preview_rows, list)
        and bool(preview_rows)
        and isinstance(preview_context, dict)
        and bool(preview_context.get("profile_id"))
        and bool(preview_context.get("site_id"))
    )
    return {"form": form, "resume_job": resume_job, "resume_preview": resume_preview}


def _discard_import_preview(request):
    """Remove session data that belongs only to an unsubmitted preview."""
    for key in (
        "import_context",
        "import_idempotency_key",
        PREVIEW_PLAN_SESSION_KEY,
        "import_rows",
        "import_unused_columns",
    ):
        request.session.pop(key, None)
    request.session["import_preview_pending"] = False
    request.session.pop(PREVIEW_USE_MATERIALIZED_ONCE_SESSION_KEY, None)
    request.session.pop("import_preview_source_job_id", None)


def _clear_restored_import_job(request):
    """Remove an audit result restored beside a pending preview."""
    request.session.pop("import_restored_execution_id", None)


def _resume_import_job(request):
    """Return the session Job or the user's latest active import Job."""
    from core.choices import JobStatusChoices

    jobs = _user_import_jobs(request).filter(status__in=JobStatusChoices.ENQUEUED_STATE_CHOICES)
    if job_pk := request.session.get("import_background_job_id"):
        if job := jobs.filter(pk=job_pk).first():
            return job
    return jobs.first()


def _import_source_rows_available(request, job):
    """Return whether one Job's stored source is still available."""
    source_document_id = (job.data or {}).get("source_document_id")
    return bool(source_document_id and SourceDocument.objects.filter(pk=source_document_id).exists())


def _import_job_progress(job, preview_blocked=False, source_rows_available=False):
    """Return current row progress from native Job data and RQ metadata."""
    from core.choices import JobStatusChoices

    data = job.data or {}
    processed = int(data.get("processed") or 0)
    total = int(data.get("total") or 0)
    if job.status in JobStatusChoices.ENQUEUED_STATE_CHOICES:
        import django_rq

        try:
            queue = django_rq.get_queue(job.queue_name or "default")
        except KeyError:
            rq_job = None
        else:
            rq_job = queue.fetch_job(str(job.job_id))
        if rq_job is not None:
            processed = int(rq_job.meta.get("processed", processed) or 0)
            total = int(rq_job.meta.get("total", total) or 0)
    percentage = round(processed * 100 / total) if total else 0
    return {
        "job": job,
        "processed": processed,
        "total": total,
        "percentage": percentage,
        "is_active": job.status in JobStatusChoices.ENQUEUED_STATE_CHOICES,
        "is_completed": job.status == JobStatusChoices.STATUS_COMPLETED,
        "is_failed": job.status in (JobStatusChoices.STATUS_FAILED, JobStatusChoices.STATUS_ERRORED),
        "preview_available": bool(data.get("accepted_plan"))
        and isinstance(data.get("context_data"), dict)
        and source_rows_available,
        "preview_blocked": preview_blocked,
        "message": data.get("message") or "",
    }


def _restore_import_session(request, job):
    """Restore preview or audit state when a user returns to a Job URL."""
    from core.choices import JobStatusChoices

    data = job.data or {}
    if request.session.get("import_background_job_id") != job.pk:
        request.session["import_background_job_id"] = job.pk
    preview_is_pending = request.session.get("import_preview_pending") is True
    failed_preview_available = job.status in (
        JobStatusChoices.STATUS_FAILED,
        JobStatusChoices.STATUS_ERRORED,
    ) and (
        data.get("accepted_plan")
        and isinstance(data.get("context_data"), dict)
        and _import_source_rows_available(request, job)
    )
    if failed_preview_available and not preview_is_pending:
        request.session[PREVIEW_PLAN_SESSION_KEY] = data["accepted_plan"]
        request.session["import_context"] = data["context_data"]
        request.session["import_preview_pending"] = True
        request.session["import_preview_source_job_id"] = job.pk
        retire_preview_revision(request.session)
        preview_is_pending = True
    if data.get("import_execution_id"):
        if preview_is_pending:
            request.session["import_restored_execution_id"] = data["import_execution_id"]
        else:
            _clear_restored_import_job(request)
            request.session["import_execution_id"] = data["import_execution_id"]
            request.session["import_preview_pending"] = False
    return data


class ImportRunView(PermissionRequiredMixin, View):
    """Step 3: queue the accepted Import Plan."""

    permission_required = "netbox_data_import.change_importprofile"

    def post(self, request):
        """Queue the accepted plan and redirect to its progress page."""
        ctx_data = request.session.get("import_context")
        plan_data = request.session.get(PREVIEW_PLAN_SESSION_KEY)
        if not isinstance(ctx_data, dict) or not isinstance(plan_data, dict):
            messages.warning(request, "No import in progress.")
            return redirect(reverse("plugins:netbox_data_import:import_setup"))
        if request.session.get("import_preview_pending") is not True:
            if job_pk := request.session.get("import_background_job_id"):
                return redirect(reverse("plugins:netbox_data_import:import_progress", kwargs={"pk": job_pk}))
            messages.warning(request, "No import in progress.")
            return redirect(reverse("plugins:netbox_data_import:import_setup"))
        if request.session.get(PREVIEW_DIRTY_SESSION_KEY) is True:
            messages.warning(request, "Recalculate and review the saved preview changes before importing.")
            return redirect(reverse("plugins:netbox_data_import:import_preview"))

        profile = get_object_or_404(
            ImportProfile.objects.restrict(request.user, "change"),
            pk=ctx_data["profile_id"],
        )
        try:
            validate_registered_adapter(profile)
        except ValidationError as exc:
            _discard_import_preview(request)
            messages.error(request, "; ".join(exc.messages))
            return redirect(reverse("plugins:netbox_data_import:import_setup"))

        document = SourceDocument.objects.filter(pk=ctx_data.get("source_document_id"), profile=profile).first()
        if document is None:
            _discard_import_preview(request)
            messages.error(request, "The stored source is no longer available. Upload it again.")
            return redirect(reverse("plugins:netbox_data_import:import_setup"))
        try:
            accepted = ImportPlan.from_dict(plan_data)
        except PlanError as exc:
            _discard_import_preview(request)
            messages.error(request, str(exc))
            return redirect(reverse("plugins:netbox_data_import:import_setup"))
        if ReviewWorkspace(accepted).has_errors:
            messages.warning(request, "Resolve every preview error before importing.")
            return redirect(reverse("plugins:netbox_data_import:import_preview"))
        selection = [unit.identity for unit in accepted.units if unit.disposition == "actionable"]
        if not selection:
            messages.info(request, "The accepted Import Plan has no changes to apply.")
            return redirect(reverse("plugins:netbox_data_import:import_preview"))

        from core.choices import JobNotificationChoices
        from .jobs import ImportJobRunner

        idempotency_key = request.session.get("import_idempotency_key") or uuid.uuid4().hex
        request.session["import_idempotency_key"] = idempotency_key
        _clear_restored_import_job(request)
        with transaction.atomic():
            job = ImportJobRunner.enqueue(
                name=ImportJobRunner.name,
                user=request.user,
                notifications=JobNotificationChoices.NOTIFICATION_NEVER,
                job_timeout=3600,
                profile_id=profile.pk,
                source_document_id=document.pk,
                accepted_plan=plan_data,
                selection=selection,
                idempotency_key=idempotency_key,
            )
            job.data = {
                "job_type": ImportJobRunner.job_type,
                "phase": "queued",
                "processed": 0,
                "total": 0,
                "filename": ctx_data.get("filename", ""),
                "profile_id": profile.pk,
                "profile_name": profile.name,
                "source_document_id": document.pk,
                "accepted_plan": plan_data,
                "context_data": ctx_data,
            }
            job.save(update_fields=["data"])

        request.session["import_background_job_id"] = job.pk
        request.session["import_preview_pending"] = False
        request.session.pop("import_preview_source_job_id", None)
        return redirect(reverse("plugins:netbox_data_import:import_progress", kwargs={"pk": job.pk}))


class ImportProgressView(PermissionRequiredMixin, View):
    """Show one resumable background import and its current progress."""

    permission_required = "netbox_data_import.change_importprofile"

    def get(self, request, pk):
        """Render the full progress page."""
        job = get_object_or_404(_user_import_jobs(request), pk=pk)
        preview_was_pending = (
            request.session.get("import_preview_pending") is True
            and request.session.get("import_preview_source_job_id") != job.pk
        )
        _restore_import_session(request, job)
        return render(
            request,
            "netbox_data_import/import_progress.html",
            _import_job_progress(
                job,
                preview_blocked=preview_was_pending,
                source_rows_available=_import_source_rows_available(request, job),
            ),
        )


class ImportProgressStatusView(PermissionRequiredMixin, View):
    """Render the HTMX progress fragment or redirect a completed import."""

    permission_required = "netbox_data_import.change_importprofile"

    def get(self, request, pk):
        """Return the current Job state."""
        job = get_object_or_404(_user_import_jobs(request), pk=pk)
        preview_was_pending = (
            request.session.get("import_preview_pending") is True
            and request.session.get("import_preview_source_job_id") != job.pk
        )
        data = _restore_import_session(request, job)
        if job.status == "completed" and data.get("import_execution_id"):
            response = HttpResponse(status=204)
            response["HX-Redirect"] = reverse("plugins:netbox_data_import:import_results")
            return response
        return render(
            request,
            "netbox_data_import/_import_progress.html",
            _import_job_progress(
                job,
                preview_blocked=preview_was_pending,
                source_rows_available=_import_source_rows_available(request, job),
            ),
        )


class ImportResultsView(PermissionRequiredMixin, View):
    """Step 4: show the Import Execution audit outcome."""

    permission_required = "netbox_data_import.view_importexecution"

    def get(self, request):
        """Render the results page for the most recent Import Execution."""
        restored_execution_id = request.session.get("import_restored_execution_id")
        execution_id = restored_execution_id or request.session.get("import_execution_id")
        if not execution_id:
            return redirect(reverse("plugins:netbox_data_import:import_setup"))

        execution = (
            ImportExecution.objects.select_related("profile", "source_document")
            .filter(
                pk=execution_id,
                actor=request.user,
            )
            .first()
        )
        if execution is None or not request.user.has_perm(self.permission_required, execution):
            return redirect(reverse("plugins:netbox_data_import:import_setup"))
        if restored_execution_id is None:
            request.session.pop("import_background_job_id", None)
            request.session["import_preview_pending"] = False
            request.session.pop("import_preview_source_job_id", None)
            for key in ("import_rows", "import_context", PREVIEW_PLAN_SESSION_KEY, "import_unused_columns"):
                request.session.pop(key, None)
        return render(
            request,
            "netbox_data_import/import_results.html",
            {"execution": execution, "job_id": execution.pk},
        )


class ImportExecutionListView(PermissionRequiredMixin, generic.ObjectListView):
    """List every past Import Execution, including the retained legacy rows."""

    queryset = ImportExecution.objects.select_related("profile").all()
    table = ImportExecutionTable
    template_name = "netbox_data_import/importexecution_list.html"
    permission_required = "netbox_data_import.view_importexecution"

    def get_required_permission(self):
        """Answer NetBox's own permission hook, which it checks separately from permission_required."""
        return "netbox_data_import.view_importexecution"


# ---------------------------------------------------------------------------
# ColumnTransformRule CRUD
# ---------------------------------------------------------------------------


class ColumnTransformRuleAddView(_ProfileChildEditView):
    """Add a column transform rule to an existing ImportProfile."""

    queryset = ColumnTransformRule.objects.all()
    form = ColumnTransformRuleForm
    template_name = "netbox_data_import/columntransformrule_edit.html"


class ColumnTransformRuleEditView(_ProfileChildEditView):
    """Edit an existing column transform rule."""

    queryset = ColumnTransformRule.objects.all()
    form = ColumnTransformRuleForm
    template_name = "netbox_data_import/columntransformrule_edit.html"


class ColumnTransformRuleDeleteView(_ProfileChildDeleteView):
    """Delete a column transform rule."""

    queryset = ColumnTransformRule.objects.all()


# ---------------------------------------------------------------------------
# Ignore / Unignore device
# ---------------------------------------------------------------------------
# The action views below (Ignore/Unignore/Sync/Quick*) are lightweight POST
# endpoints that return JSON or an immediate redirect.  No NetBox generic base
# class exists for this pattern; PermissionRequiredMixin + View is intentional.
# ---------------------------------------------------------------------------


class _PermissionScopedWriteMixin:
    """Mark preview writers and render their object-scope refusals in one place."""

    def dispatch(self, request, *args, **kwargs):
        try:
            return super().dispatch(request, *args, **kwargs)
        except ObjectPermissionDenied as exc:
            # The permission names an object the caller may not be allowed to know exists.
            logger.warning("%s: write refused outside the caller's object scope: %s", type(self).__name__, exc)
            error = "Permission denied: this action is outside your NetBox object permissions."
            if getattr(self, "permission_denied_response_format", "redirect") == "json" or _wants_json(request):
                return JsonResponse({"ok": False, "error": error}, status=403)
            messages.error(request, error)
            return redirect(_safe_next_url(request, "plugins:netbox_data_import:import_preview"))


class IgnoreDeviceView(_PermissionScopedWriteMixin, PermissionRequiredMixin, View):
    """Mark a specific device (by source_id) as ignored for a profile."""

    permission_required = "netbox_data_import.change_importprofile"

    def post(self, request):
        """Add the specified device to the profile's ignore list."""
        from .models import IgnoredDevice

        profile_id = _parse_posted_profile_id(request)
        source_id = request.POST.get("source_id")
        device_name = request.POST.get("device_name", "")
        next_url = _safe_next_url(request, "plugins:netbox_data_import:import_preview")

        if profile_id is None:
            messages.error(request, "A valid import profile is required.")
        elif source_id:
            profile = get_object_or_404(ImportProfile.objects.restrict(request.user, "change"), pk=profile_id)
            ignored = _get_or_init(IgnoredDevice, profile=profile, source_id=source_id)
            if ignored.pk is None:
                ignored.device_name = device_name
                try:
                    _validate_model_instance(ignored, f"ignored device '{source_id}'")
                except PreviewActionInvalid as exc:
                    messages.error(request, str(exc))
                    return redirect(next_url)
            save_permission_scoped_object(
                request.user,
                IgnoredDevice,
                {"profile": profile, "source_id": source_id},
                {"device_name": device_name},
                on_existing="keep",
            )
            messages.success(request, f"Device '{device_name or source_id}' added to ignore list.")
        return redirect(next_url)


class UnignoreDeviceView(_PermissionScopedWriteMixin, PermissionRequiredMixin, View):
    """Remove a device from the ignore list."""

    permission_required = "netbox_data_import.change_importprofile"

    def post(self, request):
        """Remove the specified device from the profile's ignore list."""
        from .models import IgnoredDevice

        profile_id = _parse_posted_profile_id(request)
        source_id = request.POST.get("source_id")
        next_url = _safe_next_url(request, "plugins:netbox_data_import:import_preview")

        if profile_id is None:
            messages.error(request, "A valid import profile is required.")
        elif source_id:
            profile = get_object_or_404(ImportProfile.objects.restrict(request.user, "change"), pk=profile_id)
            count = delete_permission_scoped_objects(
                request.user,
                IgnoredDevice.objects.filter(profile=profile, source_id=source_id),
            )
            if count:
                messages.success(request, "Device removed from ignore list.")
            else:
                messages.warning(request, "Device was not on the ignore list (may be ignored by class mapping).")
        return redirect(next_url)


def _wants_json(request) -> bool:
    """Return whether one preview action expects a JSON response."""
    return "application/json" in request.headers.get("Accept", "")


def _session_holds_a_preview(request) -> bool:
    """Answer whether this save belongs to a preview at all, rather than standing alone."""
    return bool(request.session.get("import_rows") or request.session.get("import_context"))


def _preview_is_active(request) -> bool:
    """Answer whether a preview is still on screen and able to receive a decision."""
    return request.session.get("import_preview_pending") is True


def _preview_accepts_decisions(request) -> bool:
    """Answer whether the posted decision belongs to the preview that is still active."""
    return _preview_is_active(request) and request.POST.get("preview_revision") == current_preview_revision(
        request.session
    )


def _preview_action_error(request, next_url, message, *, status=409):
    """Return one preview-action error through JSON or the form fallback."""
    if _wants_json(request):
        # A JSON caller renders the reason itself, so a queued message would surface later
        # on an unrelated page.
        return JsonResponse({"ok": False, "error": message}, status=status)
    messages.error(request, message)
    return redirect(next_url)


def _saved_preview_action_response(request, next_url, message):
    """Report a saved preview decision without rebuilding its materialized plan."""
    mark_preview_dirty(request.session)
    if _wants_json(request):
        return JsonResponse(pending_preview_payload(None, message))
    messages.success(request, message)
    return redirect(next_url)


def _resolved_import_target(ctx_data, user):
    """Return the engine context the saved import names, or None once that target went stale.

    The session outlives a permission change, so each request re-reads the target in the operator's
    own scope: a revoked ObjectPermission has to make the target unavailable, not merely unlisted.
    """
    from dcim.models import Location, Site
    from tenancy.models import Tenant

    sites = Site.objects.restrict(user, "view")
    locations = Location.objects.restrict(user, "view")
    tenants = Tenant.objects.restrict(user, "view")
    site = sites.filter(pk=ctx_data.get("site_id")).first()
    location = locations.filter(pk=ctx_data.get("location_id")).first() if ctx_data.get("location_id") else None
    tenant = tenants.filter(pk=ctx_data.get("tenant_id")).first() if ctx_data.get("tenant_id") else None
    if (
        site is None
        or (ctx_data.get("location_id") and (location is None or location.site_id != site.pk))
        or (ctx_data.get("tenant_id") and tenant is None)
    ):
        return None
    return {"site": site, "location": location, "tenant": tenant}


def _stale_preview_reason(request):
    """Return why the preview can no longer take a decision, or None."""
    if not _session_holds_a_preview(request):
        return None
    if not _preview_is_active(request):
        return "The import already started, so this preview can no longer take a decision."
    # A second tab can recalculate between opening the modal and saving it.
    if not _preview_accepts_decisions(request):
        return "This preview is no longer the current one. Reload the preview and choose again."
    return None


class _PreviewRowDecision(NamedTuple):
    """The request state a preview row decision has to establish before it may write."""

    profile: object
    ctx_data: dict
    rows: list
    row_number: int
    source_id: str
    source_row: dict
    next_url: str


def _preview_row_decision(request):
    """Return the validated state for a preview row decision, or the response that refuses it.

    Both decisions guard the same preview, so they read these preconditions from here: two copies
    drift, and a gate that is missing on one of them is a write the operator never authorized.
    """
    ctx_data = request.session.get("import_context") or {}
    rows = request.session.get("import_rows") or []
    next_url = _safe_next_url(request, "plugins:netbox_data_import:import_preview")
    profile_id = _parse_posted_profile_id(request)
    if profile_id is None:
        messages.error(request, "A valid import profile is required.")
        return None, _name_resolution_response(request, next_url)
    if str(ctx_data.get("profile_id")) != str(profile_id):
        messages.error(request, "The selected profile is not the active import profile.")
        return None, _name_resolution_response(request, next_url)

    profile = get_object_or_404(
        ImportProfile.objects.restrict(request.user, "change"),
        pk=profile_id,
    )
    stale_reason = _stale_preview_reason(request)
    if stale_reason is not None:
        # An inline render would replace the preview that the queued import has frozen.
        messages.error(request, stale_reason)
        return None, _navigation_response(request, next_url)
    try:
        row_number = int(request.POST.get("row_number", ""))
    except (TypeError, ValueError):
        messages.error(request, "A valid source row is required.")
        return None, _name_resolution_response(request, next_url)

    source_id = source_text(request.POST.get("source_id"))
    source_rows = [
        row for row in rows if row.get("_row_number") == row_number and source_text(row.get("source_id")) == source_id
    ]
    if not source_id or len(source_rows) != 1:
        messages.error(request, "The source ID and row must identify one active import row.")
        return None, _name_resolution_response(request, next_url)

    return _PreviewRowDecision(profile, ctx_data, rows, row_number, source_id, source_rows[0], next_url), None


def _field_review_row(request):
    """Return the current row and target field for a review POST."""
    preview = load_cached_preview(request)
    if preview is None:
        return None
    profile, result = preview
    try:
        row_number = int(request.POST.get("row_number", ""))
    except (TypeError, ValueError):
        return None
    target_field = request.POST.get("target_field", "").strip()
    row = next(
        (
            item
            for item in result.units
            if item.row_number == row_number and item.object_type == "device" and item.action in {"update", "error"}
        ),
        None,
    )
    if row is None or DeviceFieldReviewer.definition(target_field) is None:
        return None
    if not source_text(row.source_id):
        return None
    return profile, result, row, target_field


def _preview_device_action(request):
    """Return the cached row and permitted Device for one preview action."""
    preview = load_cached_preview(request)
    if preview is None:
        return None
    _profile, result = preview
    try:
        row_number = int(request.POST.get("row_number", ""))
    except (TypeError, ValueError):
        return None
    row = next(
        (
            item
            for item in result.units
            if item.row_number == row_number and item.object_type == "device" and item.action in {"update", "error"}
        ),
        None,
    )
    device_id = row.extra_data.get("netbox_device_id") if row is not None else None
    if not device_id:
        return None

    from dcim.models import Device

    device = (
        Device.objects.restrict(request.user, "change")
        .select_related("device_type__manufacturer", "rack__location", "role", "tenant", "location")
        .filter(pk=device_id)
        .first()
    )
    if device is None:
        return None
    return row, device


def _offered_difference(row, target_field) -> bool:
    """Return whether the preview offered this field for review.

    A field the import does not write is reported too, and an operator ignores it to stop
    the preview reporting it. Only a synced field has to be a writable difference.
    """
    return target_field in row.extra_data.get("field_diff", {}) or target_field in row.extra_data.get(
        "field_informational", {}
    )


def _preview_field_intent(request, target_field):
    """Return one authoritative field value after checking its NetBox baseline."""
    action = _preview_device_action(request)
    if action is None:
        return None, "The active preview row is no longer available."
    row, device = action
    if target_field not in row.extra_data.get("field_diff", {}):
        return None, "The selected field difference is no longer present."
    snapshots = row.extra_data.get("field_review_snapshots", {}).get(target_field)
    if not isinstance(snapshots, dict):
        return None, "The selected field has no authoritative preview value."
    current = DeviceFieldReviewer.current_snapshot(device, target_field)
    if current is None or current.get("canonical") != snapshots.get("netbox", {}).get("canonical"):
        return None, "The matched NetBox value changed. Recalculate the preview and try again."
    if target_field in {"u_position", "face"} and not _placement_matches_preview(device, row):
        return None, "The matched NetBox placement changed. Recalculate the preview and try again."
    return (row, device, snapshots.get("file", {}).get("canonical", "")), None


def _placement_matches_preview(device, row) -> bool:
    """Return whether placement fields still match the materialized preview."""
    state = row.extra_data.get("_placement_state")
    if not isinstance(state, dict):
        return False
    return (
        device.rack_id == state.get("rack_id")
        and normalize_for_compare(device.position) == state.get("position", "")
        and (device.face or "") == state.get("face", "")
    )


def _placement_action_intent(request):
    """Return authoritative placement inputs for preview and direct actions."""
    from dcim.models import Device

    if request.POST.get("row_number"):
        action = _preview_device_action(request)
        if action is None:
            return None, "The active preview row is no longer available.", 409
        row, device = action
        if not _placement_matches_preview(device, row):
            return (
                None,
                "The matched NetBox placement changed. Recalculate the preview and try again.",
                409,
            )
        return (
            {
                "row": row,
                "device": device,
                "rack_name": row.rack_name,
                "u_position": row.extra_data.get("u_position", ""),
                "face": row.extra_data.get("face", ""),
            },
            None,
            None,
        )

    try:
        device = (
            Device.objects.restrict(request.user, "change")
            .select_related("site", "location", "rack", "device_type")
            .get(pk=request.POST.get("device_id"))
        )
    except (Device.DoesNotExist, ValueError, TypeError):
        return None, "Device not found", 200
    return (
        {
            "row": None,
            "device": device,
            "rack_name": request.POST.get("rack_name", ""),
            "u_position": request.POST.get("u_position", ""),
            "face": request.POST.get("face", ""),
        },
        None,
        None,
    )


class IgnoreFieldDifferenceView(PermissionRequiredMixin, View):
    """Ignore one exact current field difference for a matched Device."""

    permission_required = "netbox_data_import.add_ignoredfielddifference"

    def post(self, request):
        """Save current snapshots from a fresh active preview."""
        next_url = _safe_next_url(request, "plugins:netbox_data_import:import_preview")
        review = _field_review_row(request)
        if review is None:
            return _preview_action_error(
                request,
                next_url,
                "The selected field difference is no longer current. Recalculate the preview and try again.",
            )
        profile, _result, row, target_field = review
        if not _offered_difference(row, target_field):
            return _preview_action_error(
                request,
                next_url,
                "The selected field difference is no longer present. Refresh the preview.",
            )
        snapshots = row.extra_data.get("field_review_snapshots", {}).get(target_field)
        device_id = row.extra_data.get("netbox_device_id")
        if not isinstance(snapshots, dict) or not device_id:
            return _preview_action_error(
                request,
                next_url,
                "The selected field difference has no current matched device.",
            )

        from dcim.models import Device

        device = Device.objects.restrict(request.user, "view").filter(pk=device_id).first()
        if device is None:
            return _preview_action_error(request, next_url, "The matched NetBox device is no longer available.")
        current_snapshot = DeviceFieldReviewer.current_snapshot(device, target_field)
        if current_snapshot is None or current_snapshot.get("canonical") != snapshots.get("netbox", {}).get(
            "canonical"
        ):
            return _preview_action_error(
                request,
                next_url,
                "The matched NetBox value changed. Recalculate the preview and try again.",
            )
        lookup = {
            "profile": profile,
            "source_id": row.source_id,
            "netbox_device_id": device.pk,
            "target_field": target_field,
        }
        defaults = {
            "file_snapshot": snapshots.get("file", {}),
            "netbox_snapshot": snapshots.get("netbox", {}),
        }
        try:
            with transaction.atomic():
                binding_allowed, binding_error = _ensure_field_review_device_match(
                    request.user,
                    profile,
                    row.source_id,
                    device,
                    source_text(row.extra_data.get("asset_tag"))[:50],
                )
                if not binding_allowed:
                    message = (
                        "Permission denied: cannot persist the source-to-device field-review match."
                        if binding_error == "permission"
                        else "The source row or device is already linked elsewhere."
                    )
                    # atomic-exit-safe: binding-refused-before-write
                    return _preview_action_error(request, next_url, message)
                # A denial raises, so the binding written above unwinds with the block.
                save_permission_scoped_object(
                    request.user,
                    IgnoredFieldDifference,
                    lookup,
                    defaults,
                )
        except ObjectPermissionDenied:
            return _preview_action_error(
                request,
                next_url,
                "Permission denied: cannot create or change this field review.",
            )
        except ValidationError as exc:
            return _preview_action_error(request, next_url, "; ".join(exc.messages), status=400)
        except IntegrityError:
            return _preview_action_error(
                request,
                next_url,
                "The field review or device link changed while this request was being processed. Try again.",
            )
        messages.success(request, f"Ignored the current {target_field} difference.")
        mark_preview_dirty(request.session)
        if _wants_json(request):
            return JsonResponse(
                pending_preview_payload(
                    row.row_number,
                    f"Ignored the current {target_field} difference.",
                )
            )
        return redirect(next_url)


class UnignoreFieldDifferenceView(PermissionRequiredMixin, View):
    """Remove one exact current field-difference review for a matched Device."""

    permission_required = "netbox_data_import.delete_ignoredfielddifference"

    def post(self, request):
        """Delete only the review represented by the fresh active preview."""
        next_url = _safe_next_url(request, "plugins:netbox_data_import:import_preview")
        review = _field_review_row(request)
        if review is None:
            return _preview_action_error(
                request,
                next_url,
                "The selected field review is no longer current. Recalculate the preview and try again.",
            )
        profile, _result, row, target_field = review
        if target_field not in row.extra_data.get("field_ignored", {}):
            return _preview_action_error(
                request,
                next_url,
                "The selected field review is no longer current. Refresh the preview.",
            )
        device_id = row.extra_data.get("netbox_device_id")
        from dcim.models import Device

        device = Device.objects.restrict(request.user, "view").filter(pk=device_id).first()
        if device is None:
            return _preview_action_error(request, next_url, "The matched NetBox device is no longer available.")
        try:
            with transaction.atomic():
                record = (
                    IgnoredFieldDifference.objects.select_for_update()
                    .filter(
                        profile=profile,
                        source_id=row.source_id,
                        netbox_device_id=device.pk,
                        target_field=target_field,
                    )
                    .first()
                )
                if record is None:
                    # atomic-exit-safe: record-absent-before-write
                    return _preview_action_error(
                        request,
                        next_url,
                        "The selected field review is no longer current. Refresh the preview.",
                    )
                if not request.user.has_perm("netbox_data_import.delete_ignoredfielddifference", record):
                    # atomic-exit-safe: delete-denied-before-write
                    return _preview_action_error(
                        request,
                        next_url,
                        "Permission denied: cannot remove this field review.",
                    )
                binding_allowed, binding_error = _ensure_field_review_device_match(
                    request.user,
                    profile,
                    row.source_id,
                    device,
                    source_text(row.extra_data.get("asset_tag"))[:50],
                )
                if not binding_allowed:
                    message = (
                        "Permission denied: cannot persist the source-to-device field-review match."
                        if binding_error == "permission"
                        else "The source row or device is already linked elsewhere."
                    )
                    # atomic-exit-safe: binding-refused-before-delete
                    return _preview_action_error(request, next_url, message)
                record.delete()
        except IntegrityError:
            return _preview_action_error(
                request,
                next_url,
                "The field review or device link changed while this request was being processed. Try again.",
            )
        messages.success(request, f"Showing the {target_field} difference again.")
        mark_preview_dirty(request.session)
        if _wants_json(request):
            return JsonResponse(
                pending_preview_payload(
                    row.row_number,
                    f"Showing the {target_field} difference again.",
                )
            )
        return redirect(next_url)


class RemoveExtraIpView(PermissionRequiredMixin, View):
    """Remove one stored IP from a device's import record."""

    permission_required = "dcim.change_device"

    def post(self, request):
        """Remove an IP field from the device's import record."""
        from dcim.models import Device

        device_id = request.POST.get("device_id")
        ip_field = request.POST.get("ip_field")

        def _safe_return(device=None):
            url = request.POST.get("next", "")
            if url and url_has_allowed_host_and_scheme(
                url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
            ):
                return redirect(url)
            if device:
                return redirect(device.get_absolute_url())
            return redirect("/")

        if not device_id or not ip_field:
            messages.error(request, "Missing device_id or ip_field.")
            return _safe_return()

        if ip_field not in ("primary_ip4", "primary_ip6", "oob_ip"):
            messages.error(request, f"Invalid ip_field: {ip_field}")
            return _safe_return()

        device = get_object_or_404(Device.objects.restrict(request.user, "change"), pk=device_id)
        import_source = stored_import_source(device)
        unassigned_ips = dict(import_source.unassigned_ips) if import_source is not None else {}

        if ip_field in unassigned_ips:
            del unassigned_ips[ip_field]
            import_source.unassigned_ips = unassigned_ips
            import_source.save(update_fields=["unassigned_ips"])
            messages.success(request, f"Removed {ip_field} from the import record.")
        else:
            messages.info(request, f"{ip_field} was not in the import record.")

        return _safe_return(device)


# ---------------------------------------------------------------------------
# Sync single device field from import file value
# ---------------------------------------------------------------------------


class _AjaxPermissionView(ConditionalLoginRequiredMixin, View):
    """Base for AJAX/JSON endpoints — inherits NetBox's ``ConditionalLoginRequiredMixin``.

    Subclasses set ``permission_required`` (a Django permission string) to gate
    access. Unauthenticated requests receive a JSON 401 (never a redirect, since
    these endpoints are called via ``fetch``). Authenticated users without the
    required permission receive a JSON 403. ``ConditionalLoginRequiredMixin`` is
    still inherited so that login redirects work if the request is ever reached
    via a browser navigation (e.g. direct URL), but the explicit checks above
    fire first for API callers.
    """

    permission_required: str | tuple[str, ...] | None = None
    permission_denied_response_format = "json"

    def dispatch(self, request, *args, **kwargs):
        from django.http import JsonResponse

        if not request.user.is_authenticated:
            return JsonResponse({"ok": False, "error": "Authentication required"}, status=401)
        if self.permission_required and not request.user.has_perm(self.permission_required):
            return JsonResponse({"ok": False, "error": "Permission denied"}, status=403)
        return super().dispatch(request, *args, **kwargs)


class ContactLookupView(_AjaxPermissionView):
    """Search visible NetBox Contacts for the contact-resolution picker."""

    permission_required = "tenancy.view_contact"

    def get(self, request):
        """Return a small real-shape Contact result set."""
        from django.db.models import Q
        from django.http import JsonResponse
        from tenancy.models import Contact

        query = request.GET.get("q", "").strip()
        if len(query) < 2:
            return JsonResponse({"results": []})
        contacts = (
            Contact.objects.restrict(request.user, "view")
            .filter(Q(name__icontains=query) | Q(email__icontains=query) | Q(phone__icontains=query))
            .order_by("name", "email", "pk")[:20]
        )
        return JsonResponse(
            {
                "results": [
                    {
                        "id": contact.pk,
                        "name": contact.name,
                        "email": contact.email,
                        "phone": contact.phone,
                    }
                    for contact in contacts
                ]
            }
        )


class ContactSuggestionView(_AjaxPermissionView):
    """Return the Contact one preview row's candidate values identify, as it stands now."""

    permission_required = "tenancy.view_contact"

    def get(self, request):
        """Recompute one row's suggestion, so a Contact created on another row is offered here."""
        from django.http import JsonResponse

        try:
            profile_id = int(request.GET.get("profile_id", ""))
        except (TypeError, ValueError):
            profile_id = None
        source_id = request.GET.get("source_id", "")
        if profile_id is None or not source_id:
            return JsonResponse({"error": "A valid import profile and source row are required."}, status=400)
        profile = ImportProfile.objects.filter(pk=profile_id).first()
        if profile is None:
            return JsonResponse({"error": "A valid import profile is required."}, status=400)
        try:
            # The open picker outlives an upgrade, so the stored profile can name a retired adapter.
            validate_registered_adapter(profile)
            candidates, _source_row, _result_row = _contact_candidate_context(request, profile.pk, source_id)
        except ValidationError as exc:
            return JsonResponse({"error": "; ".join(exc.messages)}, status=400)
        return JsonResponse({"suggestion": PrimaryContactResolver.suggest(candidates, profile, request.user)})


class SyncDeviceFieldView(_AjaxPermissionView):
    """Apply a single field value from the import file to an existing NetBox device."""

    permission_required = "dcim.change_device"

    _IP_FIELDS = ("primary_ip4", "primary_ip6", "oob_ip")
    _ALLOWED_FIELDS = {"device_name", "u_position", "status", "serial", "asset_tag", "face", "airflow", *_IP_FIELDS}

    def post(self, request):
        """Apply one previewed field value to its matched Device."""
        from django.http import JsonResponse

        from dcim.models import Device

        field = request.POST.get("field", "")

        if not field or field not in self._ALLOWED_FIELDS:
            return JsonResponse({"ok": False, "error": f"Field '{field}' is not syncable"})

        is_preview_action = bool(request.POST.get("row_number"))
        if is_preview_action:
            intent, error = _preview_field_intent(request, field)
            if error:
                return JsonResponse({"ok": False, "error": error}, status=409)
            row, device, value = intent
        else:
            device_id = request.POST.get("device_id")
            value = request.POST.get("value", "")
            try:
                device = Device.objects.restrict(request.user, "change").select_related("device_type").get(pk=device_id)
            except (Device.DoesNotExist, ValueError, TypeError):
                return JsonResponse({"ok": False, "error": "Device not found"})

        try:
            # Nothing wraps this request, and a receiver on the model can require a transaction.
            with transaction.atomic():
                display = self._apply_field(device, field, value, status_map(), request.user)
        except PreviewActionInvalid as exc:
            return JsonResponse({"ok": False, "error": str(exc)})
        except Exception:
            logger.exception(
                "SyncDeviceFieldView failed for device_id=%s field=%s",
                device.pk,
                field,
            )
            return JsonResponse({"ok": False, "error": "An internal error occurred."}, status=500)

        if is_preview_action:
            mark_preview_dirty(request.session)
            return JsonResponse(
                pending_preview_payload(
                    row.row_number,
                    f"Updated {field} to {display}.",
                )
            )
        return JsonResponse({"ok": True, "display": display})

    @staticmethod
    def _writer_safe_text(device, label, model_field, value):
        """Reject a value the writer would otherwise truncate away from what the preview showed."""
        text = str(value)
        limit = type(device)._meta.get_field(model_field).max_length
        if len(text) > limit:
            raise PreviewActionInvalid(f"The {label} is {len(text)} characters; NetBox allows {limit}.")
        return text

    def _apply_field(self, device, field, value, status_map, user):
        """Write one previewed value onto the device, through that field's own writer."""
        if field in self._IP_FIELDS:
            return self._apply_ip_field(device, field, value, user)
        writer = {
            "airflow": lambda: self._apply_airflow(device, value),
            "device_name": lambda: self._apply_device_name(device, value),
            "u_position": lambda: self._apply_u_position(device, value),
            "status": lambda: self._apply_status(device, value, status_map),
            "serial": lambda: self._apply_serial(device, value),
            "asset_tag": lambda: self._apply_asset_tag(device, value),
            "face": lambda: self._apply_face(device, value),
        }.get(field)
        if writer is None:
            raise PreviewActionInvalid(f"Field '{field}' is not syncable")
        return writer()

    def _apply_device_name(self, device, value):
        new_name = self._writer_safe_text(device, "device name", "name", value)
        if type(device).objects.filter(site=device.site, name=new_name).exclude(pk=device.pk).exists():
            raise PreviewActionInvalid(f"A device named '{new_name}' already exists in site '{device.site}'")
        device.name = new_name
        device.save(update_fields=["name"])
        return new_name

    def _apply_u_position(self, device, value):
        pos = source_position(value)
        if pos is None:
            raise PreviewActionInvalid(f"Cannot parse '{value}' as a finite number for u_position")
        zero_u_type = _zero_u_device_type(device)
        if zero_u_type:
            raise PreviewActionInvalid(f"Cannot set a rack position: the device type '{zero_u_type}' is 0U.")
        device.position = pos
        self._reject_invalid_placement(device)
        device.save(update_fields=["position"])
        return f"U{device.position}"

    @staticmethod
    def _apply_status(device, value, status_map):
        text = str(value).strip().lower()
        # A NetBox status slug is accepted directly too (for example "active", "offline").
        mapped = status_map.get(text) or (text if text in set(status_map.values()) else None)
        if mapped is None:
            raise PreviewActionInvalid(f"Unknown status value '{value}'")
        device.status = mapped
        device.save(update_fields=["status"])
        return device.status

    def _apply_serial(self, device, value):
        device.serial = self._writer_safe_text(device, "serial", "serial", value)
        device.save(update_fields=["serial"])
        return device.serial

    def _apply_asset_tag(self, device, value):
        device.asset_tag = self._writer_safe_text(device, "asset tag", "asset_tag", value) if value else None
        device.save(update_fields=["asset_tag"])
        return device.asset_tag

    def _apply_face(self, device, value):
        if device.rack_id is None:
            raise PreviewActionInvalid(
                "Cannot set face: device has no rack assigned. Sync rack first, or use Sync Placement."
            )
        zero_u_type = _zero_u_device_type(device)
        if zero_u_type:
            raise PreviewActionInvalid(f"Cannot set a rack face: the device type '{zero_u_type}' is 0U.")
        mapped = _FACE_MAP.get(str(value).strip().lower())
        if mapped is None:
            raise PreviewActionInvalid(f"Unknown face value '{value}' — expected 'front' or 'rear'")
        device.face = mapped
        self._reject_invalid_placement(device)
        device.save(update_fields=["face"])
        return device.face

    @staticmethod
    def _apply_airflow(device, value):
        """Write the airflow the source row states, in the wording the importer already reads."""
        _side, airflow_map, _status = translation_maps()
        text = str(value).strip().lower()
        mapped = airflow_map.get(text)
        # The stored value is also accepted, so a row already carrying one syncs as it stands.
        if mapped is None and text in set(airflow_map.values()):
            mapped = text
        if mapped is None:
            raise PreviewActionInvalid(f"Unknown airflow value '{value}'")
        device.airflow = mapped
        device.save(update_fields=["airflow"])
        return device.airflow

    def _apply_ip_field(self, device, field, value, user):
        """Point one of the device's IP fields at the address the source row carries."""
        try:
            target = ip_assignment.resolve(device, field, value)
        except ip_assignment.IPAssignmentError as exc:
            raise PreviewActionInvalid(str(exc)) from exc

        if target.already_held:
            # The device carries it already, so only the field moves. No IPAM row is written.
            held = target.held
            if getattr(device, f"{field}_id", None) != held.pk:
                setattr(device, field, held)
                device.save(update_fields=[field])
            return target.summary

        try:
            address = ip_assignment.apply(target, user)
        except ValidationError as exc:
            raise PreviewActionInvalid("; ".join(exc.messages)) from exc
        except ObjectPermissionDenied as exc:
            raise PreviewActionInvalid(f"Permission denied: {exc} for this IP address.") from exc
        setattr(device, field, address)
        device.save(update_fields=[field])
        return f"{address.address} on {target.interface.name}"

    @staticmethod
    def _reject_invalid_placement(device) -> None:
        """Reject a placement value NetBox would refuse, before it reaches an unvalidated save."""
        try:
            _validate_device_placement(device)
        except ValidationError as exc:
            raise PreviewActionInvalid(_placement_error_text(exc)) from exc


def _lookup_rack_for_device(device, value):
    """Look up a Rack by name within ``device.site``, honoring ``device.location``.

    If the device has a location set, the rack must be in the same location. If the
    device has no location, the rack must also have no location (the implicit
    "default location" semantic).

    Returns ``(rack, None)`` on success or ``(None, error_message)`` on failure.
    Error messages are static, controlled strings — no exception text is exposed,
    so the result is safe to return directly to the client.
    """
    from dcim.models import Rack

    name = (str(value) if value is not None else "").strip()
    if not name:
        return None, "Rack name is empty"
    if device.site_id is None:
        return None, "Device has no site; cannot resolve rack"
    qs = Rack.objects.filter(site=device.site, name=name)
    if device.location_id is not None:
        qs = qs.filter(location=device.location)
        loc_str = f" / location '{device.location}'"
    else:
        qs = qs.filter(location__isnull=True)
        loc_str = ""
    racks = list(qs[:2])
    if not racks:
        return None, f"Rack '{name}' not found in site '{device.site}'{loc_str}"
    if len(racks) > 1:
        return None, f"Multiple racks named '{name}' found; cannot disambiguate"
    return racks[0], None


def _validate_device_placement(device) -> None:
    """Run NetBox validation and reject only errors caused by placement fields."""
    try:
        device.full_clean()
    except ValidationError as exc:
        if not hasattr(exc, "message_dict"):
            raise
        placement_fields = {"rack", "location", "position", "face", "device_type", "__all__"}
        errors = {field: messages for field, messages in exc.message_dict.items() if field in placement_fields}
        if errors:
            raise ValidationError(errors) from exc


_FACE_MAP = {"front": "front", "rear": "rear", "0": "front", "1": "rear"}


def _placement_error_text(exc) -> str:
    """Return one readable line for a placement ValidationError."""
    if hasattr(exc, "message_dict"):
        return "; ".join(f"{name}: {', '.join(messages)}" for name, messages in exc.message_dict.items())
    return "; ".join(exc.messages)


def _zero_u_device_type(device) -> str:
    """Return the device type label when it is zero-U, which takes no position or face."""
    device_type = device.device_type
    if device_type is not None and device_type.u_height == 0:
        return str(device_type)
    return ""


def _set_rack_placement(device, u_position, face, zero_u_type):
    """Set the rack position and face on *device*.

    Returns the written field names, the field names a zero-U device type cannot take,
    and one error message for a value the writer cannot accept.
    """
    update_fields = []
    skipped = []

    if zero_u_type:
        # Clear a stored position the way the import writer does, so the device stays valid.
        if device.position is not None:
            device.position = None
            update_fields.append("position")
        if device.face:
            device.face = None
            update_fields.append("face")
        if u_position not in ("", None):
            skipped.append("position")
        if face not in ("", None):
            skipped.append("face")
        return update_fields, skipped, None

    if u_position not in ("", None):
        position = source_position(u_position)
        if position is None:
            return update_fields, skipped, f"Cannot parse '{u_position}' as a finite number for u_position"
        device.position = position
        update_fields.append("position")

    if face not in ("", None):
        mapped = _FACE_MAP.get(str(face).strip().lower())
        if mapped is None:
            return update_fields, skipped, f"Unknown face value '{face}' — expected 'front' or 'rear'"
        device.face = mapped
        update_fields.append("face")

    return update_fields, skipped, None


class SyncPlacementView(_AjaxPermissionView):
    """Atomically sync rack + (optional) u_position + (optional) face for a device.

    All-or-nothing: if the rack lookup fails, nothing is saved.
    """

    permission_required = "dcim.change_device"

    def post(self, request):
        """Apply the previewed placement to its matched Device."""
        from django.http import JsonResponse

        intent, error, status = _placement_action_intent(request)
        if error:
            return JsonResponse({"ok": False, "error": error}, status=status)
        row = intent["row"]
        device = intent["device"]
        rack_name = intent["rack_name"]
        u_position = intent["u_position"]
        face = intent["face"]

        rack, err = _lookup_rack_for_device(device, rack_name)
        if err:
            return JsonResponse({"ok": False, "error": err})

        device.rack = rack
        # NetBox rejects a rack position on a zero-U device type, so sync the rack alone.
        zero_u_type = _zero_u_device_type(device)
        placement_fields, skipped, error = _set_rack_placement(device, u_position, face, zero_u_type)
        if error:
            return JsonResponse({"ok": False, "error": error})
        update_fields = ["rack", *placement_fields]

        try:
            _validate_device_placement(device)
        except ValidationError as exc:
            return JsonResponse({"ok": False, "error": f"Validation failed: {_placement_error_text(exc)}"}, status=400)
        except Exception:
            logger.exception("SyncPlacementView full_clean failed for device_id=%s", device.pk)
            return JsonResponse({"ok": False, "error": "An internal error occurred."}, status=500)

        try:
            device.save(update_fields=update_fields)
        except Exception:
            logger.exception("SyncPlacementView save failed for device_id=%s", device.pk)
            return JsonResponse({"ok": False, "error": "An internal error occurred."}, status=500)

        parts = [f"rack={rack.name}"]
        if "position" in update_fields and device.position is not None:
            parts.append(f"U{device.position}")
        if "face" in update_fields and device.face:
            parts.append(device.face)
        display = ", ".join(parts)
        if skipped:
            display += f" (0U device type {zero_u_type} takes no {' or '.join(skipped)})"
        if row is not None:
            mark_preview_dirty(request.session)
            return JsonResponse(
                pending_preview_payload(
                    row.row_number,
                    f"Updated placement to {display}.",
                )
            )
        return JsonResponse({"ok": True, "display": display})


# ---------------------------------------------------------------------------
# Save resolution (rerere)
# ---------------------------------------------------------------------------


def _device_name_already_claimed(effective_rows, row_number, new_name, target):
    """Return why this device name is unavailable at the import target, or None when it is free."""
    from dcim.models import Device

    other_names = {
        identity_text(device_name)
        for row in effective_rows
        if row.get("_row_number") != row_number and (device_name := effective_device_name(row))
    }
    if identity_text(new_name) in other_names:
        return f"Device name '{new_name}' is already used by another source row."
    tenant = target["tenant"]
    tenant_filter = {"tenant": tenant} if tenant is not None else {"tenant__isnull": True}
    if Device.objects.filter(site=target["site"], name__iexact=new_name, **tenant_filter).exists():
        return f"Device name '{new_name}' already exists at the active import site."
    return None


class ResolveDuplicateNameView(PermissionRequiredMixin, View):
    """Save a unique device name for one duplicate source row."""

    permission_required = "netbox_data_import.change_importprofile"

    def post(self, request):
        """Validate and persist the replacement device name."""
        decision, refused = _preview_row_decision(request)
        if refused is not None:
            return refused
        profile = decision.profile
        ctx_data = decision.ctx_data
        rows = decision.rows
        row_number = decision.row_number
        source_id = decision.source_id
        next_url = decision.next_url

        new_name = request.POST.get("new_name", "").strip()
        if not new_name or len(new_name) > 64:
            messages.error(request, "The device name must contain 1 to 64 characters.")
            return _name_resolution_response(request, next_url)

        resolution_values = {
            "original_value": source_text(decision.source_row.get("device_name")),
            "resolved_fields": {"device_name": new_name},
        }
        refusal = None
        try:
            # Serialize against an executing import, which holds the same profile row.
            with locked_profile_policy(profile.pk):
                # Read the target and the claims under the lock: a name saved between the check and
                # the write would otherwise let two source rows resolve to the same device name.
                target = _resolved_import_target(ctx_data, request.user)
                if target is None:
                    refusal = "The saved import target is no longer available. Start a new preview."
                else:
                    refusal = _device_name_already_claimed(rows, row_number, new_name, target)
                if refusal is None:
                    save_permission_scoped_object(
                        request.user,
                        SourceResolution,
                        {"profile": profile, "source_id": source_id, "source_column": "device_name"},
                        resolution_values,
                    )
        except ImportProfile.DoesNotExist:
            messages.error(request, "The import profile is no longer available.")
            return _name_resolution_response(request, next_url)
        except ObjectPermissionDenied:
            messages.error(request, "Permission denied: cannot create or change this saved name.")
            return _name_resolution_response(request, next_url)
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
            return _name_resolution_response(request, next_url)
        except IntegrityError:
            messages.error(request, "The saved name changed while this request was being processed. Try again.")
            return _name_resolution_response(request, next_url)

        if refusal is not None:
            messages.error(request, refusal)
            return _name_resolution_response(request, next_url)

        messages.success(request, f"Source '{source_id}' will use device name '{new_name}'.")
        return _name_resolution_response(request, next_url)


def _duplicate_serial_shown(preview_rows, row_number) -> str:
    """Return the serial the engine calls a duplicate on this source row, or an empty string."""
    for item in preview_rows:
        if (
            item.row_number == row_number
            and item.object_type == "device"
            and item.extra_data.get("identity_conflict") == "duplicate_serial"
        ):
            return source_text(item.extra_data.get("duplicate_serial"))
    return ""


class IgnoreDuplicateSerialView(PermissionRequiredMixin, View):
    """Drop the serial from one source row so the rows sharing it stop colliding."""

    permission_required = "netbox_data_import.change_importprofile"

    def post(self, request):
        """Persist an empty serial for the row the operator gives it up on."""
        decision, refused = _preview_row_decision(request)
        if refused is not None:
            return refused
        profile = decision.profile
        ctx_data = decision.ctx_data
        rows = decision.rows
        row_number = decision.row_number
        source_id = decision.source_id
        next_url = decision.next_url

        preview = load_cached_preview(request)
        # The action settles the collision the operator was shown, on the serial it named.
        shown_serial = _duplicate_serial_shown(preview[1].units, row_number) if preview is not None else ""
        if not shown_serial:
            messages.error(request, "This row shows no duplicate serial in the current preview.")
            return _name_resolution_response(request, next_url)

        original_serial = source_text(decision.source_row.get("serial"))
        if not original_serial:
            messages.error(request, "This row carries no serial to give up.")
            return _name_resolution_response(request, next_url)

        refusal = None
        try:
            # Serialize against an executing import, which holds the same profile row.
            with locked_profile_policy(profile.pk):
                held_since = time.monotonic()
                document = SourceDocument.objects.filter(
                    pk=ctx_data.get("source_document_id"),
                    profile=profile,
                ).first()
                if document is None:
                    refusal = "The stored source is no longer available. Upload it again."
                else:
                    current = ReviewWorkspace(
                        ImportEngine.plan(
                            profile,
                            document,
                            request.user,
                            {
                                "site_id": ctx_data.get("site_id"),
                                "location_id": ctx_data.get("location_id"),
                                "tenant_id": ctx_data.get("tenant_id"),
                            },
                        )
                    )
                    if _duplicate_serial_shown(current.units, row_number) != shown_serial:
                        refusal = f"No other row this import creates still claims serial '{shown_serial}'."
                    else:
                        save_permission_scoped_object(
                            request.user,
                            SourceResolution,
                            {"profile": profile, "source_id": source_id, "source_column": "serial"},
                            {"original_value": original_serial, "resolved_fields": {"serial": ""}},
                        )
                # The dry run costs more as the file grows, and the import worker waits behind it.
                logger.info(
                    "IgnoreDuplicateSerialView: held the profile policy lock for %.2fs over %d source rows.",
                    time.monotonic() - held_since,
                    len(rows),
                )
        except ImportProfile.DoesNotExist:
            messages.error(request, "The import profile is no longer available.")
            return _name_resolution_response(request, next_url)
        except ObjectPermissionDenied:
            messages.error(request, "Permission denied: cannot create or change this saved serial.")
            return _name_resolution_response(request, next_url)
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
            return _name_resolution_response(request, next_url)
        except IntegrityError:
            messages.error(request, "The saved serial changed while this request was being processed. Try again.")
            return _name_resolution_response(request, next_url)

        if refusal is not None:
            messages.error(request, refusal)
            return _name_resolution_response(request, next_url)

        messages.success(request, f"Source '{source_id}' will import without serial '{shown_serial}'.")
        return _name_resolution_response(request, next_url)


class SaveResolutionView(_AjaxPermissionView):
    """Save a manual field resolution for rerere replay."""

    permission_required = "netbox_data_import.change_importprofile"

    def post(self, request):
        """Persist a manual field resolution for rerere replay."""
        import json

        profile_id = _parse_posted_profile_id(request)
        source_id = request.POST.get("source_id")
        source_column = request.POST.get("source_column")
        original_value = request.POST.get("original_value")
        resolved_fields_json = request.POST.get("resolved_fields", "{}")
        next_url = _safe_next_url(request, "plugins:netbox_data_import:import_preview")
        if profile_id is None:
            return _preview_action_error(request, next_url, "A valid import profile is required.", status=400)

        try:
            resolved_fields = json.loads(resolved_fields_json)
        except (json.JSONDecodeError, TypeError):
            resolved_fields = {}
        if not isinstance(resolved_fields, Mapping):
            return _preview_action_error(
                request,
                next_url,
                "Resolved fields must be a JSON object.",
                status=400,
            )

        if profile_id and source_id and source_column:
            profile = get_object_or_404(
                ImportProfile.objects.restrict(request.user, "change"),
                pk=profile_id,
            )
            stale_reason = _stale_preview_reason(request)
            if stale_reason is not None:
                return _preview_action_error(request, next_url, stale_reason, status=409)

            contact_context = None
            candidates = {}
            if source_column == "candidate:contact":
                try:
                    validate_registered_adapter(profile)
                    candidates, source_row, result_row = _contact_candidate_context(request, profile.pk, source_id)
                    validate_contact_candidate_resolution(
                        resolved_fields,
                        profile.adapter_settings.primary_contact_lookup_field,
                        candidates,
                    )
                except ValidationError as exc:
                    return _preview_action_error(request, next_url, "; ".join(exc.messages), status=400)
                original_value = json.dumps(candidates, sort_keys=True)
                contact_context = (source_row, result_row)
            try:
                validate_source_resolution_fields(profile, source_column, resolved_fields)
                # Serialize against an executing import, which holds the same profile row.
                with locked_profile_policy(profile.pk):
                    decision = _persist_contact_decision(
                        profile, resolved_fields, candidates, contact_context, request.user
                    )
                    resolved_fields = decision.resolved_fields
                    save_permission_scoped_object(
                        request.user,
                        SourceResolution,
                        {"profile": profile, "source_id": source_id, "source_column": source_column},
                        {
                            "original_value": original_value or "",
                            "resolved_fields": resolved_fields,
                        },
                    )
            except IntegrityError:
                return _preview_action_error(
                    request,
                    next_url,
                    "The resolution changed while this request was being processed. Try again.",
                )
            except ObjectPermissionDenied as exc:
                logger.warning("SaveResolutionView: write refused outside the caller's object scope: %s", exc)
                return _preview_action_error(
                    request,
                    next_url,
                    "Permission denied: this action is outside your NetBox object permissions.",
                    status=403,
                )
            except ValidationError as exc:
                return _preview_action_error(request, next_url, "; ".join(exc.messages), status=400)
            saved_message, contact_detail = _saved_resolution_report(decision.write, decision.note)
            # The rendered row keeps the action it had before this decision, so the page must
            # ask for a recalculation whichever path saved it.
            mark_preview_dirty(request.session)
            if _wants_json(request):
                row_number = contact_context[1].row_number if contact_context else None
                # The page keeps its own copy of the decision, so it is given the saved one.
                resolution = {
                    "original_value": original_value or "",
                    "resolved_fields": resolved_fields,
                    "contact": decision.contact,
                }
                return JsonResponse(
                    pending_preview_payload(row_number, saved_message, contact_detail, resolution=resolution)
                )
            messages.success(request, saved_message)
            return redirect(next_url)
        return _preview_action_error(request, next_url, "A source row and column are required.", status=400)


# ---------------------------------------------------------------------------
# Device type analysis view
# ---------------------------------------------------------------------------


class DeviceTypeAnalysisView(PermissionRequiredMixin, View):
    """Show all unique (make, model) pairs across import jobs and profiles.

    Highlights which ones have explicit DeviceTypeMapping vs auto-slugified.
    """

    permission_required = "netbox_data_import.view_importprofile"

    def get(self, request, profile_pk=None):
        """Render the device type analysis page for the given profile."""
        profile = get_object_or_404(ImportProfile, pk=profile_pk) if profile_pk else None
        profiles = ImportProfile.objects.all()

        # Build analysis from DeviceTypeMapping + auto-slugify check
        if profile:
            dt_mappings = DeviceTypeMapping.objects.filter(profile=profile)
        else:
            dt_mappings = DeviceTypeMapping.objects.select_related("profile").all()

        # Collect entries: explicit mappings
        entries = []
        for dtm in dt_mappings:
            entries.append(
                {
                    "profile": dtm.profile,
                    "source_make": dtm.source_make,
                    "source_model": dtm.source_model,
                    "manufacturer_slug": dtm.netbox_manufacturer_slug,
                    "device_type_slug": dtm.netbox_device_type_slug,
                    "mapping_type": "explicit",
                    "mapping_pk": dtm.pk,
                }
            )

        # Check which mapped device types exist in NetBox
        from dcim.models import DeviceType

        for entry in entries:
            entry["exists_in_netbox"] = DeviceType.objects.filter(
                manufacturer__slug=entry["manufacturer_slug"],
                slug=entry["device_type_slug"],
            ).exists()

        return render(
            request,
            "netbox_data_import/analysis.html",
            {
                "profile": profile,
                "profiles": profiles,
                "entries": entries,
            },
        )


# ---------------------------------------------------------------------------
# Bulk YAML import for mappings
# ---------------------------------------------------------------------------


class BulkYamlImportView(PermissionRequiredMixin, View):
    """Accept a YAML file and bulk-create ClassRoleMappings or DeviceTypeMappings for a profile.

    Useful for bootstrapping from contrib/ definition files.
    """

    permission_required = "netbox_data_import.change_importprofile"

    def get(self, request, profile_pk):
        """Render the bulk YAML import form."""
        profile = get_object_or_404(ImportProfile, pk=profile_pk)
        return render(request, "netbox_data_import/bulk_yaml_import.html", {"profile": profile})

    def _import_class_role_rows(self, data, profile, errors):
        """Import a list of class-role mapping items; return (created, skipped)."""
        created = skipped = 0
        for item in data:
            try:
                rack_type = None
                rack_type_present = "rack_type" in item
                rack_type_slug = item.get("rack_type") if rack_type_present else None
                if rack_type_slug:
                    from dcim.models import RackType

                    try:
                        rack_type = RackType.objects.get(slug=rack_type_slug)
                    except RackType.DoesNotExist:
                        errors.append(
                            f"RackType with slug '{rack_type_slug}' not found for source_class '{item.get('source_class')}'"
                        )
                        continue

                defaults = {
                    "creates_rack": item.get("creates_rack", False),
                    "role_slug": item.get("role_slug", ""),
                    "ignore": item.get("ignore", False),
                }
                if rack_type_present:
                    defaults["rack_type"] = rack_type

                obj, was_created = ClassRoleMapping.objects.get_or_create(
                    profile=profile,
                    source_class=item["source_class"],
                    defaults=defaults,
                )
                if not was_created and rack_type_present:
                    obj.rack_type = rack_type
                    obj.save(update_fields=["rack_type"])
                if was_created:
                    created += 1
                else:
                    skipped += 1
            except (KeyError, ValueError) as exc:
                errors.append(str(exc))
            except Exception:
                logger.exception("BulkYamlImportView class_role row failed for profile_id=%s", profile.pk)
                errors.append("A row failed due to an unexpected error — see server logs.")
        return created, skipped

    def _import_device_type_rows(self, data, profile, errors):
        """Import a list of device-type mapping items; return (created, skipped)."""
        created = skipped = 0
        for item in data:
            try:
                _, was_created = DeviceTypeMapping.objects.get_or_create(
                    profile=profile,
                    source_make=item["source_make"],
                    source_model=item["source_model"],
                    defaults={
                        "netbox_manufacturer_slug": item["netbox_manufacturer_slug"],
                        "netbox_device_type_slug": item["netbox_device_type_slug"],
                    },
                )
                if was_created:
                    created += 1
                else:
                    skipped += 1
            except (KeyError, ValueError) as exc:
                errors.append(str(exc))
            except Exception:
                logger.exception("BulkYamlImportView device_type row failed for profile_id=%s", profile.pk)
                errors.append("A row failed due to an unexpected error — see server logs.")
        return created, skipped

    def post(self, request, profile_pk):
        """Parse the uploaded YAML file and create mappings in bulk."""
        profile = get_object_or_404(ImportProfile, pk=profile_pk)
        yaml_file = request.FILES.get("yaml_file")
        mapping_type = request.POST.get("mapping_type", "class_role")

        if not yaml_file:
            messages.error(request, "No YAML file uploaded.")
            return render(request, "netbox_data_import/bulk_yaml_import.html", {"profile": profile})

        try:
            import yaml

            data = yaml.safe_load(yaml_file.read())
        except yaml.YAMLError as exc:
            messages.error(request, f"Failed to parse YAML: {exc}")
            return render(request, "netbox_data_import/bulk_yaml_import.html", {"profile": profile})
        except Exception:
            logger.exception("BulkYamlImportView: failed to read uploaded file for profile_id=%s", profile_pk)
            messages.error(request, "Could not read the uploaded file.")
            return render(request, "netbox_data_import/bulk_yaml_import.html", {"profile": profile})

        if not isinstance(data, list):
            messages.error(request, "YAML must be a list of mapping objects.")
            return render(request, "netbox_data_import/bulk_yaml_import.html", {"profile": profile})

        errors = []
        if mapping_type == "class_role":
            created, skipped = self._import_class_role_rows(data, profile, errors)
        elif mapping_type == "device_type":
            created, skipped = self._import_device_type_rows(data, profile, errors)
        else:
            messages.error(request, f"Unknown mapping type '{mapping_type}'.")
            return redirect(profile.get_absolute_url())

        if errors:
            messages.warning(
                request, f"Created {created}, skipped {skipped}, {len(errors)} errors: {'; '.join(errors[:3])}"
            )
        else:
            messages.success(request, f"Bulk import complete: {created} created, {skipped} already existed.")
        return redirect(profile.get_absolute_url())


# ---------------------------------------------------------------------------
# Profile YAML export / full-profile YAML import
# ---------------------------------------------------------------------------


class ExportProfileYamlView(PermissionRequiredMixin, View):
    """Download all profile configuration as a single YAML file."""

    permission_required = "netbox_data_import.change_importprofile"

    def get(self, request, pk):
        """Serialize the profile and all its mappings to YAML and return as a file download."""
        import yaml
        from django.http import HttpResponse

        profile = get_object_or_404(ImportProfile, pk=pk)

        data = {
            "profile": {
                "name": profile.name,
                "description": profile.description,
                "source_adapter": profile.source_adapter,
                "adapter_config": profile.adapter_config,
            },
            "column_mappings": [
                {"source_column": cm.source_column, "target_field": cm.target_field}
                for cm in profile.column_mappings.all()
            ],
            "class_role_mappings": [
                {
                    **{
                        k: v
                        for k, v in {
                            "source_class": m.source_class,
                            "creates_rack": m.creates_rack,
                            "role_slug": m.role_slug,
                            "ignore": m.ignore,
                        }.items()
                        if v != ""
                    },
                    "rack_type": m.rack_type.slug if m.rack_type_id else None,
                }
                for m in profile.class_role_mappings.select_related("rack_type").all()
            ],
            "device_type_mappings": [
                {
                    "source_make": m.source_make,
                    "source_model": m.source_model,
                    "netbox_manufacturer_slug": m.netbox_manufacturer_slug,
                    "netbox_device_type_slug": m.netbox_device_type_slug,
                }
                for m in profile.device_type_mappings.all()
            ],
            "manufacturer_mappings": [
                {
                    "source_make": m.source_make,
                    "netbox_manufacturer_slug": m.netbox_manufacturer_slug,
                }
                for m in profile.manufacturer_mappings.all()
            ],
            "column_transform_rules": [
                {
                    "source_column": r.source_column,
                    "pattern": r.pattern,
                    "group_1_target": r.group_1_target,
                    "group_2_target": r.group_2_target,
                }
                for r in profile.column_transform_rules.all()
            ],
        }

        yaml_str = yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False)
        safe_name = profile.name.lower().replace(" ", "_").replace("/", "-")
        filename = f"profile_{safe_name}.yaml"
        return HttpResponse(
            yaml_str,
            content_type="application/x-yaml",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )


class ImportProfileYamlView(PermissionRequiredMixin, View):
    """Import a full profile YAML (as exported by ExportProfileYamlView).

    If the profile already exists (by name), merges/updates its mappings.
    """

    permission_required = "netbox_data_import.change_importprofile"

    def get(self, request):
        """Render the profile YAML import form."""
        return render(request, "netbox_data_import/import_profile_yaml.html")

    def post(self, request):
        """Parse the uploaded YAML and create or update the profile and its mappings."""
        import yaml

        yaml_file = request.FILES.get("yaml_file")
        if not yaml_file:
            messages.error(request, "No YAML file uploaded.")
            return render(request, "netbox_data_import/import_profile_yaml.html")

        try:
            data = yaml.safe_load(yaml_file.read())
        except Exception as exc:
            messages.error(request, f"Failed to parse YAML: {exc}")
            return render(request, "netbox_data_import/import_profile_yaml.html")

        try:
            profile, stats = _apply_profile_yaml_data(data)
        except ValueError as exc:  # KeyError no longer escapes since _iter_yaml_section validates required_keys
            messages.error(request, str(exc))
            return render(request, "netbox_data_import/import_profile_yaml.html")

        summary = ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in stats.items())
        messages.success(request, f"Profile '{profile.name}' imported/updated. {summary}.")
        return redirect(profile.get_absolute_url())


# ---------------------------------------------------------------------------


class CheckDeviceNameView(PermissionRequiredMixin, View):
    """AJAX endpoint: check if a device with the given name exists in NetBox.

    Returns JSON: {"exists": bool, "url": str|null, "id": int|null}.
    """

    permission_required = "netbox_data_import.view_importprofile"

    def get(self, request):
        """Return JSON indicating whether a device with the given name exists."""
        from dcim.models import Device
        from django.http import JsonResponse

        if not request.user.has_perm("dcim.view_device"):  # pragma: no cover
            from django.http import HttpResponseForbidden

            return HttpResponseForbidden()

        name = request.GET.get("name", "").strip()
        if not name:
            return JsonResponse({"exists": False, "url": None, "id": None})

        try:
            device = Device.objects.get(name=name)
            return JsonResponse(
                {
                    "exists": True,
                    "url": request.build_absolute_uri(device.get_absolute_url()),
                    "id": device.pk,
                }
            )
        except Device.DoesNotExist:
            return JsonResponse({"exists": False, "url": None, "id": None})
        except Device.MultipleObjectsReturned:
            devices = Device.objects.filter(name=name)
            first = devices.first()
            return JsonResponse(
                {
                    "exists": True,
                    "url": request.build_absolute_uri(first.get_absolute_url()),
                    "id": first.pk,
                    "count": devices.count(),
                }
            )


# ---------------------------------------------------------------------------
# Source Resolutions list view (per profile)
# ---------------------------------------------------------------------------


class SourceResolutionListView(PermissionRequiredMixin, View):
    """List all saved name-split resolutions for a profile."""

    permission_required = "netbox_data_import.view_importprofile"

    def get(self, request, profile_pk):
        """Render the list of saved source resolutions for the given profile."""
        profile = get_object_or_404(ImportProfile, pk=profile_pk)
        resolutions = SourceResolution.objects.filter(profile=profile).order_by("source_id")
        return render(
            request,
            "netbox_data_import/source_resolution_list.html",
            {
                "profile": profile,
                "resolutions": resolutions,
            },
        )


class SourceResolutionDeleteView(_ProfileChildDeleteView):
    """Delete a saved source resolution."""

    queryset = SourceResolution.objects.all()

    def post(self, request, *args, **kwargs):
        """Serialize against an executing import, which holds the same profile row."""
        resolution = self.get_object(**kwargs)
        try:
            with locked_resolution_policy(resolution.pk):
                # atomic-exit-safe: locked-delete-committed
                return super().post(request, *args, **kwargs)
        except (SourceResolution.DoesNotExist, ImportProfile.DoesNotExist):
            # The row went away between the fetch and the lock, which is the 404 the fetch would give.
            raise Http404 from None


# ---------------------------------------------------------------------------
# Quick-resolve views (inline fixes from preview page)
# ---------------------------------------------------------------------------


class QuickCreateManufacturerView(_PermissionScopedWriteMixin, PermissionRequiredMixin, View):
    """Immediately create a Manufacturer in NetBox from the preview page."""

    permission_required = "netbox_data_import.change_importprofile"

    def post(self, request):
        """Create the manufacturer in NetBox and report the pending preview change."""
        from dcim.models import Manufacturer

        next_url = reverse("plugins:netbox_data_import:import_preview")
        profile_id = _parse_posted_profile_id(request)
        if profile_id is None:
            return _preview_action_error(
                request,
                next_url,
                "A valid import profile is required. Reload the preview and try again.",
                status=400,
            )
        get_object_or_404(ImportProfile.objects.restrict(request.user, "change"), pk=profile_id)
        stale_reason = _stale_preview_reason(request)
        if stale_reason is not None:
            return _preview_action_error(request, next_url, stale_reason, status=409)
        mfg_name = request.POST.get("mfg_name", "").strip()
        mfg_slug = request.POST.get("mfg_slug", "").strip()
        if not mfg_name or not mfg_slug:
            return _preview_action_error(request, next_url, "Manufacturer name and slug are required.", status=400)
        mfg = _get_or_init(Manufacturer, slug=mfg_slug)
        if mfg.pk is None:
            mfg.name = mfg_name
            try:
                _validate_model_instance(mfg, f"manufacturer '{mfg_name}'")
            except PreviewActionInvalid as exc:
                return _preview_action_error(request, next_url, str(exc), status=400)
        result = save_permission_scoped_object(
            request.user,
            Manufacturer,
            {"slug": mfg_slug},
            {"name": mfg_name},
            on_existing="keep",
        )
        mfg = result.instance
        created = result.created
        if created:
            saved_message = f"Manufacturer '{mfg.name}' created."
        else:
            saved_message = f"Manufacturer '{mfg.name}' already existed."
        return _saved_preview_action_response(request, next_url, saved_message)


class QuickResolveManufacturerView(_PermissionScopedWriteMixin, PermissionRequiredMixin, View):
    """Save a ManufacturerMapping (source make → NetBox manufacturer slug) from the preview page."""

    permission_required = "netbox_data_import.change_importprofile"

    def post(self, request):
        """Save the manufacturer mapping and report the pending preview change."""
        next_url = reverse("plugins:netbox_data_import:import_preview")
        profile_id = _parse_posted_profile_id(request)
        if profile_id is None:
            return _preview_action_error(
                request,
                next_url,
                "A valid import profile is required. Reload the preview and try again.",
                status=400,
            )
        profile = get_object_or_404(ImportProfile.objects.restrict(request.user, "change"), pk=profile_id)
        stale_reason = _stale_preview_reason(request)
        if stale_reason is not None:
            return _preview_action_error(request, next_url, stale_reason, status=409)
        source_make = " ".join(request.POST.get("source_make", "").split())
        netbox_mfg_slug = request.POST.get("netbox_mfg_slug", "").strip()
        if not source_make or not netbox_mfg_slug:
            return _preview_action_error(
                request,
                next_url,
                "Source make and NetBox manufacturer slug are required.",
                status=400,
            )
        mapping = _get_or_init(ManufacturerMapping, profile=profile, source_make=source_make)
        mapping.netbox_manufacturer_slug = netbox_mfg_slug
        try:
            _validate_model_instance(mapping, f"manufacturer mapping '{source_make}'")
        except PreviewActionInvalid as exc:
            return _preview_action_error(request, next_url, str(exc), status=400)
        result = save_permission_scoped_object(
            request.user,
            ManufacturerMapping,
            {"profile": profile, "source_make": source_make},
            {"netbox_manufacturer_slug": netbox_mfg_slug},
        )
        verb = "Created" if result.created else "Updated"
        return _saved_preview_action_response(
            request,
            next_url,
            f"{verb} manufacturer mapping: '{source_make}' → {netbox_mfg_slug}",
        )


class QuickResolveDeviceTypeView(_PermissionScopedWriteMixin, PermissionRequiredMixin, View):
    """Save a DeviceTypeMapping (source make/model → NetBox slugs) from the preview page.

    Optionally also creates the manufacturer and/or device type in NetBox right now.
    """

    permission_required = "netbox_data_import.change_importprofile"

    def post(self, request):
        """Save the device type mapping and report the pending preview change."""
        from dcim.models import DeviceType, Manufacturer
        from django.utils.text import slugify

        next_url = reverse("plugins:netbox_data_import:import_preview")
        profile_id = _parse_posted_profile_id(request)
        if profile_id is None:
            return _preview_action_error(
                request,
                next_url,
                "A valid import profile is required. Reload the preview and try again.",
                status=400,
            )
        profile = get_object_or_404(ImportProfile.objects.restrict(request.user, "change"), pk=profile_id)
        stale_reason = _stale_preview_reason(request)
        if stale_reason is not None:
            return _preview_action_error(request, next_url, stale_reason, status=409)
        source_make = " ".join(request.POST.get("source_make", "").split())
        source_model = " ".join(request.POST.get("source_model", "").split())
        netbox_mfg_slug = request.POST.get("netbox_mfg_slug", "").strip()
        netbox_dt_slug = request.POST.get("netbox_dt_slug", "").strip()
        action = request.POST.get("action", "map")  # "map" or "create_now"

        if not source_make or not source_model:
            return _preview_action_error(request, next_url, "Source make and model are required.", status=400)

        if not netbox_mfg_slug:
            netbox_mfg_slug = slugify(source_make)
        if not netbox_dt_slug:
            netbox_dt_slug = slugify(source_model)

        # Every name and slug below is posted directly, so validate each write before it happens.
        try:
            with transaction.atomic():
                mapping = _get_or_init(
                    DeviceTypeMapping,
                    profile=profile,
                    source_make=source_make,
                    source_model=source_model,
                )
                mapping.netbox_manufacturer_slug = netbox_mfg_slug
                mapping.netbox_device_type_slug = netbox_dt_slug
                _validate_model_instance(
                    mapping,
                    f"device type mapping '{source_make} / {source_model}'",
                )
                mapping_result = save_permission_scoped_object(
                    request.user,
                    DeviceTypeMapping,
                    {"profile": profile, "source_make": source_make, "source_model": source_model},
                    {
                        "netbox_manufacturer_slug": netbox_mfg_slug,
                        "netbox_device_type_slug": netbox_dt_slug,
                    },
                )
                created = mapping_result.created

                if action == "create_now":
                    mfg_candidate = _get_or_init(Manufacturer, slug=netbox_mfg_slug)
                    if mfg_candidate.pk is None:
                        mfg_candidate.name = source_make
                        _validate_model_instance(mfg_candidate, f"manufacturer '{source_make}'")
                    mfg = save_permission_scoped_object(
                        request.user,
                        Manufacturer,
                        {"slug": netbox_mfg_slug},
                        {"name": source_make},
                        on_existing="keep",
                    ).instance
                    dt_name = request.POST.get("netbox_dt_name", source_model).strip() or source_model
                    try:
                        u_height = max(1, int(request.POST.get("u_height", "1")))
                    except ValueError:
                        u_height = 1
                    device_type_candidate = _get_or_init(DeviceType, manufacturer=mfg, slug=netbox_dt_slug)
                    if device_type_candidate.pk is None:
                        device_type_candidate.model = dt_name
                        device_type_candidate.u_height = u_height
                        _validate_model_instance(device_type_candidate, f"device type '{dt_name}'")
                    save_permission_scoped_object(
                        request.user,
                        DeviceType,
                        {"manufacturer": mfg, "slug": netbox_dt_slug},
                        {"model": dt_name, "u_height": u_height},
                        on_existing="keep",
                    )
        except PreviewActionInvalid as exc:
            return _preview_action_error(request, next_url, str(exc), status=400)

        if action == "create_now":
            saved_message = f"Mapping saved and device type '{source_make} / {source_model}' created in NetBox."
        else:
            verb = "created" if created else "updated"
            saved_message = (
                f"DeviceType mapping {verb}: '{source_make} / {source_model}' → {netbox_mfg_slug}/{netbox_dt_slug}"
            )

        return _saved_preview_action_response(request, next_url, saved_message)


class QuickAddClassRoleMappingView(_PermissionScopedWriteMixin, PermissionRequiredMixin, View):
    """Quickly add a ClassRoleMapping (ignore / role) directly from an error row in preview."""

    permission_required = "netbox_data_import.change_importprofile"

    def post(self, request):
        """Save the class-to-role mapping and report the pending preview change."""
        from dcim.models import RackType

        next_url = reverse("plugins:netbox_data_import:import_preview")
        profile_id = _parse_posted_profile_id(request)
        if profile_id is None:
            return _preview_action_error(
                request,
                next_url,
                "A valid import profile is required. Reload the preview and try again.",
                status=400,
            )
        profile = get_object_or_404(ImportProfile.objects.restrict(request.user, "change"), pk=profile_id)
        stale_reason = _stale_preview_reason(request)
        if stale_reason is not None:
            return _preview_action_error(request, next_url, stale_reason, status=409)
        source_class = request.POST.get("source_class", "").strip()
        mapping_action = request.POST.get("mapping_action", "ignore")  # "ignore", "role", or "rack"
        role_slug = request.POST.get("role_slug", "").strip()
        creates_rack = mapping_action == "rack"
        rack_type_id = request.POST.get("rack_type_id", "").strip()

        rack_type = None
        if creates_rack and rack_type_id:
            try:
                rack_type = RackType.objects.get(pk=int(rack_type_id))
            except (RackType.DoesNotExist, ValueError, TypeError):
                return _preview_action_error(
                    request,
                    next_url,
                    f"Invalid rack type selected for class '{source_class}'. Please choose a valid rack type.",
                    status=400,
                )

        if not source_class:
            return _preview_action_error(request, next_url, "Source class is required.", status=400)

        _valid_actions = ("ignore", "role", "rack")
        if mapping_action not in _valid_actions:
            return _preview_action_error(
                request,
                next_url,
                f"Invalid mapping action '{mapping_action}'. Must be one of: {', '.join(_valid_actions)}.",
                status=400,
            )

        if mapping_action == "role" and not role_slug:
            return _preview_action_error(
                request,
                next_url,
                "A role slug is required when mapping action is 'role'.",
                status=400,
            )

        values = {
            "ignore": mapping_action == "ignore",
            "creates_rack": creates_rack,
            "rack_type": rack_type,
            "role_slug": role_slug if mapping_action == "role" else "",
        }
        mapping = _get_or_init(ClassRoleMapping, profile=profile, source_class=source_class)
        for field_name, value in values.items():
            setattr(mapping, field_name, value)
        try:
            _validate_model_instance(mapping, f"class role mapping '{source_class}'")
        except PreviewActionInvalid as exc:
            return _preview_action_error(request, next_url, str(exc), status=400)
        result = save_permission_scoped_object(
            request.user,
            ClassRoleMapping,
            {"profile": profile, "source_class": source_class},
            values,
        )
        verb = "Created" if result.created else "Updated"
        if mapping_action == "ignore":
            action_label = "ignore"
        elif mapping_action == "rack":
            rt_suffix = f" (type: {rack_type})" if rack_type else ""
            action_label = f"creates rack{rt_suffix}"
        else:
            action_label = f"role '{role_slug}'"
        return _saved_preview_action_response(
            request,
            next_url,
            f"{verb} mapping: class '{source_class}' → {action_label}",
        )


class QuickAddColumnMappingView(_PermissionScopedWriteMixin, PermissionRequiredMixin, View):
    """Quickly map an unmapped source column to a NetBox target field from the preview panel."""

    permission_required = "netbox_data_import.change_importprofile"

    def post(self, request):
        """Save the column mapping and report the pending preview change."""
        next_url = reverse("plugins:netbox_data_import:import_preview")
        profile_id = _parse_posted_profile_id(request)
        if profile_id is None:
            return _preview_action_error(
                request,
                next_url,
                "A valid import profile is required. Reload the preview and try again.",
                status=400,
            )
        profile = get_object_or_404(ImportProfile.objects.restrict(request.user, "change"), pk=profile_id)
        stale_reason = _stale_preview_reason(request)
        if stale_reason is not None:
            return _preview_action_error(request, next_url, stale_reason, status=409)
        source_column = request.POST.get("source_column", "").strip()
        target_field = request.POST.get("target_field", "").strip()

        if not source_column or not CATALOG.is_valid(target_field, output_kinds=profile.output_kinds):
            return _preview_action_error(
                request,
                next_url,
                "Valid source column and target field are required.",
                status=400,
            )

        # The catalog accepts any non-empty name after a family prefix, so it cannot bound length.
        # Validate before the displaced row is deleted: an invalid write must strand nothing.
        try:
            _validate_model_instance(
                ColumnMapping(profile=profile, source_column=source_column, target_field=target_field),
                f"column mapping '{source_column}' -> {target_field}",
            )
        except PreviewActionInvalid as exc:
            return _preview_action_error(request, next_url, str(exc), status=400)

        if target_field.startswith(CANDIDATE_TARGET_PREFIX):
            result = save_permission_scoped_object(
                request.user,
                ColumnMapping,
                {"profile": profile, "source_column": source_column, "target_field": target_field},
                {},
                on_existing="keep",
            )
            verb = "Created" if result.created else "Kept"
            saved_message = f"{verb} candidate mapping: '{source_column}' → {target_field}"
        else:
            # A quick direct mapping replaces the source column that supplied the target before it.
            with transaction.atomic():
                displaced = ColumnMapping.objects.filter(profile=profile, target_field=target_field).exclude(
                    source_column=source_column
                )
                displaced_source = displaced.values_list("source_column", flat=True).first()
                delete_permission_scoped_objects(request.user, displaced)
                result = save_permission_scoped_object(
                    request.user,
                    ColumnMapping,
                    {"profile": profile, "source_column": source_column, "target_field": target_field},
                    {},
                    on_existing="keep",
                )
            if displaced_source:
                saved_message = (
                    f"Reassigned: '{source_column}' → {target_field} (previously mapped from '{displaced_source}')"
                )
            else:
                verb = "Created" if result.created else "Kept"
                saved_message = f"{verb} mapping: '{source_column}' → {target_field}"

        return _saved_preview_action_response(request, next_url, saved_message)


class MatchExistingDeviceView(PermissionRequiredMixin, View):
    """Link a source row to an existing NetBox device (by device ID).

    Saves a DeviceExistingMatch; on next preview re-run the row shows action='update'.
    """

    permission_required = (
        "netbox_data_import.change_importprofile",
        "dcim.view_device",
    )

    def post(self, request):
        """Save the device match and redirect back to preview."""
        from dcim.models import Device

        profile_id = _parse_posted_profile_id(request)
        if profile_id is None:
            messages.error(request, "A valid import profile is required.")
            return redirect(reverse("plugins:netbox_data_import:import_preview"))
        profile = get_object_or_404(ImportProfile.objects.restrict(request.user, "change"), pk=profile_id)
        source_id = source_text(request.POST.get("source_id"))
        netbox_device_id = request.POST.get("netbox_device_id", "").strip()

        if not source_id or not netbox_device_id:
            messages.error(request, "source_id and netbox_device_id are required.")
            return redirect(reverse("plugins:netbox_data_import:import_preview"))

        preview = load_cached_preview(request)
        if preview is None or preview[0].pk != profile.pk:
            messages.error(request, "The selected profile is not the active import profile.")
            return redirect(reverse("plugins:netbox_data_import:import_preview"))
        workspace = preview[1]
        ctx_data = request.session.get("import_context") or {}
        rows = workspace.source_rows
        source_rows = [row for row in rows if source_text(row.get("source_id")) == source_id]
        if len(source_rows) != 1:
            messages.error(request, "The source ID must identify exactly one row in the active import.")
            return redirect(reverse("plugins:netbox_data_import:import_preview"))

        try:
            device = Device.objects.restrict(request.user, "view").get(pk=int(netbox_device_id))
        except (Device.DoesNotExist, ValueError):
            messages.error(request, f"Device #{netbox_device_id} not found.")
            return redirect(reverse("plugins:netbox_data_import:import_preview"))

        if device.site_id != ctx_data.get("site_id"):
            messages.error(request, "The selected device is outside the active import site.")
            return redirect(reverse("plugins:netbox_data_import:import_preview"))
        conflicting_match = (
            profile.device_matches.filter(netbox_device_id=device.pk).exclude(source_id=source_id).first()
        )
        if conflicting_match:
            messages.error(
                request,
                f"Device '{device.name}' is already linked to source '{conflicting_match.source_id}'.",
            )
            return redirect(reverse("plugins:netbox_data_import:import_preview"))

        binding_values = {
            "netbox_device_id": device.pk,
            "device_name": device.name,
            "source_asset_tag": source_text(source_rows[0].get("asset_tag"))[:50],
        }
        try:
            save_permission_scoped_object(
                request.user,
                DeviceExistingMatch,
                {"profile": profile, "source_id": source_id},
                binding_values,
            )
        except ObjectPermissionDenied:
            messages.error(request, "Permission denied: cannot create or change this device link.")
            return redirect(reverse("plugins:netbox_data_import:import_preview"))
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
            return redirect(reverse("plugins:netbox_data_import:import_preview"))
        except IntegrityError:
            messages.error(request, "The device link changed while this request was being processed. Try again.")
            return redirect(reverse("plugins:netbox_data_import:import_preview"))

        messages.success(request, f"Source '{source_id}' linked to existing device '{device.name}'.")
        return redirect(reverse("plugins:netbox_data_import:import_preview"))


def _device_name_filter(q: str):
    """Build a Django Q filter for device name search.

    Exact icontains is tried first; when the query contains separators (-, _, .)
    individual tokens (≥3 chars) are OR-ed in so that e.g. "EXAMPLE-SITE03-SW3"
    matches "edge-site03-switch03.lab.example.invalid" via the "SITE03" token.
    """
    import re as _re

    from django.db.models import Q as _Q

    base = _Q(name__icontains=q)
    tokens = [t for t in _re.split(r"[-_.\s]+", q) if len(t) >= 3]
    if len(tokens) > 1:
        token_q = _Q()
        for tok in tokens:
            token_q |= _Q(name__icontains=tok)
        return base | token_q
    return base


class SearchNetBoxObjectsView(_AjaxPermissionView):
    """AJAX search endpoint for NetBox objects used in preview quick-fix modals.

    GET params: type (manufacturer|device_type|device|role|rack_type), q (search string).
    Returns JSON list of {id, name, slug, url} dicts.
    """

    permission_required = "netbox_data_import.view_importprofile"

    def get(self, request):
        """Return a JSON list of matching NetBox objects for the given type and query."""
        from dcim.models import DeviceRole, DeviceType, Manufacturer, RackType
        from django.http import JsonResponse

        obj_type = request.GET.get("type", "device")
        q = request.GET.get("q", "").strip()
        limit = 20

        _perm_map = {
            "manufacturer": "dcim.view_manufacturer",
            "device_type": "dcim.view_devicetype",
            "device": "dcim.view_device",
            "role": "dcim.view_devicerole",
            "rack_type": "dcim.view_racktype",
        }
        required_perm = _perm_map.get(obj_type)
        if required_perm and not request.user.has_perm(required_perm):  # pragma: no cover
            return JsonResponse({"results": [], "error": "permission_denied"}, status=403)

        if not q:
            return JsonResponse({"results": []})

        results = []
        if obj_type == "manufacturer":
            for mfg in Manufacturer.objects.filter(name__icontains=q)[:limit]:
                results.append(
                    {
                        "id": mfg.pk,
                        "name": mfg.name,
                        "slug": mfg.slug,
                        "url": request.build_absolute_uri(mfg.get_absolute_url()),
                    }
                )
        elif obj_type == "device_type":
            mfg_filter = request.GET.get("mfg_slug", "")
            qs = DeviceType.objects.select_related("manufacturer")
            if mfg_filter:
                qs = qs.filter(manufacturer__slug=mfg_filter)
            for dt in qs.filter(model__icontains=q)[:limit]:
                results.append(
                    {
                        "id": dt.pk,
                        "name": f"{dt.manufacturer.name} / {dt.model}",
                        "slug": dt.slug,
                        "mfg_slug": dt.manufacturer.slug,
                        "url": request.build_absolute_uri(dt.get_absolute_url()),
                    }
                )
        elif obj_type == "device":
            self._search_devices(request, q, limit, results)
        elif obj_type == "role":
            for role in DeviceRole.objects.filter(name__icontains=q)[:limit]:
                results.append(
                    {
                        "id": role.pk,
                        "name": role.name,
                        "slug": role.slug,
                        "url": request.build_absolute_uri(role.get_absolute_url()),
                    }
                )
        elif obj_type == "rack_type":
            from django.db.models import Q

            qs = RackType.objects.select_related("manufacturer").filter(
                Q(model__icontains=q) | Q(manufacturer__name__icontains=q) | Q(slug__icontains=q)
            )[:limit]
            for rt in qs:
                results.append(
                    {
                        "id": rt.pk,
                        "name": f"{rt.manufacturer.name} / {rt.model}" if rt.manufacturer else rt.model,
                        "slug": rt.slug,
                        "url": request.build_absolute_uri(rt.get_absolute_url()),
                    }
                )

        return JsonResponse({"results": results})

    def _search_devices(self, request, q, limit, results):
        """Two-phase device search: full-string matches first, then token matches.

        This prevents a relevant exact-substring match (e.g. "example-zone03d-rc1")
        from being pushed off the result list by noisy short tokens like "rc1"
        or "prod" that match many devices.
        """
        from dcim.models import Device

        visible_devices = Device.objects.restrict(request.user, "view")
        base_qs = visible_devices.filter(name__icontains=q).distinct().select_related("site").order_by("name")
        seen_ids = set()
        for dev in base_qs[:limit]:
            seen_ids.add(dev.pk)
            results.append(
                {
                    "id": dev.pk,
                    "name": dev.name,
                    "serial": dev.serial or None,
                    "site": dev.site.name if dev.site else "",
                    "url": request.build_absolute_uri(dev.get_absolute_url()),
                }
            )
        if len(results) >= limit:
            return
        token_qs = (
            visible_devices.filter(_device_name_filter(q))
            .exclude(pk__in=seen_ids)
            .distinct()
            .select_related("site")
            .order_by("name")
        )
        for dev in token_qs[: limit - len(results)]:
            results.append(
                {
                    "id": dev.pk,
                    "name": dev.name,
                    "serial": dev.serial or None,
                    "site": dev.site.name if dev.site else "",
                    "url": request.build_absolute_uri(dev.get_absolute_url()),
                }
            )


class QuickCreateDeviceRoleView(_PermissionScopedWriteMixin, _AjaxPermissionView):
    """AJAX endpoint: create a new DeviceRole and return its details as JSON.

    Used by the Configure Class modal so operators can create missing roles
    without leaving the import preview page.
    """

    permission_required = "netbox_data_import.change_importprofile"

    def post(self, request):
        """Create the DeviceRole and return JSON {id, name, slug}."""
        from dcim.models import DeviceRole
        from django.http import JsonResponse

        profile_id = _parse_posted_profile_id(request)
        if profile_id is None:
            return JsonResponse({"error": "A valid import profile is required."}, status=400)
        get_object_or_404(ImportProfile.objects.restrict(request.user, "change"), pk=profile_id)

        name = request.POST.get("name", "").strip()
        slug = request.POST.get("slug", "").strip()
        color = request.POST.get("color", "9e9e9e").strip() or "9e9e9e"

        if not name or not slug:
            return JsonResponse({"error": "Role name and slug are required."}, status=400)

        import re

        if not re.match(r"^[-a-z0-9_]+$", slug):
            return JsonResponse(
                {"error": "Slug may only contain lowercase letters, numbers, hyphens, and underscores."}, status=400
            )

        try:
            role = _get_or_init(DeviceRole, slug=slug)
            if role.pk is None:
                role.name = name
                role.color = color
                _validate_model_instance(role, f"device role '{name}'")
            result = save_permission_scoped_object(
                request.user,
                DeviceRole,
                {"slug": slug},
                {"name": name, "color": color},
                on_existing="keep",
            )
            role = result.instance
            created = result.created
        except IntegrityError:
            logger.exception("QuickCreateDeviceRoleView: integrity error creating role slug=%s", slug)
            return JsonResponse({"error": "A device role with that slug already exists."}, status=400)
        except (ValueError, ValidationError):
            logger.exception("QuickCreateDeviceRoleView: validation error creating role slug=%s", slug)
            return JsonResponse({"error": "Invalid role data."}, status=400)
        except DatabaseError:
            logger.exception("QuickCreateDeviceRoleView: database error creating role slug=%s", slug)
            return JsonResponse({"error": "An internal error occurred."}, status=500)

        return JsonResponse(
            {
                "id": role.pk,
                "name": role.name,
                "slug": role.slug,
                "created": created,
            }
        )


class AutoMatchDevicesView(PermissionRequiredMixin, View):
    """Run the Review Workspace device auto-match command."""

    permission_required = (
        "netbox_data_import.change_importprofile",
        "netbox_data_import.add_deviceexistingmatch",
        "dcim.view_device",
    )

    def post(self, request):
        """Run auto-matching and redirect back to preview with a summary message."""
        profile_id = _parse_posted_profile_id(request)
        if profile_id is None:
            messages.error(request, "A valid import profile is required.")
            return redirect(reverse("plugins:netbox_data_import:import_preview"))
        preview = load_cached_preview(request)
        if preview is None or preview[0].pk != profile_id:
            messages.error(request, "The selected profile is not the active import profile.")
            return redirect(reverse("plugins:netbox_data_import:import_preview"))
        profile, workspace = preview
        ctx_data = request.session.get("import_context") or {}
        target = _resolved_import_target(ctx_data, request.user)
        if target is None:
            messages.error(request, "The saved import target is no longer available. Start a new preview.")
            return redirect(reverse("plugins:netbox_data_import:import_preview"))
        summary = workspace.auto_match_devices(profile, request.user, target)
        messages.success(request, summary.message())
        return redirect(reverse("plugins:netbox_data_import:import_preview"))


def _refused_row_write_response(exc, row_number):
    """Return the answer one refused row write gives the operator.

    The worker reports the same failures, so both read the message from one place.
    """
    if isinstance(exc, DatabaseError):
        logger.exception("SyncSingleRowView: database error for row_number=%s", row_number)
    return JsonResponse({"ok": False, "error": operator_failure_message(exc)}, status=400)


class SyncSingleRowView(_AjaxPermissionView):
    """AJAX endpoint: execute a single row from the current import session.

    POST body: row_number=<int>
    Returns the deferred preview-action JSON envelope.
    """

    permission_required = "netbox_data_import.change_importprofile"

    def post(self, request):
        """Execute one selected Synchronization Unit and return JSON."""
        ctx_data = request.session.get("import_context")
        plan_data = request.session.get(PREVIEW_PLAN_SESSION_KEY)
        if not isinstance(ctx_data, dict) or not isinstance(plan_data, dict):
            return JsonResponse({"ok": False, "error": "No import in progress"}, status=400)
        if stale_reason := _stale_preview_reason(request):
            return JsonResponse({"ok": False, "error": stale_reason}, status=409)
        if request.session.get(PREVIEW_DIRTY_SESSION_KEY) is True:
            return JsonResponse(
                {"ok": False, "error": "Recalculate the preview before synchronizing a row."},
                status=409,
            )

        raw_row_number = request.POST.get("row_number")
        if raw_row_number is None:
            return JsonResponse({"ok": False, "error": "row_number is required"}, status=400)
        try:
            row_number = int(raw_row_number)
        except (TypeError, ValueError):
            return JsonResponse({"ok": False, "error": "Invalid row number"}, status=400)

        profile = ImportProfile.objects.restrict(request.user, "change").filter(pk=ctx_data.get("profile_id")).first()
        if not profile:
            return JsonResponse({"ok": False, "error": "Import profile not found"}, status=400)
        try:
            validate_registered_adapter(profile)
        except ValidationError as exc:
            return JsonResponse({"ok": False, "error": "; ".join(exc.messages)}, status=400)

        try:
            accepted = ImportPlan.from_dict(plan_data)
        except PlanError as exc:
            return JsonResponse({"ok": False, "error": str(exc)}, status=409)
        workspace = ReviewWorkspace(accepted)
        preview_unit = next(
            (
                unit
                for unit in workspace.units
                if unit.row_number == row_number and unit.object_type in {"device", "rack"}
            ),
            None,
        )
        if preview_unit is None:
            return JsonResponse({"ok": False, "error": "Row not found in current preview data"}, status=400)
        if preview_unit.action != "create":
            return JsonResponse(
                {"ok": False, "error": "Only 'create' rows can be synced individually"},
                status=400,
            )

        document = SourceDocument.objects.filter(pk=ctx_data.get("source_document_id"), profile=profile).first()
        if document is None:
            return JsonResponse(
                {"ok": False, "error": "The stored source is no longer available. Upload it again."},
                status=400,
            )

        try:
            ImportEngine.execute(
                profile,
                document,
                plan_data,
                [preview_unit.identity],
                uuid.uuid4().hex,
                request.user,
            )
        except (
            PlanError,
            PlanningTargetUnavailable,
            PreconditionFailed,
            SelectionError,
            StalePlan,
            StaleSourceDocument,
        ) as exc:
            return JsonResponse({"ok": False, "error": str(exc)}, status=409)
        except (DatabaseError, ObjectPermissionDenied, ValidationError) as exc:
            return _refused_row_write_response(exc, row_number)
        except Exception:
            logger.exception("SyncSingleRowView: unexpected error for row_number=%s", row_number)
            return JsonResponse(
                {"ok": False, "error": "An unexpected error occurred. See server logs."},
                status=500,
            )

        mark_preview_dirty(request.session)
        written = f"{preview_unit.object_type.capitalize()} '{preview_unit.name}' was created in NetBox."
        return JsonResponse(pending_preview_payload(row_number, "Synchronized.", written))


class UnlinkDeviceView(_AjaxPermissionView):
    """Remove a DeviceExistingMatch (unlink a manually-linked device)."""

    permission_required = "netbox_data_import.delete_deviceexistingmatch"

    def post(self, request):
        """Delete the DeviceExistingMatch and redirect back to preview."""
        profile_id = request.POST.get("profile_id", "").strip()
        source_id = request.POST.get("source_id", "").strip()
        next_url = _safe_next_url(request, "plugins:netbox_data_import:import_preview")

        if profile_id and source_id:
            profile = get_object_or_404(
                ImportProfile.objects.restrict(request.user, "change"),
                pk=profile_id,
            )
            with transaction.atomic():
                binding = (
                    DeviceExistingMatch.objects.select_for_update().filter(profile=profile, source_id=source_id).first()
                )
                dependent_reviews = list(
                    IgnoredFieldDifference.objects.select_for_update().filter(
                        profile=profile,
                        source_id=source_id,
                    )
                )
                if binding is not None and not request.user.has_perm(
                    "netbox_data_import.delete_deviceexistingmatch",
                    binding,
                ):
                    messages.error(request, "Permission denied: cannot delete this device link.")
                elif any(
                    not request.user.has_perm("netbox_data_import.delete_ignoredfielddifference", review)
                    for review in dependent_reviews
                ):
                    messages.error(request, "Permission denied: cannot remove the dependent field reviews.")
                else:
                    if dependent_reviews:
                        IgnoredFieldDifference.objects.filter(
                            pk__in=[review.pk for review in dependent_reviews]
                        ).delete()
                    if binding is not None:
                        binding.delete()
                    if dependent_reviews:
                        messages.success(
                            request,
                            f"Unlinked source '{source_id}' and removed {len(dependent_reviews)} field review(s).",
                        )
                    elif binding is not None:
                        messages.success(request, f"Unlinked source '{source_id}'.")

        return redirect(next_url)
