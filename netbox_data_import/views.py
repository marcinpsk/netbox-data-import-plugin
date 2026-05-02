# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
import difflib
import logging

from django.contrib import messages
from django.contrib.auth.mixins import PermissionRequiredMixin
from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from netbox.views import generic
from utilities.permissions import get_permission_for_model

from .filters import ImportProfileFilterSet
from .forms import (
    ClassRoleMappingForm,
    ColumnMappingForm,
    ColumnTransformRuleForm,
    DeviceTypeMappingForm,
    ImportProfileForm,
    ImportProfileImportForm,
    ImportSetupForm,
)
from .models import (
    ClassRoleMapping,
    ColumnMapping,
    ColumnTransformRule,
    DeviceExistingMatch,
    DeviceTypeMapping,
    ImportJob,
    ImportProfile,
    ManufacturerMapping,
    SourceResolution,
    TARGET_FIELD_CHOICES,
)
from .tables import (
    ClassRoleMappingTable,
    ColumnMappingTable,
    ColumnTransformRuleTable,
    DeviceTypeMappingTable,
    ImportJobTable,
    ImportProfileTable,
)


def _safe_next_url(request, fallback: str) -> str:
    """Return a validated same-host redirect URL from POST or the fallback view name."""
    url = request.POST.get("next", "")
    if url and url_has_allowed_host_and_scheme(
        url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return url
    return reverse(fallback)


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


# Scalar profile fields handled by _apply_profile_yaml_data.
# 'tags' (M2M) is intentionally excluded — use the edit UI or the flat import path.
_PROFILE_FIELDS = (
    "description",
    "sheet_name",
    "source_id_column",
    "custom_field_name",
    "update_existing",
    "create_missing_device_types",
    "preview_view_mode",
)


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
        raise ValueError(f"Validation error in {label}: {msg}") from exc


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
    """Persist *instance*; on IntegrityError from a concurrent insert, refetch the winner.

    Uses a savepoint so the IntegrityError does not abort the outer transaction.
    """
    from django.db import IntegrityError, transaction

    try:
        with transaction.atomic():
            instance.save()
    except IntegrityError:
        instance = model_class.objects.filter(**lookup).first()
    return instance


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

    with transaction.atomic():
        # Only include fields that are explicitly present in the YAML so that a
        # partial reimport (e.g. just trimming child sections) does not silently
        # reset unrelated profile settings back to hard-coded defaults.
        profile_defaults = {f: pdata[f] for f in _PROFILE_FIELDS if f in pdata}
        profile = _get_or_init(ImportProfile, name=pdata["name"])
        for field, value in profile_defaults.items():
            setattr(profile, field, value)
        _validate_model_instance(profile, "profile")
        profile = _save_or_refetch(profile, ImportProfile, name=pdata["name"])

        stats = {}

        cm_target_fields = []
        for cm in _iter_yaml_section(data, "column_mappings", ("target_field", "source_column")):
            instance = _get_or_init(ColumnMapping, profile=profile, target_field=cm["target_field"])
            instance.source_column = cm["source_column"]
            _validate_model_instance(instance, f"column_mappings[{cm['target_field']}]")
            _save_or_refetch(instance, ColumnMapping, profile=profile, target_field=cm["target_field"])
            cm_target_fields.append(cm["target_field"])
            stats["column_mappings"] = stats.get("column_mappings", 0) + 1
        if "column_mappings" in data:
            ColumnMapping.objects.filter(profile=profile).exclude(target_field__in=cm_target_fields).delete()

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
        for r in _iter_yaml_section(data, "column_transform_rules", ("source_column", "pattern")):
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
                return redirect(request.path)
        else:
            raw = request.POST.get("data", "").strip()

        if not raw:
            messages.error(request, "No data provided.")
            return redirect(request.path)

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
                return redirect(request.path)
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


class _ProfileChildEditView(PermissionRequiredMixin, generic.ObjectEditView):
    """Base add/edit view for objects that belong to an ImportProfile.

    Handles pre-populating the hidden ``profile`` field on add (via the
    ``profile_pk`` URL kwarg) and redirecting back to the parent profile
    detail page after a successful save.

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
            obj.profile = get_object_or_404(ImportProfile, pk=url_kwargs["profile_pk"])
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
            return {"profile": get_object_or_404(ImportProfile, pk=profile_pk)}
        return {}


class _ProfileChildDeleteView(PermissionRequiredMixin, generic.ObjectDeleteView):
    """Base delete view for objects that belong to an ImportProfile.

    Redirects to the parent profile detail page after successful deletion.
    """

    def get_return_url(self, request, obj=None):
        if obj is not None and getattr(obj, "profile", None):
            return obj.profile.get_absolute_url()
        return super().get_return_url(request, obj)


# ---------------------------------------------------------------------------
# ColumnMapping CRUD
# ---------------------------------------------------------------------------


class ColumnMappingAddView(_ProfileChildEditView):
    """Add a column mapping to an existing ImportProfile."""

    queryset = ColumnMapping.objects.all()
    form = ColumnMappingForm
    template_name = "netbox_data_import/columnmapping_edit.html"
    permission_required = "netbox_data_import.add_columnmapping"


class ColumnMappingEditView(_ProfileChildEditView):
    """Edit an existing column mapping."""

    queryset = ColumnMapping.objects.all()
    form = ColumnMappingForm
    template_name = "netbox_data_import/columnmapping_edit.html"
    permission_required = "netbox_data_import.change_columnmapping"


class ColumnMappingDeleteView(_ProfileChildDeleteView):
    """Delete a column mapping."""

    queryset = ColumnMapping.objects.all()
    permission_required = "netbox_data_import.delete_columnmapping"


# ---------------------------------------------------------------------------
# ClassRoleMapping CRUD
# ---------------------------------------------------------------------------


class ClassRoleMappingAddView(_ProfileChildEditView):
    """Add a class→role mapping to an existing ImportProfile."""

    queryset = ClassRoleMapping.objects.all()
    form = ClassRoleMappingForm
    template_name = "netbox_data_import/classrolemapping_edit.html"
    permission_required = "netbox_data_import.add_classrolemapping"


class ClassRoleMappingEditView(_ProfileChildEditView):
    """Edit an existing class→role mapping."""

    queryset = ClassRoleMapping.objects.all()
    form = ClassRoleMappingForm
    template_name = "netbox_data_import/classrolemapping_edit.html"
    permission_required = "netbox_data_import.change_classrolemapping"


class ClassRoleMappingDeleteView(_ProfileChildDeleteView):
    """Delete a class→role mapping."""

    queryset = ClassRoleMapping.objects.all()
    permission_required = "netbox_data_import.delete_classrolemapping"


# ---------------------------------------------------------------------------
# DeviceTypeMapping CRUD
# ---------------------------------------------------------------------------


class DeviceTypeMappingAddView(_ProfileChildEditView):
    """Add a device type mapping to an existing ImportProfile."""

    queryset = DeviceTypeMapping.objects.all()
    form = DeviceTypeMappingForm
    template_name = "netbox_data_import/devicetypemapping_edit.html"
    permission_required = "netbox_data_import.add_devicetypemapping"


class DeviceTypeMappingEditView(_ProfileChildEditView):
    """Edit an existing device type mapping."""

    queryset = DeviceTypeMapping.objects.all()
    form = DeviceTypeMappingForm
    template_name = "netbox_data_import/devicetypemapping_edit.html"
    permission_required = "netbox_data_import.change_devicetypemapping"


class DeviceTypeMappingDeleteView(_ProfileChildDeleteView):
    """Delete a device type mapping."""

    queryset = DeviceTypeMapping.objects.all()
    permission_required = "netbox_data_import.delete_devicetypemapping"


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
        form = ImportSetupForm(initial=initial)
        return render(request, "netbox_data_import/import_setup.html", {"form": form})

    def post(self, request):
        """Parse the uploaded file and redirect to the preview step."""
        form = ImportSetupForm(request.POST, request.FILES)
        if not form.is_valid():
            return render(request, "netbox_data_import/import_setup.html", {"form": form})

        from . import engine

        profile = form.cleaned_data["profile"]
        excel_file = form.cleaned_data["excel_file"]
        site = form.cleaned_data["site"]
        location = form.cleaned_data.get("location")
        tenant = form.cleaned_data.get("tenant")

        try:
            rows, unused_stats = engine.parse_file(excel_file, profile, return_stats=True)
        except engine.ParseError as exc:
            messages.error(request, f"Failed to parse file: {exc}")
            return render(request, "netbox_data_import/import_setup.html", {"form": form})

        context = {"site": site, "location": location, "tenant": tenant}
        result = engine.run_import(rows, profile, context, dry_run=True, user=request.user)

        # Store result + raw rows + context in session for the preview/execute steps
        # Rows need JSON-safe serialization (handle datetime from Excel)
        request.session["import_result"] = result.to_session_dict()
        request.session["import_rows"] = _serialize_rows(rows)
        request.session["import_context"] = {
            "profile_id": profile.pk,
            "site_id": site.pk,
            "location_id": location.pk if location else None,
            "tenant_id": tenant.pk if tenant else None,
            "filename": excel_file.name,
        }
        request.session["import_unused_columns"] = unused_stats
        return redirect(reverse("plugins:netbox_data_import:import_preview"))


class ImportPreviewView(PermissionRequiredMixin, View):
    """Step 2: show dry-run results, let user confirm or go back."""

    permission_required = "netbox_data_import.change_importprofile"

    def get(self, request):
        """Re-run the dry-run import and render the preview template."""
        rows = request.session.get("import_rows")
        ctx = request.session.get("import_context", {})
        if not rows or not ctx:
            messages.warning(request, "No import in progress. Please start a new import.")
            return redirect(reverse("plugins:netbox_data_import:import_setup"))

        from dcim.models import Location, Site
        from tenancy.models import Tenant

        from . import engine

        profile = ImportProfile.objects.filter(pk=ctx.get("profile_id")).first()
        if not profile:
            messages.warning(request, "Import profile not found.")
            return redirect(reverse("plugins:netbox_data_import:import_setup"))

        site = Site.objects.filter(pk=ctx.get("site_id")).first()
        location = Location.objects.filter(pk=ctx.get("location_id")).first() if ctx.get("location_id") else None
        tenant = Tenant.objects.filter(pk=ctx.get("tenant_id")).first() if ctx.get("tenant_id") else None

        context_obj = {"site": site, "location": location, "tenant": tenant}
        # Re-apply saved resolutions so any resolution saved after the initial upload
        # is reflected without requiring a file re-upload.
        rows = engine.reapply_saved_resolutions(rows, profile)
        # Always re-run so any new mappings/matches are immediately reflected
        result = engine.run_import(rows, profile, context_obj, dry_run=True, user=request.user)
        request.session["import_result"] = result.to_session_dict()

        # Build existing resolutions map for the split-name modal preview
        import json as _json

        from .models import SourceResolution

        existing_resolutions = {}
        for res in SourceResolution.objects.filter(profile=profile):
            existing_resolutions.setdefault(str(res.source_id), {})[res.source_column] = {
                "original_value": res.original_value,
                "resolved_fields": res.resolved_fields,
            }

        view_mode = request.GET.get("view", profile.preview_view_mode)

        # Build unused columns list: filter out any that are now mapped
        mapped_source_cols = set(profile.column_mappings.values_list("source_column", flat=True))
        raw_unused = request.session.get("import_unused_columns") or {}
        unused_columns = [
            {
                "name": col,
                "count": stats.get("count", 0),
                "samples": stats.get("samples", []),
                "suggested_field": _fuzzy_match_netbox_field(col),
            }
            for col, stats in raw_unused.items()
            if isinstance(stats, dict) and col not in mapped_source_cols
        ]
        unused_columns.sort(key=lambda x: -x["count"])

        return render(
            request,
            "netbox_data_import/import_preview.html",
            {
                "result": result,
                "filename": ctx.get("filename", ""),
                "profile_id": ctx.get("profile_id"),
                "profile": profile,
                "view_mode": view_mode,
                "existing_resolutions_json": _json.dumps(existing_resolutions).translate(
                    {ord("<"): "\\u003C", ord(">"): "\\u003E", ord("&"): "\\u0026"}
                ),
                "existing_resolutions": existing_resolutions,
                "can_create_role": request.user.has_perm("dcim.add_devicerole"),
                "unused_columns": unused_columns,
                "target_field_choices": TARGET_FIELD_CHOICES,
            },
        )


class ImportRunView(PermissionRequiredMixin, View):
    """Step 3: run the real import (dry_run=False)."""

    permission_required = "netbox_data_import.change_importprofile"

    def post(self, request):
        """Execute the real import and redirect to the results page."""
        rows = request.session.get("import_rows")
        ctx_data = request.session.get("import_context")
        if not rows or not ctx_data:
            messages.warning(request, "No import in progress.")
            return redirect(reverse("plugins:netbox_data_import:import_setup"))

        from dcim.models import Location, Site
        from django.db import transaction
        from tenancy.models import Tenant

        from . import engine

        profile = get_object_or_404(ImportProfile, pk=ctx_data["profile_id"])
        site = get_object_or_404(Site, pk=ctx_data["site_id"])
        location = get_object_or_404(Location, pk=ctx_data["location_id"]) if ctx_data.get("location_id") else None
        tenant = get_object_or_404(Tenant, pk=ctx_data["tenant_id"]) if ctx_data.get("tenant_id") else None

        context = {"site": site, "location": location, "tenant": tenant}

        with transaction.atomic():
            result = engine.run_import(rows, profile, context, dry_run=False, user=request.user)

        # Persist job record
        from .models import ImportJob

        job = ImportJob.objects.create(
            profile=profile,
            input_filename=ctx_data.get("filename", ""),
            dry_run=False,
            site_name=site.name,
            result_counts=result.counts,
            result_rows=[r.to_dict() for r in result.rows],
        )

        request.session["import_result"] = result.to_session_dict()
        request.session["import_job_id"] = job.pk
        messages.success(
            request,
            f"Import complete: {result.counts.get('devices_created', 0)} devices created, "
            f"{result.counts.get('racks_created', 0)} racks created.",
        )
        return redirect(reverse("plugins:netbox_data_import:import_results"))


class ImportResultsView(PermissionRequiredMixin, View):
    """Step 4: show final results with links to created objects."""

    permission_required = "netbox_data_import.view_importprofile"

    def get(self, request):
        """Render the results page for the most recent import."""
        session_data = request.session.get("import_result")
        if not session_data:
            return redirect(reverse("plugins:netbox_data_import:import_setup"))

        from . import engine

        result = engine.ImportResult.from_session_dict(session_data)
        job_id = request.session.get("import_job_id")
        return render(request, "netbox_data_import/import_results.html", {"result": result, "job_id": job_id})


# ---------------------------------------------------------------------------
# Import Job history
# ---------------------------------------------------------------------------


class ImportJobListView(PermissionRequiredMixin, generic.ObjectListView):
    """List all past import jobs for audit / history."""

    queryset = ImportJob.objects.select_related("profile").all()
    table = ImportJobTable
    template_name = "netbox_data_import/importjob_list.html"
    permission_required = "netbox_data_import.view_importjob"

    def get_required_permission(self):
        """Return the permission string required to view the import job list."""
        return "netbox_data_import.view_importjob"


# ---------------------------------------------------------------------------
# ColumnTransformRule CRUD
# ---------------------------------------------------------------------------


class ColumnTransformRuleAddView(_ProfileChildEditView):
    """Add a column transform rule to an existing ImportProfile."""

    queryset = ColumnTransformRule.objects.all()
    form = ColumnTransformRuleForm
    template_name = "netbox_data_import/columntransformrule_edit.html"
    permission_required = "netbox_data_import.add_columntransformrule"


class ColumnTransformRuleEditView(_ProfileChildEditView):
    """Edit an existing column transform rule."""

    queryset = ColumnTransformRule.objects.all()
    form = ColumnTransformRuleForm
    template_name = "netbox_data_import/columntransformrule_edit.html"
    permission_required = "netbox_data_import.change_columntransformrule"


class ColumnTransformRuleDeleteView(_ProfileChildDeleteView):
    """Delete a column transform rule."""

    queryset = ColumnTransformRule.objects.all()
    permission_required = "netbox_data_import.delete_columntransformrule"


# ---------------------------------------------------------------------------
# Ignore / Unignore device
# ---------------------------------------------------------------------------
# The action views below (Ignore/Unignore/Sync/Quick*) are lightweight POST
# endpoints that return JSON or an immediate redirect.  No NetBox generic base
# class exists for this pattern; PermissionRequiredMixin + View is intentional.
# ---------------------------------------------------------------------------


class IgnoreDeviceView(PermissionRequiredMixin, View):
    """Mark a specific device (by source_id) as ignored for a profile."""

    permission_required = "netbox_data_import.add_ignoreddevice"

    def post(self, request):
        """Add the specified device to the profile's ignore list."""
        from .models import IgnoredDevice

        profile_id = request.POST.get("profile_id")
        source_id = request.POST.get("source_id")
        device_name = request.POST.get("device_name", "")
        next_url = _safe_next_url(request, "plugins:netbox_data_import:import_preview")

        if profile_id and source_id:
            profile = get_object_or_404(ImportProfile, pk=profile_id)
            IgnoredDevice.objects.get_or_create(
                profile=profile,
                source_id=source_id,
                defaults={"device_name": device_name},
            )
            messages.success(request, f"Device '{device_name or source_id}' added to ignore list.")
        return redirect(next_url)


class UnignoreDeviceView(PermissionRequiredMixin, View):
    """Remove a device from the ignore list."""

    permission_required = "netbox_data_import.delete_ignoreddevice"

    def post(self, request):
        """Remove the specified device from the profile's ignore list."""
        from .models import IgnoredDevice

        profile_id = request.POST.get("profile_id")
        source_id = request.POST.get("source_id")
        next_url = _safe_next_url(request, "plugins:netbox_data_import:import_preview")

        if profile_id and source_id:
            count, _ = IgnoredDevice.objects.filter(
                profile_id=profile_id,
                source_id=source_id,
            ).delete()
            if count:
                messages.success(request, "Device removed from ignore list.")
            else:
                messages.warning(request, "Device was not on the ignore list (may be ignored by class mapping).")
        return redirect(next_url)


class RemoveExtraIpView(PermissionRequiredMixin, View):
    """Remove a stored IP from extra_json['_ip'] on a device's data_import_source custom field."""

    permission_required = "dcim.change_device"

    def post(self, request):
        """Remove an IP field from the device's data_import_source custom field."""
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

        device = get_object_or_404(Device, pk=device_id)
        import_data = device.cf.get("data_import_source") or {}
        ip_data = import_data.get("_ip") or {}

        if ip_field in ip_data:
            del ip_data[ip_field]
            if ip_data:
                import_data["_ip"] = ip_data
            else:
                import_data.pop("_ip", None)
            device.custom_field_data["data_import_source"] = import_data
            device.save(update_fields=["custom_field_data"])
            messages.success(request, f"Removed {ip_field} from JSON storage.")
        else:
            messages.info(request, f"{ip_field} was not in JSON storage.")

        return _safe_return(device)


# ---------------------------------------------------------------------------
# Sync single device field from import file value
# ---------------------------------------------------------------------------


class SyncDeviceFieldView(PermissionRequiredMixin, View):
    """Apply a single field value from the import file to an existing NetBox device."""

    permission_required = "dcim.change_device"

    _ALLOWED_FIELDS = {"device_name", "u_position", "status", "serial", "asset_tag", "face"}

    def post(self, request):
        """Apply the given field value to the specified device."""
        from django.http import JsonResponse

        from dcim.models import Device
        from .engine import _STATUS_MAP

        device_id = request.POST.get("device_id")
        field = request.POST.get("field", "")
        value = request.POST.get("value", "")

        # Validate field first
        if not field or field not in self._ALLOWED_FIELDS:
            return JsonResponse({"ok": False, "error": f"Field '{field}' is not syncable"})

        # Look up device
        try:
            device = Device.objects.select_related("device_type").get(pk=device_id)
        except (Device.DoesNotExist, ValueError, TypeError):
            return JsonResponse({"ok": False, "error": "Device not found"})

        try:
            display = self._apply_field(device, field, value, _STATUS_MAP)
        except ValueError as exc:
            return JsonResponse({"ok": False, "error": str(exc)})
        except Exception:
            logger.exception(
                "SyncDeviceFieldView failed for device_id=%s field=%s",
                device_id,
                field,
            )
            return JsonResponse({"ok": False, "error": "An internal error occurred."}, status=500)

        return JsonResponse({"ok": True, "display": display})

    def _apply_field(self, device, field, value, status_map):
        if field == "device_name":
            new_name = str(value)[:64]
            if type(device).objects.filter(site=device.site, name=new_name).exclude(pk=device.pk).exists():
                raise ValueError(f"A device named '{new_name}' already exists in site '{device.site}'")
            device.name = new_name
            device.save(update_fields=["name"])
            return new_name

        if field == "u_position":
            try:
                pos = int(value)
            except (ValueError, TypeError):
                raise ValueError(f"Cannot parse '{value}' as integer for u_position")
            device.position = pos
            device.save(update_fields=["position"])
            return f"U{device.position}"

        if field == "status":
            v = str(value).strip().lower()
            mapped = status_map.get(v)
            # Also accept NetBox status slugs directly (e.g. "active", "offline")
            if mapped is None and v in status_map.values():
                mapped = v
            if mapped is None:
                raise ValueError(f"Unknown status value '{value}'")
            device.status = mapped
            device.save(update_fields=["status"])
            return device.status

        if field == "u_height":
            # u_height is a DeviceType field — this change affects all devices sharing this type
            try:
                height = float(value)
            except (ValueError, TypeError):
                raise ValueError(f"Cannot parse '{value}' as number for u_height")
            device.device_type.u_height = height
            device.device_type.save(update_fields=["u_height"])
            n = device.device_type.u_height
            return f"{int(n)}U" if n == int(n) else f"{n}U"

        if field == "serial":
            device.serial = str(value)[:50]
            device.save(update_fields=["serial"])
            return device.serial

        if field == "asset_tag":
            device.asset_tag = str(value)[:50] if value else None
            device.save(update_fields=["asset_tag"])
            return device.asset_tag

        if field == "face":
            v = str(value).strip().lower()
            _FACE_MAP = {"front": "front", "rear": "rear", "0": "front", "1": "rear"}
            mapped = _FACE_MAP.get(v)
            if mapped is None:
                raise ValueError(f"Unknown face value '{value}' — expected 'front' or 'rear'")
            device.face = mapped
            device.save(update_fields=["face"])
            return device.face

        raise ValueError(f"Field '{field}' is not syncable")


# ---------------------------------------------------------------------------
# Save resolution (rerere)
# ---------------------------------------------------------------------------


class SaveResolutionView(PermissionRequiredMixin, View):
    """Save a manual field resolution for rerere replay."""

    permission_required = "netbox_data_import.add_sourceresolution"

    def post(self, request):
        """Persist a manual field resolution for rerere replay."""
        import json

        from .models import SourceResolution

        profile_id = request.POST.get("profile_id")
        source_id = request.POST.get("source_id")
        source_column = request.POST.get("source_column")
        original_value = request.POST.get("original_value")
        resolved_fields_json = request.POST.get("resolved_fields", "{}")
        next_url = _safe_next_url(request, "plugins:netbox_data_import:import_preview")

        try:
            resolved_fields = json.loads(resolved_fields_json)
        except (json.JSONDecodeError, TypeError):
            resolved_fields = {}

        if profile_id and source_id and source_column:
            profile = get_object_or_404(ImportProfile, pk=profile_id)
            SourceResolution.objects.update_or_create(
                profile=profile,
                source_id=source_id,
                source_column=source_column,
                defaults={
                    "original_value": original_value or "",
                    "resolved_fields": resolved_fields,
                },
            )
            messages.success(request, "Resolution saved. Re-run the import to apply it.")
        return redirect(next_url)


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
                _, was_created = ClassRoleMapping.objects.get_or_create(
                    profile=profile,
                    source_class=item["source_class"],
                    defaults={
                        "creates_rack": item.get("creates_rack", False),
                        "role_slug": item.get("role_slug", ""),
                        "ignore": item.get("ignore", False),
                    },
                )
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
            return redirect(request.path)

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
                "sheet_name": profile.sheet_name,
                "source_id_column": profile.source_id_column,
                "custom_field_name": profile.custom_field_name,
                "update_existing": profile.update_existing,
                "create_missing_device_types": profile.create_missing_device_types,
                "preview_view_mode": profile.preview_view_mode,
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
    permission_required = "netbox_data_import.delete_sourceresolution"


# ---------------------------------------------------------------------------
# Quick-resolve views (inline fixes from preview page)
# ---------------------------------------------------------------------------


class QuickCreateManufacturerView(PermissionRequiredMixin, View):
    """Immediately create a Manufacturer in NetBox from the preview page.

    Redirects back to preview so the row changes from 'create' to a device action.
    """

    permission_required = "netbox_data_import.add_devicetypemapping"

    def post(self, request):
        """Create the manufacturer in NetBox and redirect back to preview."""
        from dcim.models import Manufacturer

        if not request.user.has_perm("dcim.add_manufacturer"):  # pragma: no cover
            messages.error(request, "Permission denied: dcim.add_manufacturer required.")
            return redirect(reverse("plugins:netbox_data_import:import_preview"))
        mfg_name = request.POST.get("mfg_name", "").strip()
        mfg_slug = request.POST.get("mfg_slug", "").strip()
        if not mfg_name or not mfg_slug:
            messages.error(request, "Manufacturer name and slug are required.")
            return redirect(reverse("plugins:netbox_data_import:import_preview"))
        mfg, created = Manufacturer.objects.get_or_create(
            slug=mfg_slug,
            defaults={"name": mfg_name},
        )
        if created:
            messages.success(request, f"Manufacturer '{mfg.name}' created.")
        else:
            messages.info(request, f"Manufacturer '{mfg.name}' already existed.")
        return redirect(reverse("plugins:netbox_data_import:import_preview"))


class QuickResolveManufacturerView(PermissionRequiredMixin, View):
    """Save a ManufacturerMapping (source make → NetBox manufacturer slug) from the preview page.

    Used when a source has inconsistent naming (e.g. 'Dell EMC' → 'dell').
    Redirects back to preview which re-runs with the mapping applied.
    """

    permission_required = "netbox_data_import.add_manufacturermapping"

    def post(self, request):
        """Save the manufacturer mapping and redirect back to preview."""
        profile_id = request.POST.get("profile_id")
        profile = get_object_or_404(ImportProfile, pk=profile_id)
        source_make = " ".join(request.POST.get("source_make", "").split())
        netbox_mfg_slug = request.POST.get("netbox_mfg_slug", "").strip()
        if not source_make or not netbox_mfg_slug:
            messages.error(request, "Source make and NetBox manufacturer slug are required.")
            return redirect(reverse("plugins:netbox_data_import:import_preview"))
        _, created = ManufacturerMapping.objects.update_or_create(
            profile=profile,
            source_make=source_make,
            defaults={"netbox_manufacturer_slug": netbox_mfg_slug},
        )
        verb = "Created" if created else "Updated"
        messages.success(request, f"{verb} manufacturer mapping: '{source_make}' → {netbox_mfg_slug}")
        return redirect(reverse("plugins:netbox_data_import:import_preview"))


class QuickResolveDeviceTypeView(PermissionRequiredMixin, View):
    """Save a DeviceTypeMapping (source make/model → NetBox slugs) from the preview page.

    Optionally also creates the manufacturer and/or device type in NetBox right now.
    Redirects back to preview which re-runs and shows the resolved rows.
    """

    permission_required = "netbox_data_import.add_devicetypemapping"

    def post(self, request):
        """Save the device type mapping (and optionally create objects) then redirect."""
        from dcim.models import DeviceType, Manufacturer
        from django.utils.text import slugify

        profile_id = request.POST.get("profile_id")
        profile = get_object_or_404(ImportProfile, pk=profile_id)
        source_make = " ".join(request.POST.get("source_make", "").split())
        source_model = " ".join(request.POST.get("source_model", "").split())
        netbox_mfg_slug = request.POST.get("netbox_mfg_slug", "").strip()
        netbox_dt_slug = request.POST.get("netbox_dt_slug", "").strip()
        action = request.POST.get("action", "map")  # "map" or "create_now"

        if not source_make or not source_model:
            messages.error(request, "Source make and model are required.")
            return redirect(reverse("plugins:netbox_data_import:import_preview"))

        if not netbox_mfg_slug:
            netbox_mfg_slug = slugify(source_make)
        if not netbox_dt_slug:
            netbox_dt_slug = slugify(source_model)

        # Save/update DeviceTypeMapping
        dtm, created = DeviceTypeMapping.objects.update_or_create(
            profile=profile,
            source_make=source_make,
            source_model=source_model,
            defaults={
                "netbox_manufacturer_slug": netbox_mfg_slug,
                "netbox_device_type_slug": netbox_dt_slug,
            },
        )

        if action == "create_now":
            if not request.user.has_perm("dcim.add_manufacturer"):  # pragma: no cover
                messages.error(request, "Permission denied: dcim.add_manufacturer required.")
                return redirect(reverse("plugins:netbox_data_import:import_preview"))
            if not request.user.has_perm("dcim.add_devicetype"):  # pragma: no cover
                messages.error(request, "Permission denied: dcim.add_devicetype required.")
                return redirect(reverse("plugins:netbox_data_import:import_preview"))
            mfg, _ = Manufacturer.objects.get_or_create(
                slug=netbox_mfg_slug,
                defaults={"name": source_make},
            )
            dt_name = request.POST.get("netbox_dt_name", source_model).strip() or source_model
            try:
                u_height = max(1, int(request.POST.get("u_height", "1")))
            except ValueError:
                u_height = 1
            DeviceType.objects.get_or_create(
                manufacturer=mfg,
                slug=netbox_dt_slug,
                defaults={"model": dt_name, "u_height": u_height},
            )
            messages.success(
                request, f"Mapping saved and device type '{source_make} / {source_model}' created in NetBox."
            )
        else:
            verb = "created" if created else "updated"
            messages.success(
                request,
                f"DeviceType mapping {verb}: '{source_make} / {source_model}' → {netbox_mfg_slug}/{netbox_dt_slug}",
            )

        return redirect(reverse("plugins:netbox_data_import:import_preview"))


class QuickAddClassRoleMappingView(PermissionRequiredMixin, View):
    """Quickly add a ClassRoleMapping (ignore / role) directly from an error row in preview.

    Redirects back to preview; error rows for that class disappear on re-run.
    """

    permission_required = "netbox_data_import.add_classrolemapping"

    def post(self, request):
        """Save the class→role mapping and redirect back to preview."""
        from dcim.models import RackType

        profile_id = request.POST.get("profile_id")
        profile = get_object_or_404(ImportProfile, pk=profile_id)
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
                messages.error(
                    request, f"Invalid rack type selected for class '{source_class}'. Please choose a valid rack type."
                )
                return redirect(reverse("plugins:netbox_data_import:import_preview"))

        if not source_class:
            messages.error(request, "Source class is required.")
            return redirect(reverse("plugins:netbox_data_import:import_preview"))

        _, created = ClassRoleMapping.objects.update_or_create(
            profile=profile,
            source_class=source_class,
            defaults={
                "ignore": mapping_action == "ignore",
                "creates_rack": creates_rack,
                "rack_type": rack_type,
                "role_slug": role_slug if mapping_action == "role" else "",
            },
        )
        verb = "Created" if created else "Updated"
        if mapping_action == "ignore":
            action_label = "ignore"
        elif mapping_action == "rack":
            rt_suffix = f" (type: {rack_type})" if rack_type else ""
            action_label = f"creates rack{rt_suffix}"
        else:
            action_label = f"role '{role_slug}'"
        messages.success(request, f"{verb} mapping: class '{source_class}' → {action_label}")
        return redirect(reverse("plugins:netbox_data_import:import_preview"))


class QuickAddColumnMappingView(PermissionRequiredMixin, View):
    """Quickly map an unmapped source column to a NetBox target field from the preview panel."""

    permission_required = "netbox_data_import.add_columnmapping"

    def post(self, request):
        """Save the column mapping and redirect back to preview."""
        import re

        profile_id = request.POST.get("profile_id")
        profile = get_object_or_404(ImportProfile, pk=profile_id)
        source_column = request.POST.get("source_column", "").strip()
        target_field = request.POST.get("target_field", "").strip()

        valid_standard_fields = {choice[0] for choice in TARGET_FIELD_CHOICES}
        is_extra_json = target_field.startswith("extra_json:")
        if is_extra_json:
            key = target_field[len("extra_json:") :]
            if not re.match(r"^[a-zA-Z0-9_-]{1,50}$", key):
                is_extra_json = False  # invalid key → fall through to error below

        if not source_column or (target_field not in valid_standard_fields and not is_extra_json):
            messages.error(request, "Valid source column and target field are required.")
            return redirect(reverse("plugins:netbox_data_import:import_preview"))

        # Remove any existing mapping that claims the same target_field (for a different source
        # column) before upserting, to avoid the unique constraint violation.
        displaced = ColumnMapping.objects.filter(profile=profile, target_field=target_field).exclude(
            source_column=source_column
        )
        displaced_source = displaced.values_list("source_column", flat=True).first()
        displaced.delete()

        _, created = ColumnMapping.objects.update_or_create(
            profile=profile,
            source_column=source_column,
            defaults={"target_field": target_field},
        )
        if displaced_source:
            messages.success(
                request,
                f"Reassigned: '{source_column}' → {target_field} (previously mapped from '{displaced_source}')",
            )
        else:
            verb = "Created" if created else "Updated"
            messages.success(request, f"{verb} mapping: '{source_column}' → {target_field}")
        return redirect(reverse("plugins:netbox_data_import:import_preview"))


class MatchExistingDeviceView(PermissionRequiredMixin, View):
    """Link a source row to an existing NetBox device (by device ID).

    Saves a DeviceExistingMatch; on next preview re-run the row shows action='update'.
    """

    permission_required = "netbox_data_import.add_deviceexistingmatch"

    def post(self, request):
        """Save the device match and redirect back to preview."""
        from dcim.models import Device

        profile_id = request.POST.get("profile_id")
        profile = get_object_or_404(ImportProfile, pk=profile_id)
        source_id = request.POST.get("source_id", "").strip()
        netbox_device_id = request.POST.get("netbox_device_id", "").strip()

        if not source_id or not netbox_device_id:
            messages.error(request, "source_id and netbox_device_id are required.")
            return redirect(reverse("plugins:netbox_data_import:import_preview"))

        try:
            device = Device.objects.get(pk=int(netbox_device_id))
        except (Device.DoesNotExist, ValueError):
            messages.error(request, f"Device #{netbox_device_id} not found.")
            return redirect(reverse("plugins:netbox_data_import:import_preview"))

        DeviceExistingMatch.objects.update_or_create(
            profile=profile,
            source_id=source_id,
            defaults={
                "netbox_device_id": device.pk,
                "device_name": device.name,
                "source_asset_tag": request.POST.get("source_asset_tag", "").strip(),
            },
        )
        messages.success(request, f"Source '{source_id}' linked to existing device '{device.name}'.")
        return redirect(reverse("plugins:netbox_data_import:import_preview"))


def _device_name_filter(q: str):
    """Build a Django Q filter for device name search.

    Exact icontains is tried first; when the query contains separators (-, _, .)
    individual tokens (≥3 chars) are OR-ed in so that e.g. "PROD-LAB03-SW3"
    matches "prod-lab03-sw03.prod-lab.aorta.net" via the "LAB03" token.
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


class SearchNetBoxObjectsView(PermissionRequiredMixin, View):
    """AJAX search endpoint for NetBox objects used in preview quick-fix modals.

    GET params: type (manufacturer|device_type|device|role|rack_type), q (search string).
    Returns JSON list of {id, name, slug, url} dicts.
    """

    permission_required = "netbox_data_import.view_importprofile"

    def get(self, request):
        """Return a JSON list of matching NetBox objects for the given type and query."""
        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, RackType
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
            for dev in Device.objects.filter(_device_name_filter(q)).distinct().select_related("site")[:limit]:
                results.append(
                    {
                        "id": dev.pk,
                        "name": dev.name,
                        "site": dev.site.name if dev.site else "",
                        "url": request.build_absolute_uri(dev.get_absolute_url()),
                    }
                )
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
            qs = RackType.objects.select_related("manufacturer").filter(model__icontains=q)[:limit]
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


class QuickCreateDeviceRoleView(PermissionRequiredMixin, View):
    """AJAX endpoint: create a new DeviceRole and return its details as JSON.

    Used by the Configure Class modal so operators can create missing roles
    without leaving the import preview page.
    """

    permission_required = "netbox_data_import.view_importprofile"

    def post(self, request):
        """Create the DeviceRole and return JSON {id, name, slug}."""
        from dcim.models import DeviceRole
        from django.http import JsonResponse

        if not request.user.has_perm("dcim.add_devicerole"):
            return JsonResponse({"error": "Permission denied: dcim.add_devicerole required."}, status=403)

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
            role, created = DeviceRole.objects.get_or_create(slug=slug, defaults={"name": name, "color": color})
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


def _auto_match_single_device(device_model, device_name, serial, asset_tag):
    """Try to match a single device row to an existing NetBox device.

    Returns (device_or_None, is_ambiguous).  Matching priority: serial →
    asset_tag → exact name.  Multiple matches on any field → ambiguous.
    """
    device = None
    if serial:
        results = list(device_model.objects.filter(serial=serial)[:2])
        if len(results) == 1:
            device = results[0]
        elif len(results) > 1:
            return None, True

    if device is None and asset_tag:
        results = list(device_model.objects.filter(asset_tag=asset_tag)[:2])
        if len(results) == 1:
            device = results[0]
        elif len(results) > 1:
            return None, True

    if device is None and device_name:
        results = list(device_model.objects.filter(name=device_name)[:2])
        if len(results) == 1:
            device = results[0]
        elif len(results) > 1:
            return None, True

    return device, False


class AutoMatchDevicesView(PermissionRequiredMixin, View):
    """Scan all device rows in the session and auto-match to existing NetBox devices.

    Priority: serial > asset_tag > exact name match.
    Name substring matches are recorded as probable_matches only (not auto-linked).
    """

    permission_required = "netbox_data_import.change_importprofile"

    def post(self, request):
        """Run auto-matching and redirect back to preview with a summary message."""
        from dcim.models import Device

        profile_id = request.POST.get("profile_id")
        profile = get_object_or_404(ImportProfile, pk=profile_id)
        rows = request.session.get("import_rows", [])

        matched = 0
        ambiguous = 0
        already = 0
        probable = 0

        for row in rows:
            source_id = str(row.get("source_id", "")).strip()
            device_name = str(row.get("device_name", "")).strip()
            serial = str(row.get("serial", "")).strip()
            asset_tag = str(row.get("asset_tag", "")).strip()
            if not source_id:
                continue
            if profile.device_matches.filter(source_id=source_id).exists():
                already += 1
                continue

            device, is_ambiguous = _auto_match_single_device(Device, device_name, serial, asset_tag)
            if is_ambiguous:
                ambiguous += 1
                continue

            if device is not None:
                DeviceExistingMatch.objects.create(
                    profile=profile,
                    source_id=source_id,
                    netbox_device_id=device.pk,
                    device_name=device.name,
                    source_asset_tag=asset_tag,
                )
                matched += 1
            elif device_name:
                # Substring name match → probable only (no auto-link)
                short_name = device_name.split(" - ")[-1].strip() if " - " in device_name else device_name
                if Device.objects.filter(name__icontains=short_name).exists():
                    probable += 1

        msg_parts = []
        if matched:
            msg_parts.append(f"{matched} auto-matched (serial/asset_tag/name)")
        if probable:
            msg_parts.append(f"{probable} probable name match(es) — use Link button to confirm")
        if ambiguous:
            msg_parts.append(f"{ambiguous} ambiguous (multiple devices)")
        if already:
            msg_parts.append(f"{already} already matched")
        messages.success(request, f"Auto-match: {', '.join(msg_parts) or 'nothing found'}.")
        return redirect(reverse("plugins:netbox_data_import:import_preview"))


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------


def _serialize_rows(rows: list) -> list:
    """Convert parsed rows to JSON-serializable format (handle Excel datetime values)."""
    import datetime

    safe_rows = []
    for row in rows:
        safe_row = {}
        for k, v in row.items():
            if isinstance(v, (datetime.datetime, datetime.date)):
                safe_row[k] = v.isoformat()
            else:
                safe_row[k] = v
        safe_rows.append(safe_row)
    return safe_rows
