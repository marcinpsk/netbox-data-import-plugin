# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
import difflib
import hashlib
import logging
import time
import uuid
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from functools import cached_property
from typing import ClassVar
from urllib.parse import parse_qs, urlencode, urlsplit

from core.signals import clear_events
from django.contrib import messages
from django.contrib.auth.mixins import PermissionRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, IntegrityError
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.defaultfilters import pluralize
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from netbox.constants import RQ_QUEUE_LOW
from netbox.views import generic
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from utilities.permissions import get_permission_for_model
from utilities.views import ConditionalLoginRequiredMixin

from . import __version__ as _plugin_version
from . import adapters, ip_assignment
from .cable_disclosure import DEVICE_ROW, DISCLOSURE_SOURCE, POLICY_HIDDEN, POLICY_VISIBLE, policy_row_is_disclosed
from .cable_target import ELIGIBLE_TERMINATION_LIMIT, eligible_terminations
from .catalog import CANDIDATE_TARGET_PREFIX, CATALOG, POLICY_SECTIONS, OutputKind
from .contact_resolution import PrimaryContactResolver, contact_identity, suggest_contact_roles
from .device_field_review import DeviceFieldReviewer, sync_change_preview
from .field_keys import SELECT_TERMINATION_TASK, parse_termination_field_key
from .filters import ImportProfileFilterSet, InferenceBackendFilterSet
from .forms import (
    CableClassMappingForm,
    CableSegmentOverrideForm,
    ClassRoleMappingForm,
    ColumnMappingForm,
    ColumnTransformRuleForm,
    DeviceTypeMappingForm,
    ImportProfileBulkEditForm,
    ImportProfileFilterForm,
    ImportProfileForm,
    ImportProfileImportForm,
    ImportSetupForm,
    InferenceBackendFilterForm,
    InferenceBackendForm,
    cable_policy_form_initial,
)
from .identity import identity_in, identity_text
from .import_engine import (
    ImportEngine,
    PreconditionFailed,
    SelectionError,
    StalePlan,
    StaleSourceDocument,
    operator_failure_message,
)
from .models import (
    PreviewState,
    CableClassMapping,
    CableSegmentOverride,
    ClassRoleMapping,
    ColumnMapping,
    ColumnTransformRule,
    DeviceExistingMatch,
    DeviceTypeMapping,
    IgnoredFieldDifference,
    ImportExecution,
    ImportProfile,
    InferenceBackend,
    ManufacturerMapping,
    SourceDocument,
    SourceResolution,
    locked_profile_policy,
    locked_resolution_policy,
    stored_import_source,
    validate_adapter_target_module,
    validate_contact_candidate_resolution,
    validate_registered_adapter,
    validate_source_resolution_fields,
)
from .netbox_reader import NetBoxReader, PlanningTargetUnavailable
from .object_permissions import (
    POLICY_WRITE_REFUSED,
    ObjectPermissionDenied,
    assess_permission_scoped_save_option,
    delete_permission_scoped_objects,
    save_permission_scoped_object,
)
from .plan import Disposition, ImportPlan, PlanError
from .preview_coordinator import (
    SYNC_FINISHED,
    SYNC_NOT_THIS_PREVIEW,
    UNREADABLE_PREVIEW,
    CancelTraceSync,
    CommandOutcome,
    DiscardPreview,
    LockedPreview,
    PreviewClaim,
    PreviewCommand,
    PreviewCommandRefused,
    QueueImport,
    RereadPreview,
    RestorePreview,
    StalePreview,
    StartPreview,
    apply_preview_command,
    read_preview,
    setup_claim,
    sync_hold_reason,
    validate_preview_plan,
)
from .profile_yaml import (
    DuplicateYamlKeyError,
    ProfileDocumentInvalid,
    apply_profile_document,
    load_yaml_document,
    serialize_profile,
)
from .review_workspace import (
    PROFILE_POLICY_MOVED,
    TERMINATION_UNRESOLVABLE,
    IneligibleDeviceSelection,
    IneligibleLocationSelection,
    SYNC_ALL_NOTHING,
    SYNC_DEPENDENCY_HELD,
    ProfilePolicyMoved,
    ReviewWorkspace,
    UnacceptablePolicyDecision,
    clear_cable_segment_override_and_replan,
    clear_trace_location_resolution_and_replan,
    held_sync,
    save_cable_class_mapping_and_replan,
    save_cable_segment_override_and_replan,
    save_termination_resolution_and_replan,
    save_trace_device_resolution_and_replan,
    save_trace_location_resolution_and_replan,
    with_blocked_sync,
)
from .tables import (
    CableClassMappingTable,
    ClassRoleMappingTable,
    ColumnMappingTable,
    ColumnTransformRuleTable,
    DeviceTypeMappingTable,
    ImportExecutionTable,
    ImportProfileTable,
    InferenceBackendTable,
)
from .trace_device_resolution import DeviceEvidence, eligible_trace_devices, source_device_key
from .trace_location_resolution import (
    eligible_trace_locations,
    location_prefix_spellings,
    present_location_tree,
    site_locations,
    source_location_key,
)
from .values import (
    effective_device_name,
    normalize_for_compare,
    source_position,
    source_text,
    status_map,
    translation_maps,
)


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


def _row_key(unit) -> str:
    """Return the key the sync modal looks a row up by.

    Row numbers repeat across object types, so the number alone lets one row replace another.
    """
    return f"{unit.object_type}:{unit.row_number}"


#: The rack filter's "no rack" option value. Tom Select drops an option whose value is empty.
NO_RACK_FILTER_VALUE = "__no_rack__"


def _rack_filter_options(units) -> tuple[list[dict[str, str]], str]:
    """Return the racks the preview names and the value that stands for no rack.

    Rows that name no rack are offered as their own option, matching the rack view's group.
    """
    named = {unit.rack_name for unit in units if unit.rack_name}
    options = [{"value": rack, "label": rack} for rack in sorted(named, key=identity_text)]
    no_rack_value = NO_RACK_FILTER_VALUE
    # A rack may legally carry the sentinel's name, and two options cannot share one value.
    while no_rack_value in named:
        no_rack_value += "_"
    if any(unit.object_type == "device" and not unit.rack_name for unit in units):
        options.append({"value": no_rack_value, "label": "(No rack)"})
    return options, no_rack_value


def _sync_change_preview_by_row(units, labels):
    """Return each reviewed row's fields grouped by what a sync would do to them."""
    previews = {}
    for unit in units:
        entries = sync_change_preview(unit.extra_data, labels)
        if entries:
            previews[_row_key(unit)] = entries
    return previews


def _candidate_values(extra_data):
    """Return candidate values after validating the serialized display shape."""
    candidate_values = extra_data.get("candidate_values", {})
    if not isinstance(candidate_values, Mapping) or any(
        not isinstance(candidates, Mapping) for candidates in candidate_values.values()
    ):
        raise ValidationError("The active Import Plan has invalid candidate values.")
    return candidate_values


def _contact_candidate_context(workspace, source_id):
    """Return Contact candidates and row state for one row of the active preview."""
    result_rows = [
        row for row in workspace.units if str(row.source_id) == str(source_id) and row.object_type == "device"
    ]
    source_rows = [row for row in workspace.source_rows if str(row.get("source_id")) == str(source_id)]
    if len(source_rows) != 1 or len(result_rows) != 1:
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
        return "Resolution saved." + contact_note, contact_note.strip()
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

# An Import Plan error can name stored preview data, so a response states one fixed sentence instead.
UNPLANNABLE_IMPORT = "This import could not be planned. The server log names the reason."
UPLOAD_NOT_UTF8 = "The uploaded file is not UTF-8 text."
UPLOAD_UNREADABLE = "Could not read the uploaded file. The server log names the reason."


class ImportProfileListView(generic.ObjectListView):
    """List all import profiles with their mapping counts."""

    queryset = ImportProfile.objects.prefetch_related("column_mappings", "class_role_mappings", "device_type_mappings")
    table = ImportProfileTable
    filterset = ImportProfileFilterSet
    filterset_form = ImportProfileFilterForm
    template_name = "netbox_data_import/importprofile_list.html"


class ImportProfileView(generic.ObjectView):
    """Detail view for a single import profile, with inline mapping tables."""

    queryset = ImportProfile.objects.prefetch_related(
        "column_mappings",
        "class_role_mappings",
        "device_type_mappings",
        "cable_class_mappings",
    )

    def get_extra_context(self, request, instance):
        """Inject inline mapping tables into the template context."""
        column_table = ColumnMappingTable(instance.column_mappings.all())
        class_role_table = ClassRoleMappingTable(instance.class_role_mappings.all())
        device_type_table = DeviceTypeMappingTable(instance.device_type_mappings.all())
        transform_table = ColumnTransformRuleTable(instance.column_transform_rules.all())
        cable_class_table = CableClassMappingTable(instance.cable_class_mappings.all(), viewer=request.user)
        applicable_policy_sections = frozenset(
            section.key for section in POLICY_SECTIONS if section.applies_to(instance.output_kinds)
        )
        return {
            "column_table": column_table,
            "class_role_table": class_role_table,
            "device_type_table": device_type_table,
            "transform_table": transform_table,
            "cable_class_table": cable_class_table,
            "applicable_policy_sections": applicable_policy_sections,
        }


class ImportProfileEditView(generic.ObjectEditView):
    """Create or edit an ImportProfile."""

    queryset = ImportProfile.objects.all()
    form = ImportProfileForm
    template_name = "netbox_data_import/importprofile_edit.html"


class ImportProfileDeleteView(generic.ObjectDeleteView):
    """Delete an ImportProfile and all its child mappings."""

    queryset = ImportProfile.objects.all()


class InferenceBackendListView(generic.ObjectListView):
    """Every configured backend row. At most one may be enabled, and that one is the active backend."""

    queryset = InferenceBackend.objects.all()
    table = InferenceBackendTable
    filterset = InferenceBackendFilterSet
    filterset_form = InferenceBackendFilterForm


_INFERENCE_MODELS_SESSION_KEY = "netbox_data_import.inference_backend_models"


class InferenceBackendView(generic.ObjectView):
    """One backend row, as `resolve_active_backend` reads it while this row is the enabled one."""

    queryset = InferenceBackend.objects.all()

    def get_extra_context(self, request, instance):
        """Offer model suggestions once, immediately after a connection test discovered them."""
        discovery = request.session.pop(_INFERENCE_MODELS_SESSION_KEY, None)
        if not isinstance(discovery, Mapping) or discovery.get("backend_id") != instance.pk:
            return {}
        models = discovery.get("models")
        if not isinstance(models, list) or not all(isinstance(model, str) for model in models):
            return {}
        return {"model_choices": tuple(models)}


class InferenceBackendEditView(generic.ObjectEditView):
    """Create or edit one backend row. Model validation applies the api_root trust boundary."""

    queryset = InferenceBackend.objects.all()
    form = InferenceBackendForm


class InferenceBackendDeleteView(generic.ObjectDeleteView):
    """Delete one backend row. With no enabled row left, the active backend is the plugin setting fallback."""

    queryset = InferenceBackend.objects.all()


class InferenceBackendChangeLogView(generic.ObjectChangeLogView):
    """Display the change log for one InferenceBackend."""

    queryset = InferenceBackend.objects.all()


class InferenceBackendConnectionTestView(PermissionRequiredMixin, View):
    """Run the connection test. Specification 13.1 authorizes it with this one permission."""

    permission_required = "netbox_data_import.change_inferencebackend"

    def post(self, request, pk):
        """Run the test now and show its redacted result where the configuration lives."""
        from .inference_connection_test import run_connection_test

        # restrict() applies the ObjectPermission constraints a model-level check would ignore.
        backend = get_object_or_404(InferenceBackend.objects.restrict(request.user, "change"), pk=pk)
        result = run_connection_test(backend.pk, backend.backend_key)
        request.session.pop(_INFERENCE_MODELS_SESSION_KEY, None)
        if result.models:
            request.session[_INFERENCE_MODELS_SESSION_KEY] = {
                "backend_id": backend.pk,
                "models": list(result.models),
            }
        if result.category == "ok":
            messages.success(request, f"Connection test succeeded. {result.detail}")
        else:
            category = result.category.replace("_", " ")
            messages.error(request, f"Connection test failed ({category}). {result.detail}")
        return redirect(backend.get_absolute_url())


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
        raise PreviewCommandRefused(f"Validation error in {label}: {msg}") from exc


def _get_or_init(model_class, **lookup):
    """Return an existing object for a natural lookup, or one unsaved object."""
    return model_class.objects.filter(**lookup).first() or model_class(**lookup)


class ImportProfileBulkImportView(generic.BulkImportView):
    """Import ImportProfile objects via NetBox's built-in import UI.

    Supports two formats from the same text area / file upload:

    * **Hierarchical YAML** - the format produced by the "Export YAML" button
      (top-level keys: ``profile``, ``column_mappings``, ``class_role_mappings``,
      ``device_type_mappings``, ``manufacturer_mappings``,
      ``column_transform_rules``, ``cable_class_mappings``).  All nested
      mappings are created/updated.
    * **Flat CSV/YAML** - one record per profile, plain metadata fields only
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
            except UnicodeDecodeError:
                messages.error(request, UPLOAD_NOT_UTF8)
                return redirect(reverse("plugins:netbox_data_import:importprofile_bulk_import"))
            except OSError:
                logger.warning("ImportProfileBulkImportView: the uploaded file cannot be read.", exc_info=True)
                messages.error(request, UPLOAD_UNREADABLE)
                return redirect(reverse("plugins:netbox_data_import:importprofile_bulk_import"))
        else:
            raw = request.POST.get("data", "").strip()

        if not raw:
            messages.error(request, "No data provided.")
            return redirect(reverse("plugins:netbox_data_import:importprofile_bulk_import"))

        try:
            data = load_yaml_document(raw)
        except DuplicateYamlKeyError as exc:
            messages.error(request, f"Failed to parse YAML: {exc}")
            return redirect(reverse("plugins:netbox_data_import:importprofile_bulk_import"))
        except yaml.YAMLError:
            # Input failed YAML parsing — let NetBox's BulkImportView handle it
            # (covers CSV and flat formats with YAML-invalid characters).
            if upload:
                upload.seek(0)
            return super().post(request)

        # Hierarchical format: delegate to shared helper.
        if isinstance(data, dict) and "profile" in data:
            try:
                profile, stats = apply_profile_document(data, request.user)
            except ObjectPermissionDenied as exc:
                raise PermissionDenied from exc
            except ProfileDocumentInvalid as exc:
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


class CableClassMappingAddView(_ProfileChildEditView):
    """Add a CableClass mapping to an existing ImportProfile."""

    queryset = CableClassMapping.objects.all()
    form = CableClassMappingForm
    template_name = "netbox_data_import/cableclassmapping_edit.html"
    permission_required = "netbox_data_import.add_cableclassmapping"


class CableClassMappingEditView(_ProfileChildEditView):
    """Edit an existing CableClass mapping."""

    queryset = CableClassMapping.objects.all()
    form = CableClassMappingForm
    template_name = "netbox_data_import/cableclassmapping_edit.html"
    permission_required = "netbox_data_import.change_cableclassmapping"

    def get_object(self, **kwargs):
        """Refuse an edit form that would disclose a row through its initial values."""
        obj = super().get_object(**kwargs)
        if obj.pk and not self.request.user.has_perm("netbox_data_import.view_cableclassmapping", obj):
            raise PermissionDenied
        return obj


class CableClassMappingDeleteView(_ProfileChildDeleteView):
    """Delete a CableClass mapping."""

    queryset = CableClassMapping.objects.all()
    permission_required = "netbox_data_import.delete_cableclassmapping"

    def get_object(self, **kwargs):
        """Refuse a delete page that would disclose the row it names."""
        obj = super().get_object(**kwargs)
        if not self.request.user.has_perm("netbox_data_import.view_cableclassmapping", obj):
            raise PermissionDenied
        return obj


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


def _review_workspace_url(profile):
    """Return the review surface declared for one profile's complete output set."""
    if profile.output_kinds == frozenset({OutputKind.SOURCE_TRACE}):
        return reverse("plugins:netbox_data_import:trace_workspace")
    return reverse("plugins:netbox_data_import:import_preview")


# These views intentionally use raw django.views.View rather than a NetBox
# generic view base.  The wizard is a three-step state machine, kept by the Preview Coordinator
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
        """Store the uploaded file, plan it, and make it the session's preview."""
        form = ImportSetupForm(request.POST, request.FILES, user=request.user)
        if not form.is_valid():
            return render(request, "netbox_data_import/import_setup.html", _import_setup_context(request, form))

        profile = form.cleaned_data["profile"]
        excel_file = form.cleaned_data["excel_file"]
        command = StartPreview(
            profile=profile,
            content=excel_file.read(),
            filename=excel_file.name,
            site=form.cleaned_data["site"],
            location=form.cleaned_data.get("location"),
            tenant=form.cleaned_data.get("tenant"),
        )
        try:
            apply_preview_command(request, PreviewClaim.posted(request.POST), command)
        except StalePreview as exc:
            return _stale_response(request, exc, reverse("plugins:netbox_data_import:import_setup"))
        except ImportProfile.DoesNotExist:
            # The form validated the profile, and the setup lock found it deleted since then.
            raise Http404("The import profile is no longer available.") from None
        except (adapters.SourceUnreadable, adapters.UnknownSourceAdapter, PlanningTargetUnavailable) as exc:
            logger.warning("ImportSetupView: source planning refused", exc_info=True)
            messages.error(request, f"Failed to parse file: {operator_failure_message(exc)}")
            return render(request, "netbox_data_import/import_setup.html", _import_setup_context(request, form))
        except PlanError:
            logger.warning("ImportSetupView: planning produced an unreadable Import Plan.", exc_info=True)
            messages.error(request, UNPLANNABLE_IMPORT)
            return render(request, "netbox_data_import/import_setup.html", _import_setup_context(request, form))
        except PreviewCommandRefused as exc:
            messages.error(request, exc.operator_message)
            return render(request, "netbox_data_import/import_setup.html", _import_setup_context(request, form))
        return redirect(_review_workspace_url(profile))


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
    claimed_by_row = row.extra_data.get("claimed_by_row")
    if claimed_by_row is not None:
        identities.append((claimed_by_row, row.object_type))
    return tuple(dict.fromkeys(identity for identity in identities if identity != (row.row_number, row.object_type)))


# A rack position collides between two rows, so either row of the group offers these.
_GROUP_CONFLICT_ACTIONS = ("ignore_position", "ignore_row")

# The Resolve column of the conflict comparison renders these, and nothing else.
_RESOLVE_COLUMN_ACTIONS = ("ignore_serial", "ignore_position", "ignore_row")


def _group_offered_actions(row, u_position, group_actions):
    """Return the actions one member of a conflict group can run, its own first."""
    offered = list(row.extra_data.get("offered_actions", []))
    if not row.source_id:
        return offered
    for action in group_actions:
        if action in offered or (action == "ignore_position" and not u_position):
            continue
        offered.append(action)
    return offered


def _conflict_comparison_row(row, source_rows_by_number, *, is_current, group_actions=()):
    """Return the source facts that the conflict comparison shows for one result row."""
    source_row = source_rows_by_number.get(row.row_number, {})
    extra_data = row.extra_data
    serial = extra_data.get("source_serial", source_row.get("serial", ""))
    asset_tag = extra_data.get("asset_tag", source_row.get("asset_tag", ""))
    rack_name = row.rack_name or source_row.get("rack_name", "")
    if not rack_name and row.object_type == "rack":
        rack_name = row.name
    u_position = extra_data.get("u_position", source_row.get("u_position"))
    offered = _group_offered_actions(row, u_position, group_actions)
    return {
        "row_number": row.row_number,
        "name": row.name,
        "source_id": row.source_id,
        "serial": serial,
        "asset_tag": asset_tag,
        "rack_name": rack_name,
        "u_position": u_position,
        "face": extra_data.get("face", source_row.get("face", "")),
        "action": row.action,
        "detail": row.detail,
        # The row column's own actions, plus the ones any member of this conflict group can run.
        "offered_actions": offered,
        # One source of truth for whether the Resolve column has anything to draw.
        "has_resolve_action": any(action in offered for action in _RESOLVE_COLUMN_ACTIONS),
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
        group_actions = [
            action for action in row.extra_data.get("offered_actions", ()) if action in _GROUP_CONFLICT_ACTIONS
        ]
        conflict_rows_by_row[(row.row_number, row.object_type)] = [
            _conflict_comparison_row(row, source_rows_by_number, is_current=True, group_actions=group_actions),
            *(
                _conflict_comparison_row(
                    other_row, source_rows_by_number, is_current=False, group_actions=group_actions
                )
                for other_row in other_rows
            ),
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


TARGET_GONE = "The saved import target is no longer available. Start a new preview."
CONCURRENT_WRITE = "A row this command writes changed while it was processed. Try again."


def _unregistered_adapter_reason(profile):
    """Return why this release cannot plan for the profile's adapter, or None."""
    try:
        validate_registered_adapter(profile)
        # Planning raises the same error for a registered adapter no Target Module implements.
        validate_adapter_target_module(profile.source_adapter)
    except ValidationError as exc:
        return "; ".join(exc.messages)
    return None


def _review_snapshot(request, *, profile_action="change"):
    """Return the active preview a review page shows, or the response that sends the operator elsewhere.

    A page load only reads (ADR 0004), so a preview it cannot show stays until a command replaces it.
    """
    snapshot = read_preview(request, profile_action=profile_action)
    setup = redirect(reverse("plugins:netbox_data_import:import_setup"))
    if snapshot.state == PreviewState.EXPIRED or snapshot.expired:
        messages.warning(request, "This import preview expired. Start a new import.")
        return None, setup
    if snapshot.state not in PreviewState.ACTIVE:
        messages.warning(request, "No import in progress. Please start a new import.")
        return None, setup
    if snapshot.profile is None:
        messages.warning(request, "Import profile not found.")
        return None, setup
    if reason := _unregistered_adapter_reason(snapshot.profile):
        messages.error(request, reason)
        return None, setup
    if snapshot.document is None:
        messages.warning(request, "The stored source is no longer available. Upload it again.")
        return None, setup
    if snapshot.state == PreviewState.SUBMITTED:
        from core.models import Job

        if not Job.objects.filter(pk=snapshot.job_id).exists():
            return None, render(
                request,
                "netbox_data_import/preview_notice.html",
                {
                    "title": "Import Job is no longer available",
                    "message": "The import Job was removed. Re-read this preview before the next decision.",
                    "preview_claim": snapshot.claim,
                    "offer_reread": True,
                    "next_url": request.get_full_path(),
                },
            )
        return None, redirect(reverse("plugins:netbox_data_import:import_progress", kwargs={"pk": snapshot.job_id}))
    return snapshot, None


def _retained_sync(snapshot):
    """Return the status of the preview's own trace sync and why it holds the preview, for a page that says so."""
    if snapshot.state != PreviewState.SYNC_PENDING:
        return None, ""
    from core.models import Job

    from .jobs import IMPORT_TASK_LOST, LOST, import_job_status

    job = Job.objects.filter(pk=snapshot.job_id).first()
    if job is None:
        return None, SYNC_FINISHED
    status = import_job_status(job)
    if status.state == LOST:
        return status, IMPORT_TASK_LOST
    return status, sync_hold_reason([job.status]) or SYNC_FINISHED


def _sync_status_context(snapshot, identity: str, retained) -> dict:
    """Return what the workspace's sync status block renders from `_retained_sync`, on the page and for each poll."""
    status, reason = retained
    read_url = reverse("plugins:netbox_data_import:trace_sync_status")
    return {
        "sync_status": status,
        "retained_sync_reason": reason,
        "sync_active": status is not None and status.active,
        "sync_status_url": f"{read_url}?{urlencode({'trace': identity, **snapshot.claim.fields()})}",
        "sync_trace": identity,
        "preview_claim": snapshot.claim,
        "reread_next": _trace_workspace_url(identity),
    }


def _live_plan(snapshot, actor):
    """Return the plan live NetBox states right now for the preview's source, or None once its target is gone."""
    try:
        return ImportEngine.plan(snapshot.profile, snapshot.document, actor, snapshot.planning_context)
    except PlanningTargetUnavailable:
        return None


def _unreadable_preview_page(request, snapshot):
    """Show the recovery a preview whose stored plan this release cannot read still has: re-read it."""
    logger.warning("The stored Import Plan of this preview is unreadable.")
    return render(
        request,
        "netbox_data_import/preview_notice.html",
        {
            "title": "Preview needs a re-read",
            "message": "This preview was planned by an earlier release. Re-read it from its stored source.",
            "preview_claim": snapshot.claim,
            "offer_reread": True,
            "next_url": request.get_full_path(),
        },
    )


class ImportPreviewView(PermissionRequiredMixin, View):
    """Step 2: show the stored preview, let user confirm or go back."""

    permission_required = "netbox_data_import.change_importprofile"

    def get(self, request):
        """Render the current preview URL."""
        return self.render_preview(request, request.get_full_path())

    def render_preview(self, request, preview_url):
        """Render the session's stored preview and say whether live NetBox has moved under it."""
        snapshot, refusal = _review_snapshot(request)
        if refusal is not None:
            return refusal
        profile = snapshot.profile
        try:
            result = snapshot.workspace(request.user)
        except PlanError:
            logger.warning("ImportPreviewView: the stored Import Plan is unreadable.", exc_info=True)
            return _unreadable_preview_page(request, snapshot)
        if retained_reason := _retained_sync(snapshot)[1]:
            messages.warning(request, retained_reason)
        live = _live_plan(snapshot, request.user)
        if live is None:
            messages.warning(request, TARGET_GONE)
            return redirect(reverse("plugins:netbox_data_import:import_setup"))
        rows = result.source_rows

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

        # The preview serves every adapter, and only the flat one declares a stored view mode.
        stored_view_mode = profile.adapter_settings.get("preview_view_mode", "rows")
        view_mode = parse_qs(urlsplit(preview_url).query).get("view", [stored_view_mode])[-1]

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
            _row_key(r): r.extra_data.get("conflicts", {}) for r in result.units if r.extra_data.get("conflicts")
        }
        # The modal names a field for the operator; the catalog is where those names live.
        target_field_labels = {key: CATALOG.display(key) for key, _label in CATALOG.choices()}
        candidate_values_by_row = {}
        try:
            for row in result.units:
                candidate_values = _candidate_values(row.extra_data)
                if candidate_values:
                    candidate_values_by_row[_row_key(row)] = candidate_values
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
            return redirect(reverse("plugins:netbox_data_import:import_setup"))
        contact_suggestions_by_row = {
            _row_key(r): r.extra_data["contact_suggestion"]
            for r in result.units
            if r.extra_data.get("contact_suggestion")
        }
        contact_role_suggestions_by_row = {
            row_number: suggest_contact_roles(candidates["contact"])
            for row_number, candidates in candidate_values_by_row.items()
            if candidates.get("contact")
        }
        extra_columns_by_row = {
            _row_key(r): r.extra_data.get("extra_columns", {})
            for r in result.units
            if r.extra_data.get("extra_columns")
        }
        sync_change_preview_by_row = _sync_change_preview_by_row(result.units, target_field_labels)
        rack_filter_options, no_rack_filter_value = _rack_filter_options(result.units)
        split_field_values_by_source_id = {
            r.source_id: _split_field_values(r) for r in result.units if r.object_type == "device" and r.source_id
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
                "filename": snapshot.context.get("filename", ""),
                "profile_id": profile.pk,
                "profile": profile,
                "preview_url": preview_url,
                "view_mode": view_mode,
                # Only a trace preview has a workspace to open, so only it offers the link.
                "trace_workspace_available": result.has_traces,
                "existing_resolutions_json": _json.dumps(existing_resolutions).translate(
                    {ord("<"): "\\u003C", ord(">"): "\\u003E", ord("&"): "\\u0026"}
                ),
                "existing_resolutions": existing_resolutions,
                "plugin_version": _plugin_version,
                "resolved_contact_source_ids": [
                    source_id for source_id, columns in existing_resolutions.items() if "candidate:contact" in columns
                ],
                "configured_source_classes": set(profile.class_role_mappings.values_list("source_class", flat=True)),
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
                "sync_change_preview_by_row": sync_change_preview_by_row,
                "rack_filter_options": rack_filter_options,
                "no_rack_filter_value": no_rack_filter_value,
                "split_field_values_by_source_id": split_field_values_by_source_id,
                "non_card_error_rows": non_card_error_rows,
                "preview_claim": snapshot.claim,
                "drift": live.fingerprint != result.plan.fingerprint,
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
    """Return the setup form, the claim it posts, and the most relevant resumable import state."""
    setup_claim(request)
    snapshot = read_preview(request, include_plan=False)
    resumable = snapshot.active and snapshot.state in (PreviewState.READY, PreviewState.SYNC_PENDING)
    return {
        "form": form,
        "preview_claim": snapshot.claim,
        "resume_job": _resume_import_job(request, snapshot),
        "resume_preview_url": _review_workspace_url(snapshot.profile) if resumable else "",
        "discardable": snapshot.state in PreviewState.ACTIVE,
    }


def _resume_import_job(request, snapshot):
    """Return the preview's own active Job, or the user's latest active import Job."""
    from core.choices import JobStatusChoices

    jobs = _user_import_jobs(request).filter(status__in=JobStatusChoices.ENQUEUED_STATE_CHOICES)
    if snapshot.job_id is not None and (job := jobs.filter(pk=snapshot.job_id).first()):
        return job
    return jobs.first()


def _import_source_rows_available(job):
    """Return whether one Job's stored source is still available."""
    source_document_id = (job.data or {}).get("source_document_id")
    return bool(source_document_id and SourceDocument.objects.filter(pk=source_document_id).exists())


def _import_job_progress(request, job):
    """Return current progress from native Job data and RQ metadata, and what the operator can do next.

    A failed final import offers its preview back only while the session's preview is still the one
    that submitted it; a newer preview is never replaced from here.
    """
    from core.choices import JobStatusChoices
    from .jobs import IMPORT_TASK_LOST, LOST, QUEUED, import_job_status

    data = job.data or {}
    status = import_job_status(job)
    percentage = round(status.processed * 100 / status.total) if status.total else 0
    abandoned = status.state == LOST
    cancelled = data.get("phase") == "cancelled"
    is_failed = abandoned or job.status in (JobStatusChoices.STATUS_FAILED, JobStatusChoices.STATUS_ERRORED)
    snapshot = read_preview(request, include_plan=False)
    restorable = (
        is_failed
        and snapshot.state == PreviewState.SUBMITTED
        and snapshot.job_id == job.pk
        and snapshot.active
        and _import_source_rows_available(job)
    )
    holds_preview = snapshot.active and snapshot.state == PreviewState.SYNC_PENDING and snapshot.job_id == job.pk
    execution_id = data.get("import_execution_id")
    return {
        "job": job,
        "status": status,
        "processed": status.processed,
        "total": status.total,
        "percentage": percentage,
        "is_active": status.active,
        "is_completed": job.status == JobStatusChoices.STATUS_COMPLETED,
        "is_failed": is_failed,
        "cancelled": cancelled,
        "cancel_claim": snapshot.claim if holds_preview and status.state == QUEUED else None,
        "workspace_url": _review_workspace_url(snapshot.profile) if cancelled and snapshot.active else "",
        "restore_claim": snapshot.claim if restorable else None,
        "preview_replaced": (
            is_failed
            and not restorable
            and not cancelled
            and isinstance(data.get("context_data"), dict)
            and snapshot.job_id != job.pk
        ),
        "results_url": (
            reverse("plugins:netbox_data_import:import_results", kwargs={"pk": execution_id}) if execution_id else ""
        ),
        "message": IMPORT_TASK_LOST if abandoned else data.get("message") or "",
    }


class _PreviewCommandMixin:
    """Render the refusals a preview command raises, the same way for every command view."""

    def dispatch(self, request, *args, **kwargs):
        try:
            return super().dispatch(request, *args, **kwargs)
        except (StalePreview, ProfilePolicyMoved) as exc:
            return _stale_response(request, exc, self.refusal_url(request), json=self._answers_json(request))
        except PreviewCommandRefused as exc:
            return self._refusal(request, exc.operator_message, exc.status)
        except StaleSourceDocument as exc:
            return self._refusal(request, exc.operator_message, 409, reverse("plugins:netbox_data_import:import_setup"))
        except PlanningTargetUnavailable:
            return self._refusal(request, TARGET_GONE, 409, reverse("plugins:netbox_data_import:import_setup"))
        except ObjectPermissionDenied as exc:
            # The permission names an object the caller may not be allowed to know exists.
            logger.warning("%s: write refused outside the caller's object scope: %s", type(self).__name__, exc)
            return self._refusal(
                request,
                "Permission denied: this action is outside your NetBox object permissions.",
                403,
            )
        except ImportProfile.DoesNotExist:
            # Every policy write locks its profile, which can be deleted after the page was rendered.
            return self._refusal(request, "The import profile is no longer available.", 404)
        except IntegrityError:
            return self._refusal(request, CONCURRENT_WRITE, 409)
        except ValidationError as exc:
            # A model refused a value the command derived from the plan, so the command wrote nothing.
            return self._refusal(request, "; ".join(exc.messages), 400)

    def _answers_json(self, request) -> bool:
        return getattr(self, "permission_denied_response_format", "redirect") == "json" or _wants_json(request)

    def _refusal(self, request, error, status, url=None):
        """Render one refused command the way this caller asked for its answer."""
        if self._answers_json(request):
            return JsonResponse({"ok": False, "error": error}, status=status)
        messages.error(request, error)
        return _navigation_response(request, url or self.refusal_url(request))

    def refusal_url(self, request):
        """Return the page a refused form command goes back to."""
        return _safe_next_url(request, "plugins:netbox_data_import:import_preview")


def _stale_json(exc):
    """Refuse a stale preview read or command in the JSON envelope every preview script reads."""
    return JsonResponse({"ok": False, "error": exc.operator_message, "code": "preview_stale"}, status=409)


def _stale_response(request, exc, url, *, json=False):
    """Refuse a command made against a preview that is not the active one, with HTTP 409 in every format."""
    message = exc.operator_message
    if json or _wants_json(request):
        return _stale_json(exc)
    if request.headers.get("HX-Request") == "true":
        # htmx follows this header before it decides whether a 409 swaps, so the page reloads and says why.
        messages.error(request, message)
        response = HttpResponse(status=409)
        response["HX-Redirect"] = url
        return response
    return render(
        request,
        "netbox_data_import/preview_notice.html",
        {"title": "This preview changed", "message": message, "next_url": url},
        status=409,
    )


class FinalImport(QueueImport):
    """Queue every actionable unit of the reviewed plan, which submits the preview."""

    def selection_for(self, preview):
        """Refuse a plan with errors or with nothing to apply; otherwise select every actionable unit."""
        if preview.workspace.has_errors:
            raise PreviewCommandRefused("Resolve every preview error before importing.")
        selection = [unit.identity for unit in preview.plan.units if unit.disposition == Disposition.ACTIONABLE]
        if not selection:
            raise PreviewCommandRefused("The accepted Import Plan has no changes to apply.")
        return selection


class ImportRunView(_PreviewCommandMixin, PermissionRequiredMixin, View):
    """Step 3: queue the accepted Import Plan."""

    permission_required = "netbox_data_import.change_importprofile"

    def post(self, request):
        """Queue the accepted plan and redirect to its progress page."""
        result = apply_preview_command(request, PreviewClaim.posted(request.POST), FinalImport())
        return redirect(reverse("plugins:netbox_data_import:import_progress", kwargs={"pk": result.outcome.job_id}))


SESSION_ENDED = "Your session has ended. Reload the page to log in again."


class _SessionEndedRefusal:
    """Refuse an ended session in the JSON envelope, so htmx never swaps the login page into the workspace."""

    def handle_no_permission(self):
        """Answer an anonymous script or htmx caller with 401; a plain form post still goes to the login page."""
        if not self.request.user.is_authenticated and (
            self.request.headers.get("HX-Request") == "true" or _wants_json(self.request)
        ):
            return JsonResponse({"ok": False, "error": SESSION_ENDED}, status=401)
        return super().handle_no_permission()


class PreviewRereadView(_SessionEndedRefusal, _PreviewCommandMixin, PermissionRequiredMixin, View):
    """Re-read the preview from its stored source against live NetBox, which also recovers a plan of an older schema."""

    permission_required = "netbox_data_import.change_importprofile"

    def post(self, request):
        """Replan the preview and go back to the page that asked."""
        result = apply_preview_command(request, PreviewClaim.posted(request.POST), RereadPreview())
        messages.success(request, result.outcome.message)
        return redirect(self.refusal_url(request))

    def refusal_url(self, request):
        """Return the posted page, so a re-read keeps the trace and the view the operator was on."""
        return _safe_next_url(request, "plugins:netbox_data_import:import_preview")


class PreviewDiscardView(_PreviewCommandMixin, PermissionRequiredMixin, View):
    """Discard the session's preview."""

    permission_required = "netbox_data_import.change_importprofile"

    def post(self, request):
        """End the preview and go back to setup."""
        result = apply_preview_command(request, PreviewClaim.posted(request.POST), DiscardPreview())
        messages.success(request, result.outcome.message)
        return redirect(reverse("plugins:netbox_data_import:import_setup"))

    def refusal_url(self, request):
        """Return the setup page, which shows whatever preview is active now."""
        return reverse("plugins:netbox_data_import:import_setup")


class ImportRestoreView(_PreviewCommandMixin, PermissionRequiredMixin, View):
    """Return to the preview whose final import failed."""

    permission_required = "netbox_data_import.change_importprofile"

    def post(self, request, pk):
        """Re-read the failed import's preview and open it."""
        get_object_or_404(_user_import_jobs(request), pk=pk)
        result = apply_preview_command(request, PreviewClaim.posted(request.POST), RestorePreview(pk))
        messages.success(request, result.outcome.message)
        profile = ImportProfile.objects.filter(pk=result.profile_id).first()
        return redirect(_review_workspace_url(profile) if profile is not None else self.refusal_url(request))

    def refusal_url(self, request):
        """Return the progress page the command came from."""
        return reverse("plugins:netbox_data_import:import_progress", kwargs=request.resolver_match.kwargs)


class ImportProgressView(PermissionRequiredMixin, View):
    """Show one resumable background import and its current progress."""

    permission_required = "netbox_data_import.change_importprofile"

    def get(self, request, pk):
        """Render the full progress page."""
        job = get_object_or_404(_user_import_jobs(request), pk=pk)
        return render(request, "netbox_data_import/import_progress.html", _import_job_progress(request, job))


class ImportProgressStatusView(PermissionRequiredMixin, View):
    """Render the HTMX progress fragment or redirect a completed import."""

    permission_required = "netbox_data_import.change_importprofile"

    def get(self, request, pk):
        """Return the current Job state."""
        job = get_object_or_404(_user_import_jobs(request), pk=pk)
        progress = _import_job_progress(request, job)
        if progress["is_completed"] and progress["results_url"]:
            response = HttpResponse(status=204)
            response["HX-Redirect"] = progress["results_url"]
            return response
        return render(request, "netbox_data_import/_import_progress.html", progress)


class ImportResultsView(PermissionRequiredMixin, View):
    """Step 4: show one Import Execution audit outcome."""

    permission_required = "netbox_data_import.view_importexecution"

    def get(self, request, pk):
        """Render the results page for one Import Execution this operator ran."""
        execution = (
            ImportExecution.objects.select_related("profile", "source_document")
            .filter(pk=pk, actor=request.user)
            .first()
        )
        if execution is None or not request.user.has_perm(self.permission_required, execution):
            return redirect(reverse("plugins:netbox_data_import:import_setup"))
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


def _wants_json(request) -> bool:
    """Return whether one preview action expects a JSON response."""
    return "application/json" in request.headers.get("Accept", "")


def _command_response(request, result, next_url, *, row_number=None, detail="", **extra):
    """Report one committed preview command: JSON for a script, a message and a redirect for a form.

    The coordinator replanned and advanced the revision, so the page loads again to show both.
    """
    message = result.outcome.message
    # The page loads again after every command, so the message waits for that load in both formats.
    messages.success(request, f"{message} {detail}" if detail and detail not in message else message)
    if _wants_json(request):
        return JsonResponse(
            {
                "ok": True,
                "row_number": row_number,
                "preview_state": "replanned",
                "message": message,
                "detail": detail,
                **extra,
            }
        )
    return redirect(next_url)


def _resolved_import_target(planning_context, user):
    """Return the engine context the preview names, or None once that target went stale.

    A preview outlives a permission change, so each command re-reads the target in the operator's own
    scope: a revoked ObjectPermission has to make the target unavailable, not merely unlisted.
    """
    from dcim.models import Location, Site
    from tenancy.models import Tenant

    sites = Site.objects.restrict(user, "view")
    locations = Location.objects.restrict(user, "view")
    tenants = Tenant.objects.restrict(user, "view")
    site = sites.filter(pk=planning_context.get("site_id")).first()
    location_id, tenant_id = planning_context.get("location_id"), planning_context.get("tenant_id")
    location = locations.filter(pk=location_id).first() if location_id else None
    tenant = tenants.filter(pk=tenant_id).first() if tenant_id else None
    if (
        site is None
        or (location_id and (location is None or location.site_id != site.pk))
        or (tenant_id and tenant is None)
    ):
        return None
    return {"site": site, "location": location, "tenant": tenant}


def _source_row(workspace, source_id) -> dict:
    """Return the one row of the active preview that carries this source ID, or refuse the command."""
    rows = [row for row in workspace.source_rows if source_text(row.get("source_id")) == source_id]
    if not source_id or len(rows) != 1:
        raise PreviewCommandRefused("The source ID must identify exactly one row in the active import.")
    return rows[0]


def _posted_row_number(request) -> int:
    """Return the posted source row number, or refuse the command."""
    try:
        return int(request.POST.get("row_number", ""))
    except (TypeError, ValueError):
        raise PreviewCommandRefused("A valid source row is required.") from None


@dataclass(frozen=True)
class _IgnoreDevice(PreviewCommand):
    """Add one source row of the preview to the profile's ignore list."""

    source_id: str
    device_name: str

    def apply(self, preview):
        """Store the ignore row, keeping one that already exists."""
        from .models import IgnoredDevice

        _source_row(preview.workspace, self.source_id)
        ignored = _get_or_init(IgnoredDevice, profile=preview.profile, source_id=self.source_id)
        if ignored.pk is None:
            ignored.device_name = self.device_name
            _validate_model_instance(ignored, f"ignored device '{self.source_id}'")
        save_permission_scoped_object(
            preview.actor,
            IgnoredDevice,
            {"profile": preview.profile, "source_id": self.source_id},
            {"device_name": self.device_name},
            on_existing="keep",
        )
        return CommandOutcome(message=f"Device '{self.device_name or self.source_id}' added to ignore list.")


@dataclass(frozen=True)
class _UnignoreDevice(PreviewCommand):
    """Remove one source row of the preview from the profile's ignore list."""

    source_id: str

    def apply(self, preview):
        """Delete the ignore row, refusing when there is none to delete."""
        from .models import IgnoredDevice

        _source_row(preview.workspace, self.source_id)
        rows = IgnoredDevice.objects.filter(profile=preview.profile, source_id=self.source_id)
        if not delete_permission_scoped_objects(preview.actor, rows):
            raise PreviewCommandRefused("Device was not on the ignore list (may be ignored by class mapping).")
        return CommandOutcome(message="Device removed from ignore list.")


class IgnoreDeviceView(_PreviewCommandMixin, PermissionRequiredMixin, View):
    """Mark a specific device (by source_id) as ignored for a profile."""

    permission_required = "netbox_data_import.change_importprofile"

    def post(self, request):
        """Add the specified device to the profile's ignore list."""
        command = _IgnoreDevice(
            source_id=source_text(request.POST.get("source_id")), device_name=request.POST.get("device_name", "")
        )
        result = apply_preview_command(request, PreviewClaim.posted(request.POST), command)
        return _command_response(request, result, self.refusal_url(request))


class UnignoreDeviceView(_PreviewCommandMixin, PermissionRequiredMixin, View):
    """Remove a device from the ignore list."""

    permission_required = "netbox_data_import.change_importprofile"

    def post(self, request):
        """Remove the specified device from the profile's ignore list."""
        command = _UnignoreDevice(source_id=source_text(request.POST.get("source_id")))
        result = apply_preview_command(request, PreviewClaim.posted(request.POST), command)
        return _command_response(request, result, self.refusal_url(request))


def _row_decision_source(workspace, row_number, source_id) -> dict:
    """Return the source row a row decision names, refusing one the active preview does not show."""
    rows = [
        row
        for row in workspace.source_rows
        if row.get("_row_number") == row_number and source_text(row.get("source_id")) == source_id
    ]
    if not source_id or len(rows) != 1:
        raise PreviewCommandRefused("The source ID and row must identify one active import row.")
    return rows[0]


def _device_unit(workspace, row_number):
    """Return the Device unit the preview shows on one row, for a field or placement command."""
    return next(
        (
            item
            for item in workspace.units
            if item.row_number == row_number and item.object_type == "device" and item.action in {"update", "error"}
        ),
        None,
    )


def _field_review_row(workspace, row_number, target_field):
    """Return the row a field-review command names, or refuse one the preview no longer shows."""
    row = _device_unit(workspace, row_number)
    if row is None or DeviceFieldReviewer.definition(target_field) is None or not source_text(row.source_id):
        raise StalePreview("The selected field difference is no longer current. Re-read the preview and try again.")
    return row


def _offered_difference(row, target_field) -> bool:
    """Return whether the preview offered this field for review.

    A field the import does not write is reported too, and an operator ignores it to stop
    the preview reporting it. Only a synced field has to be a writable difference.
    """
    return target_field in row.extra_data.get("field_diff", {}) or target_field in row.extra_data.get(
        "field_informational", {}
    )


def _field_baseline_error(row, device, target_field) -> str | None:
    """Return why *device* no longer holds the preview baseline of one field, or None when it does."""
    snapshots = row.extra_data["field_review_snapshots"][target_field]
    current = DeviceFieldReviewer.current_snapshot(device, target_field)
    if current is None or current.get("canonical") != snapshots.get("netbox", {}).get("canonical"):
        return "The matched NetBox value changed. Re-read the preview and try again."
    if target_field in {"u_position", "face"} and not _placement_matches_preview(device, row):
        return "The matched NetBox placement changed. Re-read the preview and try again."
    return None


def _placement_matches_preview(device, row) -> bool:
    """Return whether placement fields still match the materialized preview."""
    state = row.extra_data.get("_placement_state")
    # A baseline that states no location cannot prove the Device stayed in one, so it fails closed.
    if not isinstance(state, dict) or "location_id" not in state:
        return False
    return (
        device.location_id == state["location_id"]
        and device.rack_id == state.get("rack_id")
        and normalize_for_compare(device.position) == state.get("position", "")
        and (device.face or "") == state.get("face", "")
    )


def _locked_device_for_update(actor, device_pk):
    """Return the Device row locked and snapshotted for update, or None when it is gone or not permitted."""
    from dcim.models import Device

    # PostgreSQL refuses FOR UPDATE on a nullable outer join, so `of` locks the Device row alone.
    device = (
        Device.objects.restrict(actor, "change")
        .select_for_update(of=("self",))
        .select_related("site", "location", "rack", "device_type")
        .filter(pk=device_pk)
        .first()
    )
    if device is not None:
        device.snapshot()
    return device


def _field_review_binding(actor, profile, row, device) -> None:
    """Persist the confirmed source-to-device identity a field review rests on, or refuse."""
    allowed, error = _ensure_field_review_device_match(
        actor, profile, row.source_id, device, source_text(row.extra_data.get("asset_tag"))[:50]
    )
    if not allowed:
        raise PreviewCommandRefused(
            "Permission denied: cannot persist the source-to-device field-review match."
            if error == "permission"
            else "The source row or device is already linked elsewhere.",
            409,
        )


@dataclass(frozen=True)
class _IgnoreFieldDifference(PreviewCommand):
    """Ignore one exact current field difference of one matched Device."""

    row_number: int
    target_field: str

    def apply(self, preview):
        """Save the current snapshots of the difference the preview showed."""
        from dcim.models import Device

        row = _field_review_row(preview.workspace, self.row_number, self.target_field)
        if not _offered_difference(row, self.target_field):
            raise StalePreview("The selected field difference is no longer present. Re-read the preview.")
        snapshots = row.extra_data.get("field_review_snapshots", {}).get(self.target_field)
        device_id = row.extra_data.get("netbox_device_id")
        if not isinstance(snapshots, dict) or not device_id:
            raise PreviewCommandRefused("The selected field difference has no current matched device.", 409)
        device = Device.objects.restrict(preview.actor, "view").filter(pk=device_id).first()
        if device is None:
            raise PreviewCommandRefused("The matched NetBox device is no longer available.", 409)
        current = DeviceFieldReviewer.current_snapshot(device, self.target_field)
        if current is None or current.get("canonical") != snapshots.get("netbox", {}).get("canonical"):
            raise PreviewCommandRefused("The matched NetBox value changed. Re-read the preview and try again.", 409)
        _field_review_binding(preview.actor, preview.profile, row, device)
        try:
            save_permission_scoped_object(
                preview.actor,
                IgnoredFieldDifference,
                {
                    "profile": preview.profile,
                    "source_id": row.source_id,
                    "netbox_device_id": device.pk,
                    "target_field": self.target_field,
                },
                {"file_snapshot": snapshots.get("file", {}), "netbox_snapshot": snapshots.get("netbox", {})},
            )
        except ObjectPermissionDenied as exc:
            raise PreviewCommandRefused("Permission denied: cannot create or change this field review.", 409) from exc
        except ValidationError as exc:
            raise PreviewCommandRefused("; ".join(exc.messages)) from exc
        return CommandOutcome(message=f"Ignored the current {self.target_field} difference.")


@dataclass(frozen=True)
class _UnignoreFieldDifference(PreviewCommand):
    """Remove one exact current field-difference review of one matched Device."""

    row_number: int
    target_field: str

    def apply(self, preview):
        """Delete only the review the preview showed."""
        from dcim.models import Device

        row = _field_review_row(preview.workspace, self.row_number, self.target_field)
        if self.target_field not in row.extra_data.get("field_ignored", {}):
            raise StalePreview("The selected field review is no longer current. Re-read the preview.")
        device = (
            Device.objects.restrict(preview.actor, "view").filter(pk=row.extra_data.get("netbox_device_id")).first()
        )
        if device is None:
            raise PreviewCommandRefused("The matched NetBox device is no longer available.", 409)
        record = (
            IgnoredFieldDifference.objects.select_for_update()
            .filter(
                profile=preview.profile,
                source_id=row.source_id,
                netbox_device_id=device.pk,
                target_field=self.target_field,
            )
            .first()
        )
        if record is None:
            raise StalePreview("The selected field review is no longer current. Re-read the preview.")
        if not preview.actor.has_perm("netbox_data_import.delete_ignoredfielddifference", record):
            raise PreviewCommandRefused("Permission denied: cannot remove this field review.", 409)
        _field_review_binding(preview.actor, preview.profile, row, device)
        record.delete()
        return CommandOutcome(message=f"Showing the {self.target_field} difference again.")


class IgnoreFieldDifferenceView(_PreviewCommandMixin, PermissionRequiredMixin, View):
    """Ignore one exact current field difference for a matched Device."""

    permission_required = "netbox_data_import.add_ignoredfielddifference"

    def post(self, request):
        """Save current snapshots from the active preview."""
        row_number = _posted_row_number(request)
        command = _IgnoreFieldDifference(
            row_number=row_number, target_field=request.POST.get("target_field", "").strip()
        )
        result = apply_preview_command(request, PreviewClaim.posted(request.POST), command)
        return _command_response(request, result, self.refusal_url(request), row_number=row_number)


class UnignoreFieldDifferenceView(_PreviewCommandMixin, PermissionRequiredMixin, View):
    """Remove one exact current field-difference review for a matched Device."""

    permission_required = "netbox_data_import.delete_ignoredfielddifference"

    def post(self, request):
        """Delete only the review represented by the active preview."""
        row_number = _posted_row_number(request)
        command = _UnignoreFieldDifference(
            row_number=row_number, target_field=request.POST.get("target_field", "").strip()
        )
        result = apply_preview_command(request, PreviewClaim.posted(request.POST), command)
        return _command_response(request, result, self.refusal_url(request), row_number=row_number)


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
        try:
            snapshot = read_preview(request, expected=PreviewClaim.posted(request.GET), profile_action="view")
        except StalePreview as exc:
            return _stale_json(exc)
        if not snapshot.active:
            return _stale_json(StalePreview("No current import preview matches this request."))
        # The open picker outlives an upgrade, so the stored profile can name a retired adapter.
        if reason := _unregistered_adapter_reason(snapshot.profile):
            return JsonResponse({"error": reason}, status=400)
        try:
            candidates, _source_row, _result_row = _contact_candidate_context(
                snapshot.workspace(request.user), request.GET.get("source_id", "")
            )
        except PlanError:
            return JsonResponse({"error": UNREADABLE_PREVIEW}, status=409)
        except ValidationError as exc:
            return JsonResponse({"error": "; ".join(exc.messages)}, status=400)
        return JsonResponse({"suggestion": PrimaryContactResolver.suggest(candidates, snapshot.profile, request.user)})


@dataclass(frozen=True)
class _SyncDeviceField(PreviewCommand):
    """Apply one previewed field value of one row to its matched Device."""

    row_number: int
    field: str

    def apply(self, preview):
        """Write the file value the preview showed onto the locked Device, after rechecking its baseline."""
        row = _device_unit(preview.workspace, self.row_number)
        device_id = row.extra_data.get("netbox_device_id") if row is not None else None
        if not device_id:
            raise StalePreview("The active preview row is no longer available.")
        if self.field not in row.extra_data.get("field_diff", {}):
            raise StalePreview("The selected field difference is no longer present.")
        snapshots = row.extra_data.get("field_review_snapshots", {}).get(self.field)
        if not isinstance(snapshots, dict):
            raise PreviewCommandRefused("The selected field has no authoritative preview value.", 409)
        device = _locked_device_for_update(preview.actor, device_id)
        if device is None:
            raise PreviewCommandRefused("Device not found", 409)
        if error := _field_baseline_error(row, device, self.field):
            raise PreviewCommandRefused(error, 409)
        value = snapshots.get("file", {}).get("canonical", "")
        try:
            display = SyncDeviceFieldView._apply_field(device, self.field, value, status_map(), preview.actor)
        except PreviewCommandRefused:
            raise
        except Exception as exc:
            logger.exception("SyncDeviceFieldView failed for device_id=%s field=%s", device.pk, self.field)
            raise PreviewCommandRefused("An internal error occurred.", 500) from exc
        return CommandOutcome(message=f"Updated {self.field} to {display}.")


class SyncDeviceFieldView(_PreviewCommandMixin, _AjaxPermissionView):
    """Apply a single field value from the import file to an existing NetBox device."""

    permission_required = "dcim.change_device"

    _IP_FIELDS = ("primary_ip4", "primary_ip6", "oob_ip")
    _ALLOWED_FIELDS = {"u_position", "status", "serial", "asset_tag", "face", "airflow", *_IP_FIELDS}

    def post(self, request):
        """Apply one previewed field value to its matched Device."""
        field = request.POST.get("field", "")
        if not field or field not in self._ALLOWED_FIELDS:
            return JsonResponse({"ok": False, "error": f"Field '{field}' is not syncable"}, status=400)
        row_number = _posted_row_number(request)
        command = _SyncDeviceField(row_number=row_number, field=field)
        result = apply_preview_command(request, PreviewClaim.posted(request.POST), command)
        return _command_response(request, result, self.refusal_url(request), row_number=row_number)

    @staticmethod
    def _writer_safe_text(device, label, model_field, value):
        """Reject a value the writer would otherwise truncate away from what the preview showed."""
        text = str(value)
        limit = type(device)._meta.get_field(model_field).max_length
        if len(text) > limit:
            raise PreviewCommandRefused(f"The {label} is {len(text)} characters; NetBox allows {limit}.")
        return text

    @classmethod
    def _apply_field(cls, device, field, value, status_map, user):
        """Write one previewed value onto the locked and snapshotted device, through that field's own writer."""
        if field in cls._IP_FIELDS:
            return cls._apply_ip_field(device, field, value, user)
        writer = {
            "airflow": lambda: cls._apply_airflow(device, value),
            "u_position": lambda: cls._apply_u_position(device, value),
            "status": lambda: cls._apply_status(device, value, status_map),
            "serial": lambda: cls._apply_serial(device, value),
            "asset_tag": lambda: cls._apply_asset_tag(device, value),
            "face": lambda: cls._apply_face(device, value),
        }.get(field)
        if writer is None:
            raise PreviewCommandRefused(f"Field '{field}' is not syncable")
        return writer()

    @classmethod
    def _apply_u_position(cls, device, value):
        pos = source_position(value)
        if pos is None:
            raise PreviewCommandRefused(f"Cannot parse '{value}' as a finite number for u_position")
        zero_u_type = _zero_u_device_type(device)
        if zero_u_type:
            raise PreviewCommandRefused(f"Cannot set a rack position: the device type '{zero_u_type}' is 0U.")
        device.position = pos
        cls._reject_invalid_placement(device)
        device.save(update_fields=["position"])
        return f"U{device.position}"

    @staticmethod
    def _apply_status(device, value, status_map):
        text = str(value).strip().lower()
        # A NetBox status slug is accepted directly too (for example "active", "offline").
        mapped = status_map.get(text) or (text if text in set(status_map.values()) else None)
        if mapped is None:
            raise PreviewCommandRefused(f"Unknown status value '{value}'")
        device.status = mapped
        device.save(update_fields=["status"])
        return device.status

    @classmethod
    def _apply_serial(cls, device, value):
        device.serial = cls._writer_safe_text(device, "serial", "serial", value)
        device.save(update_fields=["serial"])
        return device.serial

    @classmethod
    def _apply_asset_tag(cls, device, value):
        device.asset_tag = cls._writer_safe_text(device, "asset tag", "asset_tag", value) if value else None
        device.save(update_fields=["asset_tag"])
        return device.asset_tag

    @classmethod
    def _apply_face(cls, device, value):
        if device.rack_id is None:
            raise PreviewCommandRefused(
                "Cannot set face: device has no rack assigned. Sync rack first, or use Sync Placement."
            )
        zero_u_type = _zero_u_device_type(device)
        if zero_u_type:
            raise PreviewCommandRefused(f"Cannot set a rack face: the device type '{zero_u_type}' is 0U.")
        mapped = _FACE_MAP.get(str(value).strip().lower())
        if mapped is None:
            raise PreviewCommandRefused(f"Unknown face value '{value}' — expected 'front' or 'rear'")
        device.face = mapped
        cls._reject_invalid_placement(device)
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
            raise PreviewCommandRefused(f"Unknown airflow value '{value}'")
        device.airflow = mapped
        device.save(update_fields=["airflow"])
        return device.airflow

    @classmethod
    def _apply_ip_field(cls, device, field, value, user):
        """Point one of the device's IP fields at the address the source row carries."""
        try:
            target = ip_assignment.resolve(device, field, value)
        except ip_assignment.IPAssignmentError as exc:
            raise PreviewCommandRefused(exc.operator_message) from exc

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
            raise PreviewCommandRefused("; ".join(exc.messages)) from exc
        except ObjectPermissionDenied as exc:
            raise PreviewCommandRefused("Permission denied: cannot assign this IP address.") from exc
        setattr(device, field, address)
        device.save(update_fields=[field])
        return f"{address.address} on {target.interface.name}"

    @staticmethod
    def _reject_invalid_placement(device) -> None:
        """Reject a placement value NetBox would refuse, before it reaches an unvalidated save."""
        try:
            _validate_device_placement(device)
        except ValidationError as exc:
            raise PreviewCommandRefused(_placement_error_text(exc)) from exc


def _lookup_rack_for_device(actor, device, value):
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
    qs = Rack.objects.restrict(actor, "view").filter(identity_in("name", [identity_text(name)]), site=device.site)
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


@dataclass(frozen=True)
class _SyncPlacement(PreviewCommand):
    """Apply one row's previewed rack, position and face to its matched Device, all or nothing."""

    row_number: int

    def apply(self, preview):
        """Recheck the placement baseline under the Device lock, then write the previewed placement."""
        row = _device_unit(preview.workspace, self.row_number)
        device_id = row.extra_data.get("netbox_device_id") if row is not None else None
        if not device_id:
            raise StalePreview("The active preview row is no longer available.")
        device = _locked_device_for_update(preview.actor, device_id)
        if device is None:
            raise PreviewCommandRefused("Device not found", 409)
        if not _placement_matches_preview(device, row):
            raise PreviewCommandRefused("The matched NetBox placement changed. Re-read the preview and try again.", 409)
        rack, error = _lookup_rack_for_device(preview.actor, device, row.rack_name)
        if error:
            raise PreviewCommandRefused(error)
        device.rack = rack
        # NetBox rejects a rack position on a zero-U device type, so sync the rack alone.
        zero_u_type = _zero_u_device_type(device)
        placement_fields, skipped, error = _set_rack_placement(
            device, row.extra_data.get("u_position", ""), row.extra_data.get("face", ""), zero_u_type
        )
        if error:
            raise PreviewCommandRefused(error)
        update_fields = ["rack", *placement_fields]
        try:
            _validate_device_placement(device)
        except ValidationError as exc:
            raise PreviewCommandRefused(f"Validation failed: {_placement_error_text(exc)}") from exc
        try:
            device.save(update_fields=update_fields)
        except Exception as exc:
            logger.exception("SyncPlacementView save failed for device_id=%s", device.pk)
            raise PreviewCommandRefused("An internal error occurred.", 500) from exc
        parts = [f"rack={rack.name}"]
        if "position" in update_fields and device.position is not None:
            parts.append(f"U{device.position}")
        if "face" in update_fields and device.face:
            parts.append(device.face)
        display = ", ".join(parts)
        if skipped:
            display += f" (0U device type {zero_u_type} takes no {' or '.join(skipped)})"
        return CommandOutcome(message=f"Updated placement to {display}.")


class SyncPlacementView(_PreviewCommandMixin, _AjaxPermissionView):
    """Atomically sync rack + (optional) u_position + (optional) face for a device.

    All-or-nothing: if the rack lookup fails, nothing is saved.
    """

    permission_required = "dcim.change_device"

    def post(self, request):
        """Apply the previewed placement to its matched Device."""
        row_number = _posted_row_number(request)
        result = apply_preview_command(request, PreviewClaim.posted(request.POST), _SyncPlacement(row_number))
        return _command_response(request, result, self.refusal_url(request), row_number=row_number)


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
    if (
        Device.objects.filter(site=target["site"], **tenant_filter)
        .filter(identity_in("name", [identity_text(new_name)]))
        .exists()
    ):
        return f"Device name '{new_name}' already exists at the active import site."
    return None


class _RowDecisionView(_PreviewCommandMixin, PermissionRequiredMixin, View):
    """A preview row decision: a refused one renders the preview in place for HTMX, as a saved one does."""

    permission_required = "netbox_data_import.change_importprofile"

    def _refusal(self, request, error, status, url=None):
        if self._answers_json(request):
            return JsonResponse({"ok": False, "error": error}, status=status)
        messages.error(request, error)
        return _name_resolution_response(request, url or self.refusal_url(request))

    def decide(self, request, command):
        """Run one row decision and answer with the preview it leaves behind."""
        result = apply_preview_command(request, PreviewClaim.posted(request.POST), command)
        messages.success(request, result.outcome.message)
        return _name_resolution_response(request, self.refusal_url(request))


@dataclass(frozen=True)
class _ResolveDuplicateName(PreviewCommand):
    """Save a unique device name for one duplicate source row."""

    row_number: int
    source_id: str
    new_name: str

    def apply(self, preview):
        """Check the name against the other rows and the import target, then save it."""
        source_row = _row_decision_source(preview.workspace, self.row_number, self.source_id)
        target = _resolved_import_target(preview.planning_context, preview.actor)
        if target is None:
            raise PreviewCommandRefused(TARGET_GONE, 409)
        # The claims are read under the locks, so two rows cannot both resolve to one device name.
        refusal = _device_name_already_claimed(preview.workspace.source_rows, self.row_number, self.new_name, target)
        if refusal is not None:
            raise PreviewCommandRefused(refusal)
        try:
            save_permission_scoped_object(
                preview.actor,
                SourceResolution,
                {"profile": preview.profile, "source_id": self.source_id, "source_column": "device_name"},
                {
                    "original_value": source_text(source_row.get("device_name")),
                    "resolved_fields": {"device_name": self.new_name},
                },
            )
        except ObjectPermissionDenied as exc:
            raise PreviewCommandRefused("Permission denied: cannot create or change this saved name.", 403) from exc
        except ValidationError as exc:
            raise PreviewCommandRefused("; ".join(exc.messages)) from exc
        return CommandOutcome(message=f"Source '{self.source_id}' will use device name '{self.new_name}'.")


class ResolveDuplicateNameView(_RowDecisionView):
    """Save a unique device name for one duplicate source row."""

    def post(self, request):
        """Validate and persist the replacement device name."""
        new_name = request.POST.get("new_name", "").strip()
        if not new_name or len(new_name) > 64:
            raise PreviewCommandRefused("The device name must contain 1 to 64 characters.")
        command = _ResolveDuplicateName(
            row_number=_posted_row_number(request),
            source_id=source_text(request.POST.get("source_id")),
            new_name=new_name,
        )
        return self.decide(request, command)


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


@dataclass(frozen=True)
class _IgnoreDuplicateSerial(PreviewCommand):
    """Drop the serial from one source row so the rows sharing it stop colliding."""

    row_number: int
    source_id: str

    def apply(self, preview):
        """Settle the collision the operator was shown, and only while live NetBox still has it."""
        source_row = _row_decision_source(preview.workspace, self.row_number, self.source_id)
        shown_serial = _duplicate_serial_shown(preview.workspace.units, self.row_number)
        if not shown_serial:
            raise PreviewCommandRefused("This row shows no duplicate serial in the current preview.")
        original_serial = source_text(source_row.get("serial"))
        if not original_serial:
            raise PreviewCommandRefused("This row carries no serial to give up.")
        held_since = time.monotonic()
        current = ReviewWorkspace(
            ImportEngine.plan(preview.profile, preview.document, preview.actor, preview.planning_context),
            preview.actor,
        )
        # The dry run costs more as the file grows, and the import worker waits behind it.
        logger.info(
            "IgnoreDuplicateSerialView: held the profile policy lock for %.2fs over %d source rows.",
            time.monotonic() - held_since,
            len(preview.workspace.source_rows),
        )
        if _duplicate_serial_shown(current.units, self.row_number) != shown_serial:
            raise PreviewCommandRefused(f"No other row this import creates still claims serial '{shown_serial}'.")
        try:
            save_permission_scoped_object(
                preview.actor,
                SourceResolution,
                {"profile": preview.profile, "source_id": self.source_id, "source_column": "serial"},
                {"original_value": original_serial, "resolved_fields": {"serial": ""}},
            )
        except ObjectPermissionDenied as exc:
            raise PreviewCommandRefused("Permission denied: cannot create or change this saved serial.", 403) from exc
        except ValidationError as exc:
            raise PreviewCommandRefused("; ".join(exc.messages)) from exc
        return CommandOutcome(message=f"Source '{self.source_id}' will import without serial '{shown_serial}'.")


class IgnoreDuplicateSerialView(_RowDecisionView):
    """Drop the serial from one source row so the rows sharing it stop colliding."""

    def post(self, request):
        """Persist an empty serial for the row the operator gives it up on."""
        command = _IgnoreDuplicateSerial(
            row_number=_posted_row_number(request), source_id=source_text(request.POST.get("source_id"))
        )
        return self.decide(request, command)


def _position_conflict_rows(units) -> set[int]:
    """Return every row number a rack-position collision names in the current preview."""
    involved: set[int] = set()
    for unit in units:
        if "rack_position_occupied" not in (unit.extra_data.get("identity_conflicts") or ()):
            continue
        involved.add(unit.row_number)
        for key in ("claimed_by_row", "conflict_row_number"):
            if (other := unit.extra_data.get(key)) is not None:
                involved.add(other)
    return involved


@dataclass(frozen=True)
class _IgnorePosition(PreviewCommand):
    """Drop the rack position from one source row so the rows sharing the unit stop colliding."""

    row_number: int
    source_id: str

    def apply(self, preview):
        """Save an empty rack position for a row of a collision the preview shows."""
        source_row = _row_decision_source(preview.workspace, self.row_number, self.source_id)
        # Either row of the collision may give its position up, so the whole group is eligible.
        if self.row_number not in _position_conflict_rows(preview.workspace.units):
            raise PreviewCommandRefused("This row is not part of a rack position conflict in the current preview.")
        original_position = source_text(source_row.get("u_position"))
        if not original_position:
            raise PreviewCommandRefused("This row carries no rack position to give up.")
        try:
            save_permission_scoped_object(
                preview.actor,
                SourceResolution,
                {"profile": preview.profile, "source_id": self.source_id, "source_column": "u_position"},
                {"original_value": original_position, "resolved_fields": {"u_position": None}},
            )
        except ObjectPermissionDenied as exc:
            raise PreviewCommandRefused(
                "Permission denied: cannot create or change this saved rack position.", 403
            ) from exc
        except ValidationError as exc:
            raise PreviewCommandRefused("; ".join(exc.messages)) from exc
        return CommandOutcome(
            message=f"Source '{self.source_id}' will import into its rack without position U{original_position}."
        )


class IgnorePositionView(_RowDecisionView):
    """Drop the rack position from one source row so the rows sharing the unit stop colliding."""

    def post(self, request):
        """Persist an empty rack position for the row the operator unplaces."""
        command = _IgnorePosition(
            row_number=_posted_row_number(request), source_id=source_text(request.POST.get("source_id"))
        )
        return self.decide(request, command)


# The split modal sends each part to one of these Target Fields, and a part can replace the row's value.
SPLIT_TARGET_FIELDS = ("device_name", "asset_tag", "serial", "make", "model", "rack_name")
# A serial is compared exactly; every other split target compares by name identity.
EXACT_SPLIT_FIELDS = frozenset({"serial"})
ACKNOWLEDGEMENT_INVALID = "Acknowledged fields must be a JSON list of field names."


def _split_field_values(unit) -> dict[str, str]:
    """Return the value one preview row carries for each split target, which a split part can replace."""
    return {
        "device_name": unit.name or "",
        "asset_tag": unit.extra_data.get("asset_tag", ""),
        "serial": unit.extra_data.get("source_serial", ""),
        "make": unit.extra_data.get("source_make", ""),
        "model": unit.extra_data.get("source_model", ""),
        "rack_name": unit.rack_name or "",
        "source_id": unit.source_id,
    }


def _unacknowledged_replacement(workspace, source_id, source_column, resolved_fields, acknowledged) -> str:
    """Return why a split replaces a value the current preview row carries without the operator's acknowledgement.

    The row is read from the plan at the claimed revision, so a second save compares against the first.
    """
    if source_column not in SPLIT_TARGET_FIELDS:
        return ""
    rows = [unit for unit in workspace.units if unit.object_type == "device" and str(unit.source_id) == str(source_id)]
    carried = _split_field_values(rows[0]) if len(rows) == 1 else {}
    for field, value in resolved_fields.items():
        if field == source_column or field not in SPLIT_TARGET_FIELDS or field in acknowledged:
            continue
        existing, replacement = source_text(carried.get(field)), source_text(value)
        if not existing:
            continue
        same = (
            existing == replacement
            if field in EXACT_SPLIT_FIELDS
            else identity_text(existing) == identity_text(replacement)
        )
        if not same:
            return (
                f"The split replaces the {CATALOG.display(field)} '{existing}' with '{replacement}'. "
                "Acknowledge the replacement to save it."
            )
    return ""


def _posted_acknowledgement(request) -> tuple[str, ...]:
    """Return the field names the operator acknowledged a split replaces, or refuse a malformed list."""
    import json

    try:
        acknowledged = json.loads(request.POST.get("acknowledged_fields") or "[]")
    except json.JSONDecodeError:
        raise PreviewCommandRefused(ACKNOWLEDGEMENT_INVALID) from None
    if not isinstance(acknowledged, list) or not all(isinstance(field, str) for field in acknowledged):
        raise PreviewCommandRefused(ACKNOWLEDGEMENT_INVALID)
    return tuple(acknowledged)


@dataclass(frozen=True)
class _SaveResolution(PreviewCommand):
    """Save one manual field resolution of one preview row for rerere replay."""

    source_id: str
    source_column: str
    original_value: str
    resolved_fields: Mapping
    acknowledged: tuple[str, ...]

    def apply(self, preview):
        """Validate the decision against the current row, write the Contact it names, then save it."""
        import json

        workspace = preview.workspace
        _source_row(workspace, self.source_id)
        refusal = _unacknowledged_replacement(
            workspace, self.source_id, self.source_column, self.resolved_fields, self.acknowledged
        )
        if refusal:
            raise PreviewCommandRefused(refusal)
        contact_context = None
        candidates = {}
        original_value = self.original_value
        try:
            if self.source_column == "candidate:contact":
                candidates, source_row, result_row = _contact_candidate_context(workspace, self.source_id)
                validate_contact_candidate_resolution(
                    self.resolved_fields, preview.profile.adapter_settings.primary_contact_lookup_field, candidates
                )
                original_value = json.dumps(candidates, sort_keys=True)
                contact_context = (source_row, result_row)
            validate_source_resolution_fields(preview.profile, self.source_column, self.resolved_fields)
            decision = _persist_contact_decision(
                preview.profile, self.resolved_fields, candidates, contact_context, preview.actor
            )
            save_permission_scoped_object(
                preview.actor,
                SourceResolution,
                {"profile": preview.profile, "source_id": self.source_id, "source_column": self.source_column},
                {"original_value": original_value or "", "resolved_fields": decision.resolved_fields},
            )
        except ValidationError as exc:
            raise PreviewCommandRefused("; ".join(exc.messages)) from exc
        message, detail = _saved_resolution_report(decision.write, decision.note)
        return CommandOutcome(
            message=message,
            payload={
                "row_number": contact_context[1].row_number if contact_context else None,
                "detail": detail,
                # The page keeps its own copy of the decision, so it is given the saved one.
                "resolution": {
                    "original_value": original_value or "",
                    "resolved_fields": decision.resolved_fields,
                    "contact": decision.contact,
                },
            },
        )


class SaveResolutionView(_PreviewCommandMixin, _AjaxPermissionView):
    """Save a manual field resolution for rerere replay."""

    permission_required = "netbox_data_import.change_importprofile"

    def post(self, request):
        """Persist a manual field resolution of the active preview."""
        import json

        source_id = source_text(request.POST.get("source_id"))
        source_column = request.POST.get("source_column")
        try:
            resolved_fields = json.loads(request.POST.get("resolved_fields", "{}"))
        except (json.JSONDecodeError, TypeError):
            resolved_fields = {}
        if not isinstance(resolved_fields, Mapping):
            raise PreviewCommandRefused("Resolved fields must be a JSON object.")
        if not source_id or not source_column:
            raise PreviewCommandRefused("A source row and column are required.")
        command = _SaveResolution(
            source_id=source_id,
            source_column=source_column,
            original_value=request.POST.get("original_value") or "",
            resolved_fields=resolved_fields,
            acknowledged=_posted_acknowledgement(request),
        )
        result = apply_preview_command(request, PreviewClaim.posted(request.POST), command)
        payload = result.outcome.payload
        return _command_response(
            request,
            result,
            self.refusal_url(request),
            row_number=payload["row_number"],
            detail=payload["detail"],
            resolution=payload["resolution"],
        )


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


def _bulk_row_refusal(item, required: tuple[str, ...]) -> str:
    """Return why one bulk YAML mapping row cannot be imported, or an empty string when it can."""
    if not isinstance(item, dict):
        return "A row is not a mapping."
    missing = [key for key in required if key not in item]
    return f"A row is missing the key(s): {', '.join(missing)}." if missing else ""


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
            if refusal := _bulk_row_refusal(item, ("source_class",)):
                errors.append(refusal)
                continue
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
            except Exception:
                logger.exception("BulkYamlImportView class_role row failed for profile_id=%s", profile.pk)
                errors.append("A row failed due to an unexpected error — see server logs.")
        return created, skipped

    def _import_device_type_rows(self, data, profile, errors):
        """Import a list of device-type mapping items; return (created, skipped)."""
        created = skipped = 0
        required = ("source_make", "source_model", "netbox_manufacturer_slug", "netbox_device_type_slug")
        for item in data:
            if refusal := _bulk_row_refusal(item, required):
                errors.append(refusal)
                continue
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

            data = load_yaml_document(yaml_file.read())
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

    permission_required = "netbox_data_import.view_importprofile"

    def get(self, request, pk):
        """Serialize the profile and all its mappings to YAML and return as a file download."""
        import yaml
        from django.http import HttpResponse

        profile = get_object_or_404(ImportProfile.objects.restrict(request.user, "view"), pk=pk)

        try:
            data = serialize_profile(profile, request.user)
        except ObjectPermissionDenied as exc:
            raise PermissionDenied from exc

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

    def has_permission(self):
        """Allow the page when the actor can create or update at least one profile."""
        return any(
            self.request.user.has_perm(permission)
            for permission in (
                "netbox_data_import.add_importprofile",
                "netbox_data_import.change_importprofile",
            )
        )

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
            data = load_yaml_document(yaml_file.read())
        except yaml.YAMLError as exc:
            messages.error(request, f"Failed to parse YAML: {exc}")
            return render(request, "netbox_data_import/import_profile_yaml.html")
        except OSError:
            logger.warning("ImportProfileYamlView: the uploaded file cannot be read.", exc_info=True)
            messages.error(request, UPLOAD_UNREADABLE)
            return render(request, "netbox_data_import/import_profile_yaml.html")

        try:
            profile, stats = apply_profile_document(data, request.user)
        except ObjectPermissionDenied as exc:
            raise PermissionDenied from exc
        except ProfileDocumentInvalid as exc:
            messages.error(request, str(exc))
            return render(request, "netbox_data_import/import_profile_yaml.html")

        summary = ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in stats.items())
        messages.success(request, f"Profile '{profile.name}' imported/updated. {summary}.")
        return redirect(profile.get_absolute_url())


# ---------------------------------------------------------------------------


class CheckDeviceNameView(PermissionRequiredMixin, View):
    """AJAX endpoint: check if a Device the user can view has the name identity of the given name.

    Returns JSON: {"exists": bool, "url": str|null, "id": int|null}, and "count" when several Devices match.
    """

    permission_required = "netbox_data_import.view_importprofile"

    def get(self, request):
        """Return JSON indicating whether a device with the given name exists."""
        from dcim.models import Device
        from django.http import JsonResponse

        if not request.user.has_perm("dcim.view_device"):  # pragma: no cover
            from django.http import HttpResponseForbidden

            return HttpResponseForbidden()

        key = identity_text(request.GET.get("name", ""))
        if not key:
            return JsonResponse({"exists": False, "url": None, "id": None})

        devices = Device.objects.restrict(request.user, "view").filter(identity_in("name", [key])).order_by("pk")
        found = list(devices[:2])
        if not found:
            return JsonResponse({"exists": False, "url": None, "id": None})
        answer = {
            "exists": True,
            "url": request.build_absolute_uri(found[0].get_absolute_url()),
            "id": found[0].pk,
        }
        if len(found) > 1:
            answer["count"] = devices.count()
        return JsonResponse(answer)


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


def _trace_sync_block_reason(reviewed_plan: ImportPlan, live_plan: ImportPlan) -> str:
    """Return why live NetBox prevents synchronization of the reviewed plan."""
    if live_plan.fingerprint != reviewed_plan.fingerprint:
        return "NetBox has changed. Re-read the preview before synchronizing."
    return ""


def _with_device_resolution_permissions(profile, actor, questions):
    """Add the permission state for each Device resolution action."""
    from .models import TraceDeviceResolution, index_digest

    results = []
    for question in questions:
        key = question["key"]
        assessment = assess_permission_scoped_save_option(
            actor,
            TraceDeviceResolution,
            {
                "profile": profile,
                "source_device_key": key,
                "source_device_key_digest": index_digest(key),
            },
            {
                "source_device_label": question["labels"][0] if question["labels"] else "",
                "selected_device_id": 1,
                "selected_display_name": "Pending Device selection",
            },
            unknown_fields={"selected_device_id", "selected_display_name"},
        )
        results.append(
            {
                **question,
                "action_allowed": assessment.allowed,
                "action_reason": (
                    "" if assessment.allowed else "You do not have permission to save a Device resolution."
                ),
            }
        )
    return results


def _disable_policy_form(form) -> None:
    for field in form.fields.values():
        field.disabled = True


def _visible_row_ids(model, viewer, rows) -> set[int]:
    """Return the supplied row IDs this live viewer may read."""
    row_ids = [row.pk for row in rows]
    return set(model.objects.restrict(viewer, "view").filter(pk__in=row_ids).values_list("pk", flat=True))


POLICY_SAVE_PERMISSION_REFUSED = "You do not have permission to save this CableClass policy."


def _cable_policy_forms(profile, trace, viewer) -> list:
    """Return one policy form per CableClass in the selected trace's `cable_policies` display."""
    if trace is None:
        return []
    planned = {policy["cable_class"]: policy for policy in trace.cable_policies}
    stated = list(planned)
    stored_rows = list(CableClassMapping.objects.filter(profile=profile, cable_class__in=stated))
    rows = {row.cable_class: row for row in stored_rows}
    visible_ids = _visible_row_ids(CableClassMapping, viewer, stored_rows)
    forms = []
    for index, cable_class in enumerate(stated):
        row = rows.get(cable_class)
        decision = planned[cable_class]
        visible = row is None or row.pk in visible_ids
        disclosed = row is not None and visible and policy_row_is_disclosed(decision, row)
        hidden = decision.get(POLICY_VISIBLE) is False or not visible
        moved = row is not None and visible and not disclosed
        assessment = assess_permission_scoped_save_option(
            viewer,
            CableClassMapping,
            {"profile": profile, "cable_class": cable_class},
            {
                "cable_type_resolved": False,
                "cable_type": None,
                "cable_profile_resolved": False,
                "cable_profile": None,
            },
            unknown_fields={"cable_type_resolved", "cable_type", "cable_profile_resolved", "cable_profile"},
        )
        form = CableClassMappingForm(
            instance=row if disclosed else CableClassMapping(profile=profile, cable_class=cable_class),
            auto_id=f"id_%s_{index}",
        )
        if hidden or moved or not assessment.allowed:
            _disable_policy_form(form)
        forms.append(
            {
                "cable_class": cable_class,
                "cable_type": POLICY_HIDDEN if hidden else decision["cable_type"],
                "cable_profile": POLICY_HIDDEN if hidden else decision["cable_profile"],
                "resolved": False if hidden else bool(decision["policy"]),
                "form": form,
                "reason": (
                    POLICY_WRITE_REFUSED
                    if hidden
                    else PROFILE_POLICY_MOVED
                    if moved
                    else POLICY_SAVE_PERMISSION_REFUSED
                    if not assessment.allowed
                    else ""
                ),
            }
        )
    return forms


RETAINED_SEGMENT_REASON = (
    "This plan keeps the Cable that already proves this segment, and an override cannot change it. "
    "Correct that Cable in NetBox, then re-read."
)
SEGMENT_FORCE_PERMISSION_REFUSED = "You do not have permission to force this segment policy."
SEGMENT_CLEAR_PERMISSION_REFUSED = "You do not have permission to clear this segment policy."


def _workspace_segment(workspace, unit_identity: str, position: str):
    """Return the reviewed trace and the segment it states at *position*, or None for neither."""
    try:
        index = int(position)
    except (TypeError, ValueError):
        return None, None
    trace = next((item for item in workspace.traces if item.identity == unit_identity), None)
    if trace is None:
        return None, None
    return trace, next((segment for segment in trace.segments if segment["index"] == index), None)


def _segment_policy_forms(profile, trace, viewer) -> list:
    """Return each segment of the selected trace with the control that forces its Cable policy."""
    if trace is None:
        return []
    keys = [segment["segment_key"] for segment in trace.segments if segment["segment_key"]]
    stored_rows = list(CableSegmentOverride.objects.filter(profile=profile, segment_key__in=keys))
    rows = {row.segment_key: row for row in stored_rows}
    mappings = list(
        CableClassMapping.objects.filter(
            profile=profile,
            cable_class__in={segment["cable_class"] for segment in trace.segments},
        )
    )
    mappings_by_class = {row.cable_class: row for row in mappings}
    visible_overrides = _visible_row_ids(CableSegmentOverride, viewer, stored_rows)
    visible_mappings = _visible_row_ids(CableClassMapping, viewer, mappings)
    forms = []
    for segment in trace.segments:
        stored = rows.get(segment["segment_key"])
        # An unplanned segment carries no policy disclosure, so no row can have moved under it.
        deciding = (stored or mappings_by_class.get(segment["cable_class"])) if segment["segment_key"] else None
        visible_ids = visible_overrides if stored is not None else visible_mappings
        visible = deciding is None or deciding.pk in visible_ids
        disclosed = deciding is not None and visible and policy_row_is_disclosed(segment, deciding)
        hidden = segment.get(POLICY_VISIBLE) is False or not visible
        moved = deciding is not None and visible and not disclosed
        force_assessment = assess_permission_scoped_save_option(
            viewer,
            CableSegmentOverride,
            {"profile": profile, "segment_key": segment["segment_key"]},
            {
                "cable_type": None,
                "cable_profile": None,
                "source_trace_identity": trace.trace_identity,
                "segment_index": segment["index"],
            },
            unknown_fields={"cable_type", "cable_profile"},
        )
        form = CableSegmentOverrideForm(
            instance=(
                stored
                if stored is not None and disclosed
                else CableSegmentOverride(profile=profile, segment_key=segment["segment_key"])
            ),
            initial={} if hidden or moved else cable_policy_form_initial(segment["policy"]),
            auto_id=f"id_%s_segment_{segment['index']}",
        )
        shared_reason = POLICY_WRITE_REFUSED if hidden else PROFILE_POLICY_MOVED if moved else ""
        force_reason = (
            shared_reason
            or _segment_override_reason(segment)
            or ("" if force_assessment.allowed else SEGMENT_FORCE_PERMISSION_REFUSED)
        )
        delete_permission = get_permission_for_model(CableSegmentOverride, "delete")
        clear_reason = shared_reason or (
            SEGMENT_CLEAR_PERMISSION_REFUSED
            if stored is not None and viewer is not None and not viewer.has_perm(delete_permission, stored)
            else ""
        )
        if force_reason:
            _disable_policy_form(form)
        forms.append(
            {
                **segment,
                "cable_type": POLICY_HIDDEN if hidden else segment["cable_type"],
                "cable_profile": POLICY_HIDDEN if hidden else segment["cable_profile"],
                "position": segment["index"] + 1,
                "reason": force_reason,
                "clear_reason": clear_reason,
                "form": form,
            }
        )
    return forms


def _segment_override_reason(segment) -> str:
    """Return why one segment cannot take an override now, or an empty string when it can."""
    if not segment["segment_key"]:
        return "This trace has not resolved both ends of this segment."
    if segment["retained"]:
        return RETAINED_SEGMENT_REASON
    return ""


def _workspace_cable_classes(workspace) -> set:
    """Return every CableClass value the reviewed preview's traces actually state."""
    return {
        segment["cable_class"] for trace in workspace.traces for segment in trace.segments if segment["cable_class"]
    }


def _workspace_device_questions(workspace) -> dict[str, dict]:
    """Return the active plan's Device questions, keyed by canonical source label."""
    questions: dict[str, dict] = {}
    for trace in workspace.traces:
        for item in trace.devices:
            key = source_device_key(item.get("key", ""))
            if key:
                questions.setdefault(key, item)
    return questions


def _workspace_location_paths(workspace) -> dict[str, str]:
    """Return each source Location path the active plan carries, by canonical key, in key order."""
    paths: dict[str, str] = {}
    for trace in workspace.traces:
        for item in trace.devices:
            for path in item.get("locations", ()):
                if key := source_location_key(path):
                    paths.setdefault(key, path)
    return dict(sorted(paths.items()))


def _workspace_location_prefixes(workspace) -> dict[str, str]:
    """Return each prefix key of the active plan's source Location paths, with its representative spelling."""
    return location_prefix_spellings(_workspace_location_paths(workspace))


def _deduplicate_findings(findings: list[dict[str, str]]) -> list[dict[str, str]]:
    """Return findings once per message in first-seen order, keeping every lost override."""
    messages: set[str] = set()
    unique: list[dict[str, str]] = []
    for finding in findings:
        if finding["code"] == "cable.segment_override_lost" or finding["message"] not in messages:
            messages.add(finding["message"])
            unique.append(finding)
    return unique


class _TraceWorkspaceMixin(_SessionEndedRefusal, _PreviewCommandMixin):
    """A trace workspace command: a refusal goes back to the trace it came from."""

    def refusal_url(self, request):
        """Return the trace a refused workspace command came from, so the selection survives."""
        return _trace_workspace_url(request.POST.get("trace", ""))


def _claimed_snapshot(request, *, profile_action="change", claim=None):
    """Return the preview a read answers for, which must be exactly the one its page displays."""
    expected = PreviewClaim.posted(request.GET) if claim is None else claim
    snapshot = read_preview(request, expected=expected, profile_action=profile_action)
    if not snapshot.active:
        raise StalePreview("No current import preview matches this request.")
    return snapshot


def _trace_reader(actor, profile, planning_context):
    """Return the operator's scoped reader for the import target of one trace preview."""
    return NetBoxReader.for_actor(actor).for_planning_context(planning_context, output_kinds=profile.output_kinds)


def _trace_workspace_url(identity: str) -> str:
    """Return the workspace URL that reopens one trace."""
    url = reverse("plugins:netbox_data_import:trace_workspace")
    # An identity the replan dropped selects nothing, and the page falls back to its first trace.
    return f"{url}?{urlencode({'trace': identity.strip()})}" if identity.strip() else url


def _proposal_entries(workspace, terminations, *, profile, viewer, reader):
    """Return the proposal display and each termination with its proposal read, as the workspace shows them."""
    from .proposal_presentation import ProposalPresentation

    display = ProposalPresentation(profile=profile, actor=viewer, reader=reader)
    reads = display.fields(
        [
            {**field, "source_ambiguous": workspace.termination_sources.get(field["field_key"]) is None}
            for field in terminations
        ]
    )
    return display, [
        {**field, "proposal": reads[field["field_key"]]["presentation"], "proposal_read": reads[field["field_key"]]}
        for field in terminations
    ]


def _active_proposal_count(profile, workspace, display) -> tuple[int | str, int]:
    """Return what the summary strip shows for the active proposals, and when the database counted them.

    The time is the counting statement's own start, in microseconds, so a later count saw every commit an
    earlier one saw, and the page can refuse an answer that arrives after a newer one.
    """
    from django.db.models import Count, DateTimeField, Func, Max
    from django.db.models.functions import Coalesce

    from .models import ProposalStatus, ResolutionProposal

    counted_at = Func(function="statement_timestamp", output_field=DateTimeField())
    result = ResolutionProposal.objects.filter(
        profile=profile,
        task_type=SELECT_TERMINATION_TASK,
        field_key__in=list(workspace.termination_sources),
        status__in=ProposalStatus.ACTIVE,
    ).aggregate(count=Count("pk"), at=Coalesce(Max(counted_at), counted_at))
    stamp = (result["at"] - datetime.fromtimestamp(0, tz=UTC)) // timedelta(microseconds=1)
    return ("Not permitted" if display.view_reason else result["count"]), stamp


def _device_url(question) -> str:
    """Return the NetBox page of a Device question's resolved Device, while the viewer may still view it."""
    # The live disclosure check drops this source from a question that names a hidden Device.
    source = question.get(DISCLOSURE_SOURCE)
    return reverse("dcim:device", kwargs={"pk": source["pk"]}) if source and source["kind"] == DEVICE_ROW else ""


def _termination_cards(terminations, trace, claim):
    """Add what each termination card renders beyond its proposal: the resolved Device, an id, and its read."""
    resolved = {source_device_key(device["key"]): device for device in trace.devices if device.get("selected")}
    read_url = reverse("plugins:netbox_data_import:trace_proposal")
    return [
        {
            **termination,
            "resolved_device": device["selected"] if device else "",
            "resolved_device_url": _device_url(device) if device else "",
            "card_id": "proposalCard" + hashlib.sha256(termination["field_key"].encode()).hexdigest()[:16],
            "read_url": f"{read_url}?"
            + urlencode({"field_key": termination["field_key"], "trace": trace.identity, **claim.fields()}),
        }
        for termination in terminations
        for device in (resolved.get(parse_termination_field_key(termination["field_key"])["device"]),)
    ]


def _proposal_card(request, snapshot, field_key: str, identity: str):
    """Render one termination card of the preview the page displays, for a poll or after a proposal command."""
    try:
        workspace = snapshot.workspace(request.user)
    except PlanError:
        raise StalePreview(UNREADABLE_PREVIEW) from None
    traces = [trace for trace in workspace.traces if any(item["field_key"] == field_key for item in trace.terminations)]
    trace = next((trace for trace in traces if trace.identity == identity), traces[0] if traces else None)
    if trace is None:
        raise InvalidProposalTarget("This preview asked no question about that termination.")
    field = next(item for item in trace.terminations if item["field_key"] == field_key)
    try:
        reader = _trace_reader(request.user, snapshot.profile, snapshot.planning_context)
    except PlanningTargetUnavailable:
        reader = None
    display, entries = _proposal_entries(
        workspace, [field], profile=snapshot.profile, viewer=request.user, reader=reader
    )
    active_proposals, counted_at = _active_proposal_count(snapshot.profile, workspace, display)
    return render(
        request,
        "netbox_data_import/_proposal_card_answer.html",
        {
            "termination": _termination_cards(entries, trace, snapshot.claim)[0],
            "selected_trace": trace,
            "preview_claim": snapshot.claim,
            "active_proposals": active_proposals,
            "active_proposals_counted_at": counted_at,
        },
    )


def _proposal_card_after(request, result, field_key: str):
    """Render the card a proposal command changed, from the preview the command left behind."""
    snapshot = _claimed_snapshot(request, profile_action="view", claim=result.claim)
    return _proposal_card(request, snapshot, field_key, request.POST.get("trace", ""))


class TraceReviewWorkspaceView(PermissionRequiredMixin, View):
    """Section 10.2: one review workspace page per preview, for the traces it planned."""

    permission_required = "netbox_data_import.change_importprofile"

    def get(self, request):
        """Render the reviewed traces and say whether live NetBox has moved under them."""
        snapshot, refusal = _review_snapshot(request)
        if refusal is not None:
            return refusal
        profile = snapshot.profile
        try:
            workspace = snapshot.workspace(request.user)
        except PlanError:
            return _unreadable_preview_page(request, snapshot)
        live = _live_plan(snapshot, request.user)
        if live is None:
            messages.warning(request, TARGET_GONE)
            return redirect(reverse("plugins:netbox_data_import:import_setup"))
        # Section 10.2: compared on each full load and on the re-read action, never polled.
        sync_block_reason = _trace_sync_block_reason(workspace.plan, live)
        drift = bool(sync_block_reason)
        retained = _retained_sync(snapshot)
        retained_reason = retained[1]
        block_reason = retained_reason or sync_block_reason
        traces = (
            [with_blocked_sync(trace, block_reason) for trace in workspace.traces] if block_reason else workspace.traces
        )
        wanted = request.GET.get("trace", "")
        selected = next((trace for trace in traces if trace.identity == wanted), traces[0] if traces else None)
        summary = dict(workspace.trace_summary)
        from .models import TerminationResolution, TraceDeviceResolution, TraceLocationResolution

        summary["saved_decisions"] = sum(
            model.objects.restrict(request.user, "view").filter(profile=profile).count()
            for model in (TerminationResolution, TraceDeviceResolution, TraceLocationResolution)
        )
        summary["preview_state"] = "changed in NetBox" if drift else "current"
        from .proposal_presentation import group_terminations

        try:
            reader = _trace_reader(request.user, profile, snapshot.planning_context)
        except PlanningTargetUnavailable:
            messages.warning(request, TARGET_GONE)
            return redirect(reverse("plugins:netbox_data_import:import_setup"))
        # The picker reads Locations a page at a time, so the page only asks whether any is visible.
        has_locations = site_locations(reader).exists()
        location_tree = present_location_tree(
            profile=profile,
            viewer=request.user,
            reader=reader,
            paths=_workspace_location_paths(workspace),
            has_locations=has_locations,
        )
        proposal_display, terminations = _proposal_entries(
            workspace, selected.terminations if selected else [], profile=profile, viewer=request.user, reader=reader
        )
        if selected is not None:
            selected = replace(
                selected,
                findings=(
                    _deduplicate_findings(selected.findings) if selected.disposition != "invalid" else selected.findings
                ),
                terminations=terminations,
            )
        cable_policy_forms = _cable_policy_forms(profile, selected, request.user)
        segment_policy_forms = _segment_policy_forms(profile, selected, request.user)
        attention, settled = group_terminations(selected.terminations if selected else [])
        selected_devices = _with_device_resolution_permissions(
            profile,
            request.user,
            selected.devices if selected else [],
        )
        attention_devices = [device for device in selected_devices if device.get("selectable")]
        manual_devices = [device for device in selected_devices if device.get("state_style") == "manual"]
        settled_devices = [
            device
            for device in selected_devices
            if not device.get("selectable") and device.get("state_style") != "manual"
        ]
        attention = _termination_cards(attention, selected, snapshot.claim) if selected else []
        summary["active_proposals"], summary["active_proposals_counted_at"] = _active_proposal_count(
            profile, workspace, proposal_display
        )
        ask_all_reason = (
            proposal_display.request_block_reason
            or retained_reason
            or ("" if summary["unresolved_terminations"] else "Every termination in this preview is resolved.")
        )
        return render(
            request,
            "netbox_data_import/trace_workspace.html",
            {
                "profile": profile,
                "traces": traces,
                "selected_trace": selected,
                "attention_devices": attention_devices,
                "manual_devices": manual_devices,
                "settled_devices": settled_devices,
                "attention_terminations": attention,
                "settled_terminations": settled,
                "cable_policy_forms": cable_policy_forms,
                "segment_policy_forms": segment_policy_forms,
                "summary": summary,
                "ask_all_reason": ask_all_reason,
                "sync_all": held_sync(workspace.sync_all.action, block_reason),
                "sync_all_note": workspace.sync_all.unsynced_note,
                "location_tree": location_tree,
                "has_locations": has_locations,
                "import_location_unavailable": reader.location_unavailable,
                "drift": drift,
                **_sync_status_context(snapshot, selected.identity if selected else "", retained),
                "plugin_version": _plugin_version,
            },
        )


def _refuse_drifted_sync(preview) -> None:
    """Refuse a trace sync that live NetBox has moved under."""
    live = ImportEngine.plan(preview.profile, preview.document, preview.actor, preview.planning_context)
    if reason := _trace_sync_block_reason(preview.plan, live):
        raise PreviewCommandRefused(reason, 409)


@dataclass(frozen=True)
class _TraceSync(QueueImport):
    """Queue one Source Trace with the units its changes depend on, keeping the preview pending on it."""

    identity: str
    keeps_preview = True

    def selection_for(self, preview):
        """Refuse a trace whose dependency cannot sync, a sync live NetBox has moved under, or nothing to sync."""
        selection = preview.workspace.sync_selection(self.identity)
        if selection and preview.workspace.cannot_sync(selection):
            raise PreviewCommandRefused(SYNC_DEPENDENCY_HELD)
        _refuse_drifted_sync(preview)
        if not selection:
            raise PreviewCommandRefused("That trace has no changes to synchronize.")
        return list(selection)


class TraceSyncView(_TraceWorkspaceMixin, PermissionRequiredMixin, View):
    """Synchronize one Source Trace together with the units its changes depend on."""

    permission_required = "netbox_data_import.change_importprofile"

    def post(self, request):
        """Queue the reviewed plan for one trace's own selection."""
        command = _TraceSync(identity=request.POST.get("identity", "").strip())
        result = apply_preview_command(request, PreviewClaim.posted(request.POST), command)
        return redirect(reverse("plugins:netbox_data_import:import_progress", kwargs={"pk": result.outcome.job_id}))

    def refusal_url(self, request):
        """Return the trace the sync was asked for."""
        return _trace_workspace_url(request.POST.get("identity", ""))


class _TraceSyncAll(QueueImport):
    """Queue every actionable Source Trace that can sync, with the units their changes depend on, as one Job."""

    keeps_preview = True

    def selection_for(self, preview):
        """Refuse a sync live NetBox has moved under, or a preview with no trace that can sync."""
        _refuse_drifted_sync(preview)
        selection = preview.workspace.sync_all.units
        if not selection:
            raise PreviewCommandRefused(SYNC_ALL_NOTHING)
        return list(selection)


class TraceSyncAllView(_TraceWorkspaceMixin, PermissionRequiredMixin, View):
    """Synchronize every actionable Source Trace and keep the workspace for the traces that remain."""

    permission_required = "netbox_data_import.change_importprofile"

    def post(self, request):
        """Queue the reviewed plan for every trace that can sync, then show the Job's progress."""
        result = apply_preview_command(request, PreviewClaim.posted(request.POST), _TraceSyncAll())
        return redirect(reverse("plugins:netbox_data_import:import_progress", kwargs={"pk": result.outcome.job_id}))


class TraceSyncStatusView(_TraceWorkspaceMixin, PermissionRequiredMixin, View):
    """Answer the workspace's poll of its trace sync, and reload the page once the sync ends."""

    permission_required = "netbox_data_import.view_importprofile"

    def get(self, request):
        """Return the status block while the Job waits or runs; after that the whole page changes."""
        snapshot = _claimed_snapshot(request, profile_action="view")
        context = _sync_status_context(snapshot, request.GET.get("trace", ""), _retained_sync(snapshot))
        if not context["sync_active"]:
            # The trace actions and the re-read button all change, so the page loads again.
            response = HttpResponse(status=204)
            response["HX-Refresh"] = "true"
            return response
        return render(request, "netbox_data_import/_trace_sync_status.html", context)

    def refusal_url(self, request):
        """Return the trace the polling page shows."""
        return _trace_workspace_url(request.GET.get("trace", ""))


class TraceSyncCancelView(_TraceWorkspaceMixin, PermissionRequiredMixin, View):
    """Cancel the preview's own trace sync before a worker starts it."""

    permission_required = "netbox_data_import.change_importprofile"

    def post(self, request):
        """Cancel the sync, then show the workspace with the preview re-read."""
        job_id = request.POST.get("job_id", "")
        if not job_id.isascii() or not job_id.isdigit():
            raise StalePreview(SYNC_NOT_THIS_PREVIEW)
        result = apply_preview_command(request, PreviewClaim.posted(request.POST), CancelTraceSync(int(job_id)))
        messages.success(request, result.outcome.message)
        return redirect(_trace_workspace_url(request.POST.get("trace", "")))


CANDIDATE_LIMIT_INVALID = f"Candidate limit must be an integer from 1 to {ELIGIBLE_TERMINATION_LIMIT}."
# PostgreSQL reads OFFSET and LIMIT as bigint, so the last row of a page must stay inside that range.
CANDIDATE_OFFSET_MAX = 2**63 - 1 - ELIGIBLE_TERMINATION_LIMIT
CANDIDATE_OFFSET_INVALID = f"Candidate offset must be an integer from 0 to {CANDIDATE_OFFSET_MAX}."


@dataclass(frozen=True)
class CandidatePage:
    """One picker page, or the fixed sentence that refuses it."""

    limit: int = 0
    offset: int = 0
    error: str = ""


def _candidate_page(params) -> CandidatePage:
    """Return the page limit and offset of one picker read, or of the write that rechecks its offer."""
    raw_limit, raw_offset = params.get("limit"), params.get("offset")
    try:
        limit = ELIGIBLE_TERMINATION_LIMIT if raw_limit is None else int(raw_limit)
    except (TypeError, ValueError):
        return CandidatePage(error=CANDIDATE_LIMIT_INVALID)
    if not 1 <= limit <= ELIGIBLE_TERMINATION_LIMIT:
        return CandidatePage(error=CANDIDATE_LIMIT_INVALID)
    try:
        offset = 0 if raw_offset in (None, "") else int(raw_offset)
    except (TypeError, ValueError):
        return CandidatePage(error=CANDIDATE_OFFSET_INVALID)
    if not 0 <= offset <= CANDIDATE_OFFSET_MAX:
        return CandidatePage(error=CANDIDATE_OFFSET_INVALID)
    return CandidatePage(limit=limit, offset=offset)


class TraceTerminationCandidatesView(PermissionRequiredMixin, View):
    """Serve one page of eligible terminations for the workspace picker."""

    permission_required = "netbox_data_import.change_importprofile"

    def get(self, request):
        """Return the eligible candidates and the uncapped total the count states."""
        try:
            snapshot = _claimed_snapshot(request)
            workspace = snapshot.workspace(request.user)
        except StalePreview as exc:
            return _stale_json(exc)
        except PlanError:
            return JsonResponse({"ok": False, "error": UNREADABLE_PREVIEW}, status=409)
        field_key = request.GET.get("field_key", "").strip()
        # A review read answers a question this preview asked, never one the caller invented.
        if field_key not in workspace.termination_sources:
            return JsonResponse(
                {"ok": False, "error": "This preview asked no question about that termination."}, status=400
            )
        if workspace.termination_sources[field_key] is None:
            return JsonResponse({"ok": False, "error": TERMINATION_UNRESOLVABLE}, status=400)
        page = _candidate_page(request.GET)
        if page.error:
            return JsonResponse({"ok": False, "error": page.error}, status=400)
        limit, offset = page.limit, page.offset
        try:
            reader = _trace_reader(request.user, snapshot.profile, snapshot.planning_context)
            found = eligible_terminations(
                field_key,
                reader,
                profile=snapshot.profile,
                search=request.GET.get("search", ""),
                limit=limit,
                offset=offset,
            )
        except (PlanningTargetUnavailable, ValueError):
            return JsonResponse({"ok": False, "error": TERMINATION_UNRESOLVABLE}, status=400)
        return JsonResponse(
            {
                "ok": True,
                # A candidate is its model and its id together, because two models can share one id.
                "candidates": [
                    {
                        "id": candidate.pk,
                        "object_type": candidate._meta.label_lower,
                        "model": str(candidate._meta.verbose_name),
                        "name": candidate.name,
                        "display": str(candidate),
                    }
                    for candidate in found.candidates
                ],
                "shown": len(found.candidates),
                "total": found.total,
                "offset": offset,
                "limit": limit,
            }
        )


class TraceDeviceCandidatesView(PermissionRequiredMixin, View):
    """Serve one bounded page of visible Device candidates for a plan-authored question."""

    permission_required = "netbox_data_import.change_importprofile"

    def get(self, request):
        """Return permission-scoped candidates and their source-evidence explanations."""
        try:
            snapshot = _claimed_snapshot(request)
            workspace = snapshot.workspace(request.user)
        except StalePreview as exc:
            return _stale_json(exc)
        except PlanError:
            return JsonResponse({"ok": False, "error": UNREADABLE_PREVIEW}, status=409)
        device_key = source_device_key(request.GET.get("device_key", ""))
        question = _workspace_device_questions(workspace).get(device_key)
        if question is None:
            return JsonResponse({"ok": False, "error": "This preview asked no question about that Device."}, status=400)
        search = request.GET.get("search", "")
        if len(search) > 200:
            return JsonResponse({"ok": False, "error": "Device search must be 200 characters or fewer."}, status=400)
        page = _candidate_page(request.GET)
        if page.error:
            return JsonResponse({"ok": False, "error": page.error}, status=400)
        limit, offset = page.limit, page.offset
        try:
            evidence = DeviceEvidence.from_dict(question)
        except (TypeError, ValueError):
            return JsonResponse({"ok": False, "error": "That Device cannot be resolved here."}, status=400)
        try:
            reader = _trace_reader(request.user, snapshot.profile, snapshot.planning_context)
            found = eligible_trace_devices(
                profile=snapshot.profile, reader=reader, evidence=evidence, search=search, limit=limit, offset=offset
            )
        except PlanningTargetUnavailable:
            return JsonResponse({"ok": False, "error": "That Device cannot be resolved here."}, status=400)
        return JsonResponse(
            {
                "ok": True,
                "candidates": [
                    {
                        "id": candidate.device.pk,
                        "name": candidate.device.name,
                        "display": str(candidate.device),
                        "matched_facts": [fact.to_dict() for fact in candidate.matched],
                        "conflicting_facts": [fact.to_dict() for fact in candidate.conflicting],
                        "import_location": (
                            candidate.import_location.to_dict() if candidate.import_location is not None else None
                        ),
                    }
                    for candidate in found.candidates
                ],
                "shown": len(found.candidates),
                "total": found.total,
                "offset": offset,
                "limit": limit,
            }
        )


@dataclass(frozen=True)
class _ResolveTraceDevice(PreviewCommand):
    """Save one plan-authored source Device decision; the coordinator stores the replan it returns."""

    device_key: str
    device_id: int
    search: str
    limit: int
    offset: int

    def apply(self, preview):
        """Recheck the offered Device, save it under the profile policy lock, and replan."""
        question = _workspace_device_questions(preview.workspace).get(self.device_key)
        if question is None:
            raise PreviewCommandRefused("This preview asked no question about that Device.")
        try:
            evidence = DeviceEvidence.from_dict(question)
        except (TypeError, ValueError):
            raise PreviewCommandRefused("That Device is not one of the eligible candidates.") from None
        try:
            plan, chosen = save_trace_device_resolution_and_replan(
                profile=preview.profile,
                source_document=preview.document,
                actor=preview.actor,
                planning_context=preview.planning_context,
                evidence=evidence,
                selected_device_id=self.device_id,
                search=self.search,
                limit=self.limit,
                offset=self.offset,
                reviewed_fingerprint=preview.reviewed_fingerprint,
            )
        except IneligibleDeviceSelection:
            raise PreviewCommandRefused("That Device is not one of the eligible candidates.") from None
        return CommandOutcome(message=f"Source Device resolved to '{chosen}'.", plan=plan)


class TraceResolveDeviceView(_TraceWorkspaceMixin, PermissionRequiredMixin, View):
    """Save one plan-authored source Device decision and replan the workspace."""

    permission_required = "netbox_data_import.change_importprofile"

    def post(self, request):
        """Run the decision as one coordinated command."""
        search = request.POST.get("search", "")
        if len(search) > 200:
            raise PreviewCommandRefused("Device search must be 200 characters or fewer.")
        try:
            device_id = int(request.POST.get("device_id", ""))
        except (TypeError, ValueError):
            raise PreviewCommandRefused("That Device is not one of the eligible candidates.") from None
        # The write rechecks the page that made the offer.
        page = _candidate_page(request.POST)
        if page.error:
            raise PreviewCommandRefused(page.error)
        command = _ResolveTraceDevice(
            device_key=source_device_key(request.POST.get("device_key", "")),
            device_id=device_id,
            search=search,
            limit=page.limit,
            offset=page.offset,
        )
        result = apply_preview_command(request, PreviewClaim.posted(request.POST), command)
        return _command_response(request, result, self.refusal_url(request))


LOCATION_CHOICE_REFUSED = "Choose a visible Location in the selected Site."


class TraceLocationCandidatesView(PermissionRequiredMixin, View):
    """Serve one bounded page of the selected Site's visible Locations for one source Location prefix."""

    permission_required = "netbox_data_import.change_importprofile"

    def get(self, request):
        """Return the searched page and the uncapped total the picker counts."""
        try:
            snapshot = _claimed_snapshot(request)
            workspace = snapshot.workspace(request.user)
        except StalePreview as exc:
            return _stale_json(exc)
        except PlanError:
            return JsonResponse({"ok": False, "error": UNREADABLE_PREVIEW}, status=409)
        # A review read answers a question this preview asked, never one the caller invented.
        if source_location_key(request.GET.get("location_key", "")) not in _workspace_location_prefixes(workspace):
            return JsonResponse(
                {"ok": False, "error": "This preview carries no such source Location path."}, status=400
            )
        search = request.GET.get("search", "")
        if len(search) > 200:
            return JsonResponse({"ok": False, "error": "Location search must be 200 characters or fewer."}, status=400)
        page = _candidate_page(request.GET)
        if page.error:
            return JsonResponse({"ok": False, "error": page.error}, status=400)
        limit, offset = page.limit, page.offset
        try:
            found = eligible_trace_locations(
                _trace_reader(request.user, snapshot.profile, snapshot.planning_context),
                search=search,
                limit=limit,
                offset=offset,
            )
        except PlanningTargetUnavailable:
            return JsonResponse({"ok": False, "error": "The import target is no longer available."}, status=400)
        return JsonResponse(
            {
                "ok": True,
                "candidates": [
                    {"id": candidate.location.pk, "name": candidate.location.name, "parent": candidate.parent}
                    for candidate in found.candidates
                ],
                "shown": len(found.candidates),
                "total": found.total,
                "offset": offset,
                "limit": limit,
            }
        )


@dataclass(frozen=True)
class _MapTraceLocation(PreviewCommand):
    """Map one source Location prefix the reviewed preview carries, or clear its mapping."""

    location_key: str
    location_id: int | None

    def apply(self, preview):
        """Write the decision under the profile lock; the coordinator stores the replan it returns."""
        paths = _workspace_location_prefixes(preview.workspace)
        # A review command answers a question this preview asked, never one the caller invented.
        if self.location_key not in paths:
            raise PreviewCommandRefused("This preview carries no such source Location path.")
        try:
            if self.location_id is None:
                plan = clear_trace_location_resolution_and_replan(
                    profile=preview.profile,
                    source_document=preview.document,
                    actor=preview.actor,
                    planning_context=preview.planning_context,
                    source_location_key=self.location_key,
                    reviewed_fingerprint=preview.reviewed_fingerprint,
                )
            else:
                plan = save_trace_location_resolution_and_replan(
                    profile=preview.profile,
                    source_document=preview.document,
                    actor=preview.actor,
                    planning_context=preview.planning_context,
                    source_location_path=paths[self.location_key],
                    selected_location_id=self.location_id,
                    reviewed_fingerprint=preview.reviewed_fingerprint,
                )
        except UnacceptablePolicyDecision as exc:
            raise PreviewCommandRefused("; ".join(exc.errors)) from exc
        except IneligibleLocationSelection:
            raise PreviewCommandRefused(LOCATION_CHOICE_REFUSED) from None
        settled = "saved for" if self.location_id is not None else "cleared for"
        return CommandOutcome(message=f"Location mapping {settled} '{paths[self.location_key]}'.", plan=plan)


class TraceLocationMappingView(_TraceWorkspaceMixin, PermissionRequiredMixin, View):
    """Map one source Location prefix the reviewed preview carries, or clear its mapping, then replan."""

    permission_required = "netbox_data_import.change_importprofile"

    def post(self, request):
        """Run the save or the clear as one coordinated command."""
        location_id = None
        if not request.POST.get("clear"):
            try:
                location_id = int(request.POST.get("location_id", ""))
            except (TypeError, ValueError):
                raise PreviewCommandRefused(LOCATION_CHOICE_REFUSED) from None
        command = _MapTraceLocation(
            location_key=source_location_key(request.POST.get("location_key", "")), location_id=location_id
        )
        result = apply_preview_command(request, PreviewClaim.posted(request.POST), command)
        return _command_response(request, result, self.refusal_url(request))


@dataclass(frozen=True)
class _SetCablePolicy(PreviewCommand):
    """Set the Cable policy for one CableClass the reviewed preview states."""

    cable_class: str
    data: Mapping

    def apply(self, preview):
        """Save the policy under the profile lock; the coordinator stores the replan it returns."""
        # A review command answers a question this preview asked, never one the caller invented.
        if self.cable_class not in _workspace_cable_classes(preview.workspace):
            raise PreviewCommandRefused("This preview states no segment with that CableClass.")
        try:
            plan = save_cable_class_mapping_and_replan(
                profile=preview.profile,
                source_document=preview.document,
                actor=preview.actor,
                planning_context=preview.planning_context,
                cable_class=self.cable_class,
                data=dict(self.data),
                reviewed_fingerprint=preview.reviewed_fingerprint,
            )
        except UnacceptablePolicyDecision as exc:
            raise PreviewCommandRefused("; ".join(exc.errors)) from exc
        return CommandOutcome(message=f"Cable policy saved for CableClass '{self.cable_class}'.", plan=plan)


class TraceCablePolicyView(_TraceWorkspaceMixin, PermissionRequiredMixin, View):
    """Set the Cable policy for one CableClass the reviewed preview states, then replan."""

    permission_required = "netbox_data_import.change_importprofile"

    def post(self, request):
        """Run the policy save as one coordinated command."""
        command = _SetCablePolicy(
            cable_class=request.POST.get("cable_class", "").strip(),
            data={
                "cable_type": request.POST.get("cable_type", ""),
                "cable_profile": request.POST.get("cable_profile", ""),
            },
        )
        result = apply_preview_command(request, PreviewClaim.posted(request.POST), command)
        return _command_response(request, result, self.refusal_url(request))


@dataclass(frozen=True)
class _SetSegmentPolicy(PreviewCommand):
    """Force one planned segment to its own Cable policy, or clear the override again."""

    trace: str
    segment: str
    clearing: bool
    data: Mapping

    def apply(self, preview):
        """Write the decision under the profile lock; the coordinator stores the replan it returns."""
        trace, segment = _workspace_segment(preview.workspace, self.trace, self.segment)
        # A review command answers a question this preview asked, never one the caller invented.
        if segment is None or not segment["segment_key"]:
            raise PreviewCommandRefused("This preview resolved no segment there.")
        # An override cannot decide a Cable the plan keeps, but the operator can still remove it.
        if segment["retained"] and not self.clearing:
            raise PreviewCommandRefused(RETAINED_SEGMENT_REASON)
        try:
            if self.clearing:
                plan = clear_cable_segment_override_and_replan(
                    profile=preview.profile,
                    source_document=preview.document,
                    actor=preview.actor,
                    planning_context=preview.planning_context,
                    segment_key=segment["segment_key"],
                    reviewed_fingerprint=preview.reviewed_fingerprint,
                )
            else:
                plan = save_cable_segment_override_and_replan(
                    profile=preview.profile,
                    source_document=preview.document,
                    actor=preview.actor,
                    planning_context=preview.planning_context,
                    segment_key=segment["segment_key"],
                    trace_identity=trace.trace_identity,
                    segment_index=segment["index"],
                    cable_class=segment["cable_class"],
                    data=dict(self.data),
                    reviewed_fingerprint=preview.reviewed_fingerprint,
                )
        except UnacceptablePolicyDecision as exc:
            raise PreviewCommandRefused("; ".join(exc.errors)) from exc
        settled = "cleared on" if self.clearing else "forced on"
        return CommandOutcome(message=f"Cable policy {settled} segment {segment['index'] + 1}.", plan=plan)


class TraceCableSegmentPolicyView(_TraceWorkspaceMixin, PermissionRequiredMixin, View):
    """Force one planned segment to its own Cable policy, or clear the override again."""

    permission_required = "netbox_data_import.change_importprofile"

    def post(self, request):
        """Run the override save or clear as one coordinated command."""
        command = _SetSegmentPolicy(
            trace=request.POST.get("trace", ""),
            segment=request.POST.get("segment", ""),
            clearing=bool(request.POST.get("clear")),
            data={
                "cable_type": request.POST.get("cable_type", ""),
                "cable_profile": request.POST.get("cable_profile", ""),
            },
        )
        result = apply_preview_command(request, PreviewClaim.posted(request.POST), command)
        return _command_response(request, result, self.refusal_url(request))


@dataclass(frozen=True)
class _ResolveTermination(PreviewCommand):
    """Record one operator termination decision the picker offered."""

    field_key: str
    object_type: str
    object_id: int
    search: str
    limit: int
    offset: int

    def apply(self, preview):
        """Recheck the offer, save the selection, and return the replan the coordinator stores."""
        from core.models import ObjectType

        workspace = preview.workspace
        # A review command answers a question this preview asked, never one the caller invented.
        if self.field_key not in workspace.termination_sources:
            raise PreviewCommandRefused("This preview asked no question about that termination.")
        if workspace.termination_sources[self.field_key] is None:
            raise PreviewCommandRefused(TERMINATION_UNRESOLVABLE)
        try:
            reader = _trace_reader(preview.actor, preview.profile, preview.planning_context)
            # The recheck repeats the query that made the offer, so a searched or paged candidate still counts.
            found = eligible_terminations(
                self.field_key,
                reader,
                profile=preview.profile,
                search=self.search,
                limit=self.limit,
                offset=self.offset,
            )
        except (PlanningTargetUnavailable, ValueError):
            raise PreviewCommandRefused(TERMINATION_UNRESOLVABLE) from None
        # The picker is the only legal source of a choice, so the write rechecks the offer.
        chosen = next(
            (
                candidate
                for candidate in found.candidates
                if candidate.pk == self.object_id and candidate._meta.label_lower == self.object_type
            ),
            None,
        )
        if chosen is None:
            raise PreviewCommandRefused("That termination is not one of the eligible candidates.")
        plan = save_termination_resolution_and_replan(
            profile=preview.profile,
            source_document=preview.document,
            actor=preview.actor,
            planning_context=preview.planning_context,
            task_type=SELECT_TERMINATION_TASK,
            field_key=self.field_key,
            source=workspace.termination_sources[self.field_key],
            selected_object_type=ObjectType.objects.get_for_model(type(chosen)),
            selected_object_id=chosen.pk,
            selected_display_name=str(chosen),
            device=chosen.device,
            reviewed_fingerprint=preview.reviewed_fingerprint,
        )
        return CommandOutcome(message=f"Termination resolved to '{chosen}'.", plan=plan)


class TraceResolveTerminationView(_TraceWorkspaceMixin, PermissionRequiredMixin, View):
    """Record one operator termination decision and replan the preview against it."""

    permission_required = "netbox_data_import.change_importprofile"

    def post(self, request):
        """Run the decision as one coordinated command."""
        try:
            object_id = int(request.POST.get("object_id", ""))
        except (TypeError, ValueError):
            raise PreviewCommandRefused("A termination selection names one object.") from None
        page = _candidate_page(request.POST)
        if page.error:
            raise PreviewCommandRefused(page.error)
        command = _ResolveTermination(
            field_key=request.POST.get("field_key", "").strip(),
            object_type=request.POST.get("object_type", "").strip(),
            object_id=object_id,
            search=request.POST.get("search", ""),
            limit=page.limit,
            offset=page.offset,
        )
        result = apply_preview_command(request, PreviewClaim.posted(request.POST), command)
        return _command_response(request, result, self.refusal_url(request))


class InvalidProposalId(ValueError):
    """A proposal action received no integer id."""


class InvalidProposalTarget(Exception):
    """A proposal action does not identify a termination in this preview."""


INVALID_PROPOSAL_ID_ERROR = "Enter a valid proposal_id integer."


class _TraceProposalMixin(_TraceWorkspaceMixin):
    """Answer every proposal command and read in the workspace JSON envelope."""

    permission_denied_response_format = "json"

    def dispatch(self, request, *args, **kwargs):
        """Translate domain refusals into the workspace JSON envelope."""
        from .models import ResolutionProposal
        from .proposal_tasks import UnusableCandidateSet
        from .resolution_proposals import ActiveProposalExists
        from .termination_proposal import InvalidProposalCandidate, UnsupportedProposalRole

        try:
            return super().dispatch(request, *args, **kwargs)
        except InvalidProposalId:
            return JsonResponse({"ok": False, "error": INVALID_PROPOSAL_ID_ERROR}, status=400)
        except (Http404, ResolutionProposal.DoesNotExist):
            return JsonResponse({"ok": False, "error": "That proposal is no longer available."}, status=404)
        except ActiveProposalExists as exc:
            return JsonResponse({"ok": False, "error": exc.operator_message}, status=409)
        except UnusableCandidateSet as exc:
            return JsonResponse({"ok": False, "error": exc.operator_message, "reason": exc.reason}, status=400)
        except (InvalidProposalTarget, InvalidProposalCandidate, UnsupportedProposalRole) as exc:
            logger.warning("%s: termination refused: %s", type(self).__name__, exc)
            return JsonResponse({"ok": False, "error": TERMINATION_UNRESOLVABLE}, status=400)
        except ValidationError as exc:
            return JsonResponse({"ok": False, "error": "; ".join(exc.messages)}, status=400)


PROPOSAL_QUEUE_UNAVAILABLE = "The proposal queue is unavailable. Try again later."


def _refuse_without_backend() -> None:
    """Refuse a proposal request that no Inference Backend can answer, before any attempt row exists."""
    from .proposal_presentation import backend_unavailable_reason

    if reason := backend_unavailable_reason():
        raise PreviewCommandRefused(reason, 409)


class _ProposalRequests:
    """The live NetBox reads that every proposal request of one command shares."""

    def __init__(self, preview: LockedPreview):
        self.preview = preview

    @cached_property
    def reader(self):
        """Return the operator's scoped reader for the preview's import target."""
        return _trace_reader(self.preview.actor, self.preview.profile, self.preview.planning_context)

    @cached_property
    def inventories(self):
        """Return the inventory cache that every request of this command shares."""
        from .proposal_presentation import TerminationInventories

        return TerminationInventories(profile=self.preview.profile, reader=self.reader)

    @cached_property
    def live_terminations(self) -> dict:
        """Return each termination of a plan made against live NetBox now, by field key."""
        preview = self.preview
        live = ImportEngine.plan(preview.profile, preview.document, preview.actor, preview.planning_context)
        fields: dict = {}
        for trace in ReviewWorkspace(live, preview.actor).traces:
            for item in trace.terminations:
                fields.setdefault(item["field_key"], item)
        return fields

    def queue(self, field_key: str) -> Callable[[], None]:
        """Recheck one field against live NetBox, create its attempt row and queue its Job.

        Return the compensation that fails the attempt if the queue push after the commit fails.
        """
        from core.choices import JobStatusChoices
        from core.models import Job, ObjectType

        from .cable_target import UNRESOLVED
        from .inference_backend import proposal_candidate_limit
        from .jobs import ResolutionProposalJob, import_queue_task
        from .models import ProposalFailureReason
        from .proposal_jobs import PROMPT_VERSION
        from .proposal_response import RESPONSE_SCHEMA_VERSION
        from .resolution_proposals import (
            ActiveProposalExists,
            active_proposal_exists,
            fail_queued_proposal,
            next_page_offset,
            record_proposal_job,
            request_proposal,
        )

        profile, actor = self.preview.profile, self.preview.actor
        sources = self.preview.workspace.termination_sources
        if field_key not in sources:
            raise InvalidProposalTarget("This preview asked no question about that termination.")
        if sources[field_key] is None:
            raise InvalidProposalTarget(TERMINATION_UNRESOLVABLE)
        field = self.live_terminations.get(field_key)
        if field is None:
            raise InvalidProposalTarget("This field is no longer in the preview.")
        if field["state"] != UNRESOLVED:
            raise PreviewCommandRefused("This termination is already resolved.", 409)
        # Refuse on the observed predecessor, not on the index: see active_proposal_exists.
        if active_proposal_exists(profile=profile, task_type=SELECT_TERMINATION_TASK, field_key=field_key):
            raise ActiveProposalExists("This field already has an active Resolution Proposal.")
        inventory = self.inventories.get(field_key)
        device = inventory.resolved_device
        if device is None:
            raise PreviewCommandRefused("The resolved Device is unavailable or outside your view permission.", 409)
        if inventory.candidate_error is not None:
            raise inventory.candidate_error
        snapshot = inventory.candidate_snapshot
        if snapshot is None:
            raise PreviewCommandRefused("The eligible candidates are no longer available.", 409)
        # A dense Device is searched in turns, from after the last page that used itself up.
        snapshot = snapshot.with_page(
            offset=next_page_offset(
                profile=profile, task_type=SELECT_TERMINATION_TASK, field_key=field_key, inventory=inventory
            ),
            size=proposal_candidate_limit(),
        )
        proposal = request_proposal(
            profile=profile,
            task_type=SELECT_TERMINATION_TASK,
            field_key=field_key,
            source_evidence={**parse_termination_field_key(field_key), "label": field["label"]},
            resolved_device_type=ObjectType.objects.get_for_model(device),
            resolved_device_id=device.pk,
            prompt_version=PROMPT_VERSION,
            response_schema_version=RESPONSE_SCHEMA_VERSION,
            candidate_snapshot=snapshot,
            requested_by=actor,
        )
        # The low queue lets a trace sync queued later start before minutes of inference.
        job = ResolutionProposalJob.enqueue(
            name=ResolutionProposalJob.Meta.name, user=actor, queue_name=RQ_QUEUE_LOW, proposal_id=proposal.pk
        )
        record_proposal_job(proposal.pk, job)

        def compensate():
            # The push runs after commit, so the attempt fails on its own row rather than staying queued.
            try:
                pushed = import_queue_task(job) is not None
            except (RedisConnectionError, RedisTimeoutError):
                # A queue that cannot answer cannot show the task, so the attempt fails as before.
                pushed = False
            # A failed push stops only the later pushes, so a task that reached the queue keeps its attempt.
            if pushed:
                return
            # A worker that took the attempt owns it, so only an attempt still queued fails here.
            if fail_queued_proposal(proposal.pk, reason=ProposalFailureReason.QUEUE_UNAVAILABLE):
                Job.objects.filter(pk=job.pk, status=JobStatusChoices.STATUS_PENDING).update(
                    status=JobStatusChoices.STATUS_ERRORED
                )

        return compensate


@dataclass(frozen=True)
class _RequestProposal(PreviewCommand):
    """Freeze one unresolved field's evidence and queue its inference; the plan itself does not change."""

    field_key: str
    replans = False
    # The plan is unchanged, so every claim the page holds stays current and the next field can ask.
    advances = False
    reviews_policy = False

    def apply(self, preview):
        """Queue one proposal for the field."""
        _refuse_without_backend()
        compensate = _ProposalRequests(preview).queue(self.field_key)
        return CommandOutcome(payload={"field_key": self.field_key}, compensate=compensate)


@dataclass(frozen=True)
class _RequestAllProposals(PreviewCommand):
    """Queue a proposal for every open termination of the preview that can be asked now."""

    replans = False
    advances = False
    reviews_policy = False

    def apply(self, preview: LockedPreview) -> CommandOutcome:
        """Ask about each open termination once, and count each refusal by its reason instead of stopping."""
        from .cable_target import UNRESOLVED
        from .field_keys import TERMINATION_ROLE
        from .proposal_tasks import UnusableCandidateSet
        from .resolution_proposals import ActiveProposalExists
        from .termination_proposal import InvalidProposalCandidate, UnsupportedProposalRole

        _refuse_without_backend()
        fields = dict.fromkeys(
            item["field_key"]
            for trace in preview.workspace.traces
            for item in trace.terminations
            if item.get("state", UNRESOLVED) == UNRESOLVED
            and parse_termination_field_key(item["field_key"])["role"] == TERMINATION_ROLE
        )
        requests = _ProposalRequests(preview)
        compensations: list[Callable[[], None]] = []
        skipped: Counter[str] = Counter()
        for field_key in fields:
            try:
                compensations.append(requests.queue(field_key))
            except (InvalidProposalTarget, InvalidProposalCandidate, UnsupportedProposalRole):
                skipped[TERMINATION_UNRESOLVABLE] += 1
            except (PreviewCommandRefused, ActiveProposalExists, UnusableCandidateSet) as exc:
                skipped[exc.operator_message] += 1

        def compensate():
            for each in compensations:
                each()

        return CommandOutcome(payload={"queued": len(compensations), "skipped": skipped}, compensate=compensate)


class TraceRequestProposalView(_TraceProposalMixin, PermissionRequiredMixin, View):
    """Freeze one unresolved field's evidence before dispatching inference."""

    permission_required = "netbox_data_import.change_importprofile"

    def post(self, request):
        """Create the attempt row, queue a job carrying its id alone, and answer with the field's card."""
        field_key = request.POST.get("field_key", "").strip()
        try:
            result = apply_preview_command(
                request, PreviewClaim.posted(request.POST), _RequestProposal(field_key=field_key)
            )
        except (RedisConnectionError, RedisTimeoutError):
            logger.exception("Failed to enqueue a resolution proposal")
            return JsonResponse({"ok": False, "error": PROPOSAL_QUEUE_UNAVAILABLE}, status=503)
        return _proposal_card_after(request, result, field_key)


class TraceRequestAllProposalsView(_TraceWorkspaceMixin, PermissionRequiredMixin, View):
    """Ask AI about every open termination of the preview, and report what it skipped and why."""

    permission_required = "netbox_data_import.change_importprofile"

    def post(self, request):
        """Queue the proposals in one command, then show the workspace with the outcome."""
        try:
            result = apply_preview_command(request, PreviewClaim.posted(request.POST), _RequestAllProposals())
        except (RedisConnectionError, RedisTimeoutError):
            logger.exception("Failed to enqueue resolution proposals")
            return self._refusal(request, PROPOSAL_QUEUE_UNAVAILABLE, 503)
        queued, skipped = result.outcome.payload["queued"], result.outcome.payload["skipped"]
        if queued:
            messages.success(request, f"Asked AI about {queued} termination{pluralize(queued)}.")
        if skipped:
            total = sum(skipped.values())
            reasons = "; ".join(f"{reason} ({count})" for reason, count in skipped.items())
            messages.warning(request, f"Skipped {total} termination{pluralize(total)}: {reasons}")
        if not queued and not skipped:
            messages.info(request, "No open termination needs a proposal.")
        return redirect(_trace_workspace_url(request.POST.get("trace", "")))


class TraceProposalView(_TraceProposalMixin, PermissionRequiredMixin, View):
    """Read all attempts for a field, independent of their originating plans."""

    permission_required = "netbox_data_import.view_importprofile"

    def get(self, request):
        """Answer with the field's card as it stands now, which a pending card polls."""
        snapshot = _claimed_snapshot(request, profile_action="view")
        return _proposal_card(request, snapshot, request.GET.get("field_key", "").strip(), request.GET.get("trace", ""))


@dataclass(frozen=True)
class _ProposalAction(PreviewCommand):
    """Apply one operator action to a proposal of this preview's profile."""

    proposal_id: int
    replans = False
    # An action that leaves the plan unchanged keeps every claim on the page current.
    advances = False
    reviews_policy = False
    message: ClassVar[str] = ""

    def apply(self, preview):
        """Refuse an unavailable attempt or a transition another operator already took."""
        from .models import ResolutionProposal

        proposal = ResolutionProposal.objects.get(
            pk=self.proposal_id, profile=preview.profile, task_type=SELECT_TERMINATION_TASK
        )
        if proposal.field_key not in preview.workspace.termination_sources:
            raise InvalidProposalTarget("This preview asked no question about that termination.")
        if not self.decide(proposal, preview):
            raise PreviewCommandRefused(
                "This proposal no longer permits that action. Re-read it before continuing.", 409
            )
        return CommandOutcome(message=self.message, payload={"field_key": proposal.field_key})

    def decide(self, proposal, preview) -> bool:
        """Apply the action, returning whether this caller moved the proposal."""
        raise NotImplementedError


class _TraceProposalActionView(_TraceProposalMixin, PermissionRequiredMixin, View):
    """Run one proposal action as a coordinated command."""

    permission_required = "netbox_data_import.change_importprofile"
    command_class: type[_ProposalAction]

    def post(self, request):
        """Run the action, then answer with what it changed."""
        try:
            proposal_id = int(request.POST.get("proposal_id", ""))
        except ValueError:
            raise InvalidProposalId(INVALID_PROPOSAL_ID_ERROR) from None
        result = apply_preview_command(request, PreviewClaim.posted(request.POST), self.command_class(proposal_id))
        return self.answer(request, result)

    def answer(self, request, result):
        """Answer with the card the action changed, because the plan and the claim stay the same."""
        return _proposal_card_after(request, result, result.outcome.payload["field_key"])


@dataclass(frozen=True)
class _CancelProposal(_ProposalAction):
    """Cancel active work, for any operator with preview and resolved Device access."""

    def decide(self, proposal, preview):
        """Cancel through the lifecycle's conditional transition."""
        from .proposal_tasks import proposal_task
        from .resolution_proposals import cancel_proposal

        reader = _trace_reader(preview.actor, preview.profile, preview.planning_context)
        if (
            proposal_task(proposal.task_type).resolved_device(
                profile=proposal.profile, field_key=proposal.field_key, netbox_reader=reader
            )
            is None
        ):
            raise ObjectPermissionDenied("dcim.view_device")
        return cancel_proposal(proposal.pk)


@dataclass(frozen=True)
class _AcceptProposal(_ProposalAction):
    """Write the accepted resolution; the coordinator replans in the same transaction."""

    replans = True
    advances = True
    reviews_policy = True
    message = "The proposal was accepted and the preview was replanned."

    def decide(self, proposal, preview):
        """Record the accepted resolution through the existing acceptance transaction."""
        from .proposal_decisions import accept_proposal

        if preview.workspace.termination_sources[proposal.field_key] is None:
            raise InvalidProposalTarget(TERMINATION_UNRESOLVABLE)
        return accept_proposal(
            proposal.pk,
            source=preview.workspace.termination_sources[proposal.field_key],
            operator=preview.actor,
            netbox_reader=_trace_reader(preview.actor, preview.profile, preview.planning_context),
            reviewed_fingerprint=preview.reviewed_fingerprint,
        )


@dataclass(frozen=True)
class _RejectProposal(_ProposalAction):
    """Record the explicit rejection without binding it to the requesting operator."""

    profile_action = "view"

    def decide(self, proposal, preview):
        """Reject through the permission-checked decision service."""
        from .proposal_decisions import reject_proposal

        return reject_proposal(proposal.pk, operator=preview.actor)


class TraceCancelProposalView(_TraceProposalActionView):
    """Allow any operator with preview and resolved Device access to cancel active work."""

    command_class = _CancelProposal


class TraceAcceptProposalView(_TraceProposalActionView):
    """Accept a proposal and replan the preview in one coordinated transaction."""

    command_class = _AcceptProposal

    def answer(self, request, result):
        """Show the replanned workspace, whose every form carries the claim the acceptance advanced to."""
        return _command_response(request, result, _trace_workspace_url(request.POST.get("trace", "")))


class TraceRejectProposalView(_TraceProposalActionView):
    """Record the explicit rejection without binding it to the requesting operator."""

    permission_required = "netbox_data_import.view_importprofile"
    command_class = _RejectProposal


@dataclass(frozen=True)
class _MapManufacturer(PreviewCommand):
    """Save a ManufacturerMapping (source make to NetBox manufacturer slug) from the preview page."""

    source_make: str
    netbox_mfg_slug: str

    def apply(self, preview):
        """Validate and save the mapping."""
        mapping = _get_or_init(ManufacturerMapping, profile=preview.profile, source_make=self.source_make)
        mapping.netbox_manufacturer_slug = self.netbox_mfg_slug
        _validate_model_instance(mapping, f"manufacturer mapping '{self.source_make}'")
        result = save_permission_scoped_object(
            preview.actor,
            ManufacturerMapping,
            {"profile": preview.profile, "source_make": self.source_make},
            {"netbox_manufacturer_slug": self.netbox_mfg_slug},
        )
        verb = "Created" if result.created else "Updated"
        return CommandOutcome(message=f"{verb} manufacturer mapping: '{self.source_make}' → {self.netbox_mfg_slug}")


class QuickResolveManufacturerView(_PreviewCommandMixin, PermissionRequiredMixin, View):
    """Save a ManufacturerMapping (source make → NetBox manufacturer slug) from the preview page."""

    permission_required = "netbox_data_import.change_importprofile"

    def post(self, request):
        """Save the manufacturer mapping and replan the preview."""
        source_make = " ".join(request.POST.get("source_make", "").split())
        netbox_mfg_slug = request.POST.get("netbox_mfg_slug", "").strip()
        if not source_make or not netbox_mfg_slug:
            raise PreviewCommandRefused("Source make and NetBox manufacturer slug are required.")
        command = _MapManufacturer(source_make=source_make, netbox_mfg_slug=netbox_mfg_slug)
        result = apply_preview_command(request, PreviewClaim.posted(request.POST), command)
        return _command_response(request, result, reverse("plugins:netbox_data_import:import_preview"))

    def refusal_url(self, request):
        """Return the preview the quick fix came from."""
        return reverse("plugins:netbox_data_import:import_preview")


@dataclass(frozen=True)
class _MapDeviceType(PreviewCommand):
    """Save a DeviceTypeMapping (source make and model to NetBox slugs) from the preview page."""

    source_make: str
    source_model: str
    netbox_mfg_slug: str
    netbox_dt_slug: str

    def apply(self, preview):
        """Validate and save the mapping."""
        mapping = _get_or_init(
            DeviceTypeMapping, profile=preview.profile, source_make=self.source_make, source_model=self.source_model
        )
        mapping.netbox_manufacturer_slug = self.netbox_mfg_slug
        mapping.netbox_device_type_slug = self.netbox_dt_slug
        _validate_model_instance(mapping, f"device type mapping '{self.source_make} / {self.source_model}'")
        result = save_permission_scoped_object(
            preview.actor,
            DeviceTypeMapping,
            {"profile": preview.profile, "source_make": self.source_make, "source_model": self.source_model},
            {"netbox_manufacturer_slug": self.netbox_mfg_slug, "netbox_device_type_slug": self.netbox_dt_slug},
        )
        verb = "created" if result.created else "updated"
        return CommandOutcome(
            message=(
                f"DeviceType mapping {verb}: '{self.source_make} / {self.source_model}' "
                f"→ {self.netbox_mfg_slug}/{self.netbox_dt_slug}"
            )
        )


class QuickResolveDeviceTypeView(_PreviewCommandMixin, PermissionRequiredMixin, View):
    """Save a DeviceTypeMapping (source make/model → NetBox slugs) from the preview page."""

    permission_required = "netbox_data_import.change_importprofile"

    def post(self, request):
        """Save the device type mapping and replan the preview."""
        from .device_identity import default_identity_slugs

        source_make = " ".join(request.POST.get("source_make", "").split())
        source_model = " ".join(request.POST.get("source_model", "").split())
        if not source_make or not source_model:
            raise PreviewCommandRefused("Source make and model are required.")
        if request.POST.get("action", "map") != "map":
            raise PreviewCommandRefused("The requested Device Type action is not supported.")
        # The importer derives both slugs the same way, so a default that differs maps to nothing.
        default_mfg_slug, default_dt_slug = default_identity_slugs(source_make, source_model)
        command = _MapDeviceType(
            source_make=source_make,
            source_model=source_model,
            netbox_mfg_slug=request.POST.get("netbox_mfg_slug", "").strip() or default_mfg_slug,
            netbox_dt_slug=request.POST.get("netbox_dt_slug", "").strip() or default_dt_slug,
        )
        result = apply_preview_command(request, PreviewClaim.posted(request.POST), command)
        return _command_response(request, result, reverse("plugins:netbox_data_import:import_preview"))

    def refusal_url(self, request):
        """Return the preview the quick fix came from."""
        return reverse("plugins:netbox_data_import:import_preview")


@dataclass(frozen=True)
class _MapClassRole(PreviewCommand):
    """Save a ClassRoleMapping (ignore, role or rack) from an error row of the preview."""

    source_class: str
    mapping_action: str
    role_slug: str
    rack_type_id: int | None

    def apply(self, preview):
        """Validate and save the mapping."""
        from dcim.models import RackType

        rack_type = None
        if self.rack_type_id is not None:
            rack_type = RackType.objects.filter(pk=self.rack_type_id).first()
            if rack_type is None:
                raise PreviewCommandRefused(
                    f"Invalid rack type selected for class '{self.source_class}'. Please choose a valid rack type."
                )
        values = {
            "ignore": self.mapping_action == "ignore",
            "creates_rack": self.mapping_action == "rack",
            "rack_type": rack_type,
            "role_slug": self.role_slug if self.mapping_action == "role" else "",
        }
        mapping = _get_or_init(ClassRoleMapping, profile=preview.profile, source_class=self.source_class)
        for field_name, value in values.items():
            setattr(mapping, field_name, value)
        _validate_model_instance(mapping, f"class role mapping '{self.source_class}'")
        result = save_permission_scoped_object(
            preview.actor,
            ClassRoleMapping,
            {"profile": preview.profile, "source_class": self.source_class},
            values,
        )
        verb = "Created" if result.created else "Updated"
        if self.mapping_action == "ignore":
            action_label = "ignore"
        elif self.mapping_action == "rack":
            action_label = f"creates rack{f' (type: {rack_type})' if rack_type else ''}"
        else:
            action_label = f"role '{self.role_slug}'"
        return CommandOutcome(message=f"{verb} mapping: class '{self.source_class}' → {action_label}")


class QuickAddClassRoleMappingView(_PreviewCommandMixin, PermissionRequiredMixin, View):
    """Quickly add a ClassRoleMapping (ignore / role) directly from an error row in preview."""

    permission_required = "netbox_data_import.change_importprofile"

    def post(self, request):
        """Save the class-to-role mapping and replan the preview."""
        source_class = request.POST.get("source_class", "").strip()
        mapping_action = request.POST.get("mapping_action", "ignore")  # "ignore", "role", or "rack"
        role_slug = request.POST.get("role_slug", "").strip()
        raw_rack_type = request.POST.get("rack_type_id", "").strip()
        rack_type_id = None
        if mapping_action == "rack" and raw_rack_type:
            try:
                rack_type_id = int(raw_rack_type)
            except (TypeError, ValueError):
                raise PreviewCommandRefused(
                    f"Invalid rack type selected for class '{source_class}'. Please choose a valid rack type."
                ) from None
        if not source_class:
            raise PreviewCommandRefused("Source class is required.")
        valid_actions = ("ignore", "role", "rack")
        if mapping_action not in valid_actions:
            raise PreviewCommandRefused(
                f"Invalid mapping action '{mapping_action}'. Must be one of: {', '.join(valid_actions)}."
            )
        if mapping_action == "role" and not role_slug:
            raise PreviewCommandRefused("A role slug is required when mapping action is 'role'.")
        command = _MapClassRole(
            source_class=source_class, mapping_action=mapping_action, role_slug=role_slug, rack_type_id=rack_type_id
        )
        result = apply_preview_command(request, PreviewClaim.posted(request.POST), command)
        return _command_response(request, result, reverse("plugins:netbox_data_import:import_preview"))

    def refusal_url(self, request):
        """Return the preview the quick fix came from."""
        return reverse("plugins:netbox_data_import:import_preview")


@dataclass(frozen=True)
class _MapColumn(PreviewCommand):
    """Map one unmapped source column to a NetBox target field from the preview panel."""

    source_column: str
    target_field: str

    def apply(self, preview):
        """Validate and save the mapping, replacing the column that supplied a direct target before."""
        if not CATALOG.is_valid(self.target_field, output_kinds=preview.profile.output_kinds):
            raise PreviewCommandRefused("Valid source column and target field are required.")
        # The catalog accepts any non-empty name after a family prefix, so it cannot bound length.
        # Validate before the displaced row is deleted: an invalid write must strand nothing.
        _validate_model_instance(
            ColumnMapping(profile=preview.profile, source_column=self.source_column, target_field=self.target_field),
            f"column mapping '{self.source_column}' -> {self.target_field}",
        )
        lookup = {"profile": preview.profile, "source_column": self.source_column, "target_field": self.target_field}
        if self.target_field.startswith(CANDIDATE_TARGET_PREFIX):
            result = save_permission_scoped_object(preview.actor, ColumnMapping, lookup, {}, on_existing="keep")
            verb = "Created" if result.created else "Kept"
            return CommandOutcome(message=f"{verb} candidate mapping: '{self.source_column}' → {self.target_field}")
        # A quick direct mapping replaces the source column that supplied the target before it.
        displaced = ColumnMapping.objects.filter(profile=preview.profile, target_field=self.target_field).exclude(
            source_column=self.source_column
        )
        displaced_source = displaced.values_list("source_column", flat=True).first()
        delete_permission_scoped_objects(preview.actor, displaced)
        result = save_permission_scoped_object(preview.actor, ColumnMapping, lookup, {}, on_existing="keep")
        if displaced_source:
            return CommandOutcome(
                message=(
                    f"Reassigned: '{self.source_column}' → {self.target_field} "
                    f"(previously mapped from '{displaced_source}')"
                )
            )
        verb = "Created" if result.created else "Kept"
        return CommandOutcome(message=f"{verb} mapping: '{self.source_column}' → {self.target_field}")


class QuickAddColumnMappingView(_PreviewCommandMixin, PermissionRequiredMixin, View):
    """Quickly map an unmapped source column to a NetBox target field from the preview panel."""

    permission_required = "netbox_data_import.change_importprofile"

    def post(self, request):
        """Save the column mapping and replan the preview."""
        source_column = request.POST.get("source_column", "").strip()
        target_field = request.POST.get("target_field", "").strip()
        if not source_column or not target_field:
            raise PreviewCommandRefused("Valid source column and target field are required.")
        command = _MapColumn(source_column=source_column, target_field=target_field)
        result = apply_preview_command(request, PreviewClaim.posted(request.POST), command)
        return _command_response(request, result, reverse("plugins:netbox_data_import:import_preview"))

    def refusal_url(self, request):
        """Return the preview the quick fix came from."""
        return reverse("plugins:netbox_data_import:import_preview")


@dataclass(frozen=True)
class _MatchExistingDevice(PreviewCommand):
    """Link one source row to an existing NetBox Device, so the next plan updates that Device."""

    source_id: str
    netbox_device_id: str

    def apply(self, preview):
        """Check the Device against the import site and the profile's other links, then save the link."""
        from dcim.models import Device

        source_row = _source_row(preview.workspace, self.source_id)
        try:
            device = Device.objects.restrict(preview.actor, "view").get(pk=int(self.netbox_device_id))
        except (Device.DoesNotExist, ValueError):
            raise PreviewCommandRefused(f"Device #{self.netbox_device_id} not found.") from None
        if device.site_id != preview.planning_context["site_id"]:
            raise PreviewCommandRefused("The selected device is outside the active import site.")
        conflicting = preview.profile.device_matches.filter(netbox_device_id=device.pk).exclude(
            source_id=self.source_id
        )
        if conflicting_match := conflicting.first():
            raise PreviewCommandRefused(
                f"Device '{device.name}' is already linked to source '{conflicting_match.source_id}'."
            )
        try:
            save_permission_scoped_object(
                preview.actor,
                DeviceExistingMatch,
                {"profile": preview.profile, "source_id": self.source_id},
                {
                    "netbox_device_id": device.pk,
                    "device_name": device.name,
                    "source_asset_tag": source_text(source_row.get("asset_tag"))[:50],
                },
            )
        except ObjectPermissionDenied as exc:
            raise PreviewCommandRefused("Permission denied: cannot create or change this device link.", 403) from exc
        except ValidationError as exc:
            raise PreviewCommandRefused("; ".join(exc.messages)) from exc
        return CommandOutcome(message=f"Source '{self.source_id}' linked to existing device '{device.name}'.")


class MatchExistingDeviceView(_PreviewCommandMixin, PermissionRequiredMixin, View):
    """Link a source row to an existing NetBox device (by device ID)."""

    permission_required = (
        "netbox_data_import.change_importprofile",
        "dcim.view_device",
    )

    def post(self, request):
        """Save the device match and go back to the replanned preview."""
        source_id = source_text(request.POST.get("source_id"))
        netbox_device_id = request.POST.get("netbox_device_id", "").strip()
        if not source_id or not netbox_device_id:
            raise PreviewCommandRefused("source_id and netbox_device_id are required.")
        command = _MatchExistingDevice(source_id=source_id, netbox_device_id=netbox_device_id)
        result = apply_preview_command(request, PreviewClaim.posted(request.POST), command)
        return _command_response(request, result, reverse("plugins:netbox_data_import:import_preview"))

    def refusal_url(self, request):
        """Return the preview the link came from."""
        return reverse("plugins:netbox_data_import:import_preview")


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


@dataclass(frozen=True)
class _CreateDeviceRole(PreviewCommand):
    """Create one DeviceRole from the Configure Class modal; no profile policy or plan changes."""

    name: str
    slug: str
    color: str
    replans = False
    reviews_policy = False
    reads_plan = False

    def apply(self, preview):
        """Create the role, or keep one that already holds the slug."""
        from dcim.models import DeviceRole

        role = _get_or_init(DeviceRole, slug=self.slug)
        if role.pk is None:
            role.name = self.name
            role.color = self.color
            role.full_clean(validate_unique=False)
        result = save_permission_scoped_object(
            preview.actor, DeviceRole, {"slug": self.slug}, {"name": self.name, "color": self.color}, on_existing="keep"
        )
        role = result.instance
        return CommandOutcome(payload={"id": role.pk, "name": role.name, "slug": role.slug, "created": result.created})


class QuickCreateDeviceRoleView(_PreviewCommandMixin, _AjaxPermissionView):
    """AJAX endpoint: create a new DeviceRole and return its details as JSON.

    Used by the Configure Class modal so operators can create missing roles
    without leaving the import preview page.
    """

    permission_required = "netbox_data_import.change_importprofile"

    def post(self, request):
        """Create the DeviceRole and return JSON {id, name, slug}."""
        import re

        name = request.POST.get("name", "").strip()
        slug = request.POST.get("slug", "").strip()
        color = request.POST.get("color", "9e9e9e").strip() or "9e9e9e"
        if not name or not slug:
            return JsonResponse({"error": "Role name and slug are required."}, status=400)
        if not re.match(r"^[-a-z0-9_]+$", slug):
            return JsonResponse(
                {"error": "Slug may only contain lowercase letters, numbers, hyphens, and underscores."}, status=400
            )
        try:
            result = apply_preview_command(
                request, PreviewClaim.posted(request.POST), _CreateDeviceRole(name=name, slug=slug, color=color)
            )
        except IntegrityError:
            logger.exception("QuickCreateDeviceRoleView: integrity error creating role slug=%s", slug)
            return JsonResponse({"error": "A device role with that slug already exists."}, status=400)
        except ValidationError:
            logger.exception("QuickCreateDeviceRoleView: validation error creating role slug=%s", slug)
            return JsonResponse({"error": "Invalid role data."}, status=400)
        except DatabaseError:
            logger.exception("QuickCreateDeviceRoleView: database error creating role slug=%s", slug)
            return JsonResponse({"error": "An internal error occurred."}, status=500)
        messages.success(request, f"Device role '{result.outcome.payload['name']}' is available.")
        return JsonResponse(dict(result.outcome.payload))


@dataclass(frozen=True)
class _AutoMatchDevices(PreviewCommand):
    """Save every safe exact Device match for the preview's eligible source rows."""

    def apply(self, preview):
        """Match against the import target the preview names, as the operator sees it now."""
        from .review_workspace import auto_match_devices

        target = _resolved_import_target(preview.planning_context, preview.actor)
        if target is None:
            raise PreviewCommandRefused(TARGET_GONE, 409)
        summary = auto_match_devices(preview.workspace, preview.profile, preview.actor, target)
        return CommandOutcome(message=summary.message())


class AutoMatchDevicesView(_PreviewCommandMixin, PermissionRequiredMixin, View):
    """Run the Review Workspace device auto-match command."""

    permission_required = (
        "netbox_data_import.change_importprofile",
        "netbox_data_import.add_deviceexistingmatch",
        "dcim.view_device",
    )

    def post(self, request):
        """Run auto-matching and go back to the replanned preview with a summary message."""
        result = apply_preview_command(request, PreviewClaim.posted(request.POST), _AutoMatchDevices())
        return _command_response(request, result, reverse("plugins:netbox_data_import:import_preview"))

    def refusal_url(self, request):
        """Return the preview the command came from."""
        return reverse("plugins:netbox_data_import:import_preview")


@dataclass(frozen=True)
class _SyncSingleRow(PreviewCommand):
    """Execute one row's Synchronization Unit and replan inside the execution savepoint."""

    row_number: int

    def apply(self, preview):
        """Run the selection; a failure keeps its audit row and leaves the preview as it was."""
        unit = next(
            (
                unit
                for unit in preview.workspace.units
                if unit.row_number == self.row_number and unit.object_type in {"device", "rack"}
            ),
            None,
        )
        if unit is None:
            raise PreviewCommandRefused("Row not found in current preview data")
        if unit.action not in {"create", "update"}:
            raise PreviewCommandRefused("Only 'create' and 'update' rows can be synced individually")
        try:
            _execution, plan = ImportEngine.execute_and_replan(
                preview.profile,
                preview.document,
                preview.plan.to_dict(),
                [unit.identity],
                uuid.uuid4().hex,
                preview.actor,
                validate_replan=validate_preview_plan,
            )
        except Exception as exc:  # noqa: BLE001 - the coordinator commits the failed audit, then raises this
            # The savepoint rolled back, so NetBox must not send the events its writes queued.
            clear_events.send(sender=type(self))
            return CommandOutcome(refusal=_RowWriteRefused(_refused_row_write_response(exc, self.row_number)))
        verb = "created" if unit.action == "create" else "updated"
        return CommandOutcome(
            message="Synchronized.",
            payload={"detail": f"{unit.object_type.capitalize()} '{unit.name}' was {verb} in NetBox."},
            plan=plan,
        )


class _RowWriteRefused(Exception):
    """A single-row execution that failed after its audit row was written, carrying the answer for it."""

    def __init__(self, response):
        super().__init__("The row synchronization was refused.")
        self.response = response


def _refused_row_write_response(exc, row_number):
    """Return the answer one refused row write gives the operator.

    The worker reports the same failures, so both read the message from one place.
    """
    if isinstance(exc, PreviewCommandRefused):
        return JsonResponse({"ok": False, "error": exc.operator_message}, status=exc.status)
    if isinstance(exc, PlanError):
        logger.warning("SyncSingleRowView: the Import Plan for row_number=%s is unreadable.", row_number, exc_info=exc)
        return JsonResponse({"ok": False, "error": UNREADABLE_PREVIEW}, status=409)
    if isinstance(exc, (PlanningTargetUnavailable, PreconditionFailed, SelectionError, StalePlan, StaleSourceDocument)):
        return JsonResponse({"ok": False, "error": exc.operator_message}, status=409)
    if isinstance(exc, (DatabaseError, ObjectPermissionDenied, ValidationError)):
        if isinstance(exc, DatabaseError):
            logger.error("SyncSingleRowView: database error for row_number=%s", row_number, exc_info=exc)
        return JsonResponse({"ok": False, "error": operator_failure_message(exc)}, status=400)
    logger.error("SyncSingleRowView: unexpected error for row_number=%s", row_number, exc_info=exc)
    return JsonResponse({"ok": False, "error": "An unexpected error occurred. See server logs."}, status=500)


class SyncSingleRowView(_PreviewCommandMixin, _AjaxPermissionView):
    """AJAX endpoint: execute a single row of the active preview.

    POST body: the preview claim and row_number=<int>
    Returns the preview-command JSON envelope.
    """

    permission_required = "netbox_data_import.change_importprofile"

    def post(self, request):
        """Execute one selected Synchronization Unit and return JSON."""
        raw_row_number = request.POST.get("row_number")
        if raw_row_number is None:
            return JsonResponse({"ok": False, "error": "row_number is required"}, status=400)
        try:
            row_number = int(raw_row_number)
        except (TypeError, ValueError):
            return JsonResponse({"ok": False, "error": "Invalid row number"}, status=400)
        try:
            result = apply_preview_command(request, PreviewClaim.posted(request.POST), _SyncSingleRow(row_number))
        except _RowWriteRefused as refused:
            return refused.response
        messages.success(request, f"{result.outcome.message} {result.outcome.payload['detail']}")
        return JsonResponse(
            {
                "ok": True,
                "row_number": row_number,
                "preview_state": "replanned",
                "message": result.outcome.message,
                "detail": result.outcome.payload["detail"],
            }
        )


@dataclass(frozen=True)
class _UnlinkDevice(PreviewCommand):
    """Remove one source row's Device link, and the field reviews that rest on it."""

    source_id: str

    def apply(self, preview):
        """Delete the link and its reviews under their row locks, refusing either the actor may not delete."""
        _source_row(preview.workspace, self.source_id)
        binding = (
            DeviceExistingMatch.objects.select_for_update()
            .filter(profile=preview.profile, source_id=self.source_id)
            .first()
        )
        reviews = list(
            IgnoredFieldDifference.objects.select_for_update().filter(profile=preview.profile, source_id=self.source_id)
        )
        if binding is not None and not preview.actor.has_perm("netbox_data_import.delete_deviceexistingmatch", binding):
            raise PreviewCommandRefused("Permission denied: cannot delete this device link.", 403)
        if any(not preview.actor.has_perm("netbox_data_import.delete_ignoredfielddifference", r) for r in reviews):
            raise PreviewCommandRefused("Permission denied: cannot remove the dependent field reviews.", 403)
        if binding is None and not reviews:
            raise PreviewCommandRefused(f"Source '{self.source_id}' has no device link to remove.")
        if reviews:
            IgnoredFieldDifference.objects.filter(pk__in=[review.pk for review in reviews]).delete()
        if binding is not None:
            binding.delete()
        if reviews:
            return CommandOutcome(
                message=f"Unlinked source '{self.source_id}' and removed {len(reviews)} field review(s)."
            )
        return CommandOutcome(message=f"Unlinked source '{self.source_id}'.")


class UnlinkDeviceView(_PreviewCommandMixin, _AjaxPermissionView):
    """Remove a DeviceExistingMatch (unlink a manually-linked device)."""

    permission_required = "netbox_data_import.delete_deviceexistingmatch"
    permission_denied_response_format = "redirect"

    def post(self, request):
        """Delete the DeviceExistingMatch and go back to the replanned preview."""
        command = _UnlinkDevice(source_id=source_text(request.POST.get("source_id")))
        result = apply_preview_command(request, PreviewClaim.posted(request.POST), command)
        return _command_response(request, result, self.refusal_url(request))
