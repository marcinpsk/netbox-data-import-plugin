# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from netbox.views import generic
from .models import (
    ImportProfile,
    ColumnMapping,
    ClassRoleMapping,
    DeviceTypeMapping,
    ImportJob,
    ColumnTransformRule,
    DeviceExistingMatch,
    ManufacturerMapping,
)
from .forms import (
    ImportProfileForm,
    ColumnMappingForm,
    ClassRoleMappingForm,
    DeviceTypeMappingForm,
    ColumnTransformRuleForm,
    ImportSetupForm,
)
from .tables import (
    ImportProfileTable,
    ColumnMappingTable,
    ClassRoleMappingTable,
    DeviceTypeMappingTable,
    ColumnTransformRuleTable,
)
from .filters import ImportProfileFilterSet


def _safe_next_url(request, fallback: str) -> str:
    """Return a validated same-host redirect URL from POST or the fallback view name."""
    url = request.POST.get("next", "")
    if url and url_has_allowed_host_and_scheme(
        url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return url
    return reverse(fallback)


# ---------------------------------------------------------------------------
# ImportProfile
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# ColumnMapping CRUD
# ---------------------------------------------------------------------------


class ColumnMappingAddView(LoginRequiredMixin, View):
    """Add a column mapping to an existing ImportProfile."""

    def get(self, request, profile_pk):
        """Render the add form for a new column mapping."""
        profile = get_object_or_404(ImportProfile, pk=profile_pk)
        form = ColumnMappingForm(initial={"profile": profile})
        return render(request, "netbox_data_import/columnmapping_edit.html", {"form": form, "profile": profile})

    def post(self, request, profile_pk):
        """Save a new column mapping or re-render the form with errors."""
        profile = get_object_or_404(ImportProfile, pk=profile_pk)
        form = ColumnMappingForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Column mapping added.")
            return redirect(profile.get_absolute_url())
        return render(request, "netbox_data_import/columnmapping_edit.html", {"form": form, "profile": profile})


class ColumnMappingEditView(LoginRequiredMixin, View):
    """Edit an existing column mapping."""

    def get(self, request, pk):
        """Render the edit form for an existing column mapping."""
        obj = get_object_or_404(ColumnMapping, pk=pk)
        form = ColumnMappingForm(instance=obj)
        return render(
            request, "netbox_data_import/columnmapping_edit.html", {"form": form, "profile": obj.profile, "object": obj}
        )

    def post(self, request, pk):
        """Save edits to an existing column mapping or re-render with errors."""
        obj = get_object_or_404(ColumnMapping, pk=pk)
        form = ColumnMappingForm(request.POST, instance=obj)
        if form.is_valid():
            form.save()
            messages.success(request, "Column mapping updated.")
            return redirect(obj.profile.get_absolute_url())
        return render(
            request, "netbox_data_import/columnmapping_edit.html", {"form": form, "profile": obj.profile, "object": obj}
        )


class ColumnMappingDeleteView(LoginRequiredMixin, View):
    """Delete a column mapping."""

    def get(self, request, pk):
        """Render the delete confirmation page for a column mapping."""
        obj = get_object_or_404(ColumnMapping, pk=pk)
        return render(
            request,
            "netbox_data_import/confirm_delete.html",
            {"object": obj, "return_url": obj.profile.get_absolute_url()},
        )

    def post(self, request, pk):
        """Delete the column mapping and redirect to the parent profile."""
        obj = get_object_or_404(ColumnMapping, pk=pk)
        profile_url = obj.profile.get_absolute_url()
        obj.delete()
        messages.success(request, "Column mapping deleted.")
        return redirect(profile_url)


# ---------------------------------------------------------------------------
# ClassRoleMapping CRUD
# ---------------------------------------------------------------------------


class ClassRoleMappingAddView(LoginRequiredMixin, View):
    """Add a class→role mapping to an existing ImportProfile."""

    def get(self, request, profile_pk):
        """Render the add form for a new class→role mapping."""
        profile = get_object_or_404(ImportProfile, pk=profile_pk)
        form = ClassRoleMappingForm(initial={"profile": profile})
        return render(request, "netbox_data_import/classrolemapping_edit.html", {"form": form, "profile": profile})

    def post(self, request, profile_pk):
        """Save a new class→role mapping or re-render with errors."""
        profile = get_object_or_404(ImportProfile, pk=profile_pk)
        form = ClassRoleMappingForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Class→Role mapping added.")
            return redirect(profile.get_absolute_url())
        return render(request, "netbox_data_import/classrolemapping_edit.html", {"form": form, "profile": profile})


class ClassRoleMappingEditView(LoginRequiredMixin, View):
    """Edit an existing class→role mapping."""

    def get(self, request, pk):
        """Render the edit form for an existing class→role mapping."""
        obj = get_object_or_404(ClassRoleMapping, pk=pk)
        form = ClassRoleMappingForm(instance=obj)
        return render(
            request,
            "netbox_data_import/classrolemapping_edit.html",
            {"form": form, "profile": obj.profile, "object": obj},
        )

    def post(self, request, pk):
        """Save edits to an existing class→role mapping or re-render with errors."""
        obj = get_object_or_404(ClassRoleMapping, pk=pk)
        form = ClassRoleMappingForm(request.POST, instance=obj)
        if form.is_valid():
            form.save()
            messages.success(request, "Class→Role mapping updated.")
            return redirect(obj.profile.get_absolute_url())
        return render(
            request,
            "netbox_data_import/classrolemapping_edit.html",
            {"form": form, "profile": obj.profile, "object": obj},
        )


class ClassRoleMappingDeleteView(LoginRequiredMixin, View):
    """Delete a class→role mapping."""

    def get(self, request, pk):
        """Render the delete confirmation page for a class→role mapping."""
        obj = get_object_or_404(ClassRoleMapping, pk=pk)
        return render(
            request,
            "netbox_data_import/confirm_delete.html",
            {"object": obj, "return_url": obj.profile.get_absolute_url()},
        )

    def post(self, request, pk):
        """Delete the class→role mapping and redirect to the parent profile."""
        obj = get_object_or_404(ClassRoleMapping, pk=pk)
        profile_url = obj.profile.get_absolute_url()
        obj.delete()
        messages.success(request, "Class→Role mapping deleted.")
        return redirect(profile_url)


# ---------------------------------------------------------------------------
# DeviceTypeMapping CRUD
# ---------------------------------------------------------------------------


class DeviceTypeMappingAddView(LoginRequiredMixin, View):
    """Add a device type mapping to an existing ImportProfile."""

    def get(self, request, profile_pk):
        """Render the add form for a new device type mapping."""
        profile = get_object_or_404(ImportProfile, pk=profile_pk)
        form = DeviceTypeMappingForm(initial={"profile": profile})
        return render(request, "netbox_data_import/devicetypemapping_edit.html", {"form": form, "profile": profile})

    def post(self, request, profile_pk):
        """Save a new device type mapping or re-render with errors."""
        profile = get_object_or_404(ImportProfile, pk=profile_pk)
        form = DeviceTypeMappingForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Device type mapping added.")
            return redirect(profile.get_absolute_url())
        return render(request, "netbox_data_import/devicetypemapping_edit.html", {"form": form, "profile": profile})


class DeviceTypeMappingEditView(LoginRequiredMixin, View):
    """Edit an existing device type mapping."""

    def get(self, request, pk):
        """Render the edit form for an existing device type mapping."""
        obj = get_object_or_404(DeviceTypeMapping, pk=pk)
        form = DeviceTypeMappingForm(instance=obj)
        return render(
            request,
            "netbox_data_import/devicetypemapping_edit.html",
            {"form": form, "profile": obj.profile, "object": obj},
        )

    def post(self, request, pk):
        """Save edits to an existing device type mapping or re-render with errors."""
        obj = get_object_or_404(DeviceTypeMapping, pk=pk)
        form = DeviceTypeMappingForm(request.POST, instance=obj)
        if form.is_valid():
            form.save()
            messages.success(request, "Device type mapping updated.")
            return redirect(obj.profile.get_absolute_url())
        return render(
            request,
            "netbox_data_import/devicetypemapping_edit.html",
            {"form": form, "profile": obj.profile, "object": obj},
        )


class DeviceTypeMappingDeleteView(LoginRequiredMixin, View):
    """Delete a device type mapping."""

    def get(self, request, pk):
        """Render the delete confirmation page for a device type mapping."""
        obj = get_object_or_404(DeviceTypeMapping, pk=pk)
        return render(
            request,
            "netbox_data_import/confirm_delete.html",
            {"object": obj, "return_url": obj.profile.get_absolute_url()},
        )

    def post(self, request, pk):
        """Delete the device type mapping and redirect to the parent profile."""
        obj = get_object_or_404(DeviceTypeMapping, pk=pk)
        profile_url = obj.profile.get_absolute_url()
        obj.delete()
        messages.success(request, "Device type mapping deleted.")
        return redirect(profile_url)


# ---------------------------------------------------------------------------
# Import Wizard — Phase 2 (setup + preview)
# ---------------------------------------------------------------------------


class ImportSetupView(LoginRequiredMixin, View):
    """Step 1: select profile, upload file, choose site/location/tenant."""

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
            rows = engine.parse_file(excel_file, profile)
        except engine.ParseError as exc:
            messages.error(request, f"Failed to parse file: {exc}")
            return render(request, "netbox_data_import/import_setup.html", {"form": form})

        context = {"site": site, "location": location, "tenant": tenant}
        result = engine.run_import(rows, profile, context, dry_run=True)

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
        return redirect(reverse("plugins:netbox_data_import:import_preview"))


class ImportPreviewView(LoginRequiredMixin, View):
    """Step 2: show dry-run results, let user confirm or go back."""

    def get(self, request):
        """Re-run the dry-run import and render the preview template."""
        rows = request.session.get("import_rows")
        ctx = request.session.get("import_context", {})
        if not rows or not ctx:
            messages.warning(request, "No import in progress. Please start a new import.")
            return redirect(reverse("plugins:netbox_data_import:import_setup"))

        from . import engine
        from dcim.models import Site, Location
        from tenancy.models import Tenant

        profile = ImportProfile.objects.filter(pk=ctx.get("profile_id")).first()
        if not profile:
            messages.warning(request, "Import profile not found.")
            return redirect(reverse("plugins:netbox_data_import:import_setup"))

        site = Site.objects.filter(pk=ctx.get("site_id")).first()
        location = Location.objects.filter(pk=ctx.get("location_id")).first() if ctx.get("location_id") else None
        tenant = Tenant.objects.filter(pk=ctx.get("tenant_id")).first() if ctx.get("tenant_id") else None

        context_obj = {"site": site, "location": location, "tenant": tenant}
        # Always re-run so any new mappings/matches are immediately reflected
        result = engine.run_import(rows, profile, context_obj, dry_run=True)
        request.session["import_result"] = result.to_session_dict()

        # Build existing resolutions map for the split-name modal preview
        from .models import SourceResolution
        import json as _json

        existing_resolutions = {}
        for res in SourceResolution.objects.filter(profile=profile):
            existing_resolutions.setdefault(str(res.source_id), {})[res.source_column] = {
                "original_value": res.original_value,
                "resolved_fields": res.resolved_fields,
            }

        view_mode = request.GET.get("view", profile.preview_view_mode)
        return render(
            request,
            "netbox_data_import/import_preview.html",
            {
                "result": result,
                "filename": ctx.get("filename", ""),
                "profile_id": ctx.get("profile_id"),
                "profile": profile,
                "view_mode": view_mode,
                "existing_resolutions_json": _json.dumps(existing_resolutions),
            },
        )


class ImportRunView(LoginRequiredMixin, View):
    """Step 3: run the real import (dry_run=False)."""

    def post(self, request):
        """Execute the real import and redirect to the results page."""
        rows = request.session.get("import_rows")
        ctx_data = request.session.get("import_context")
        if not rows or not ctx_data:
            messages.warning(request, "No import in progress.")
            return redirect(reverse("plugins:netbox_data_import:import_setup"))

        from django.db import transaction
        from dcim.models import Site, Location
        from tenancy.models import Tenant
        from . import engine

        profile = get_object_or_404(ImportProfile, pk=ctx_data["profile_id"])
        site = get_object_or_404(Site, pk=ctx_data["site_id"])
        location = get_object_or_404(Location, pk=ctx_data["location_id"]) if ctx_data.get("location_id") else None
        tenant = get_object_or_404(Tenant, pk=ctx_data["tenant_id"]) if ctx_data.get("tenant_id") else None

        context = {"site": site, "location": location, "tenant": tenant}

        with transaction.atomic():
            result = engine.run_import(rows, profile, context, dry_run=False)

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


class ImportResultsView(LoginRequiredMixin, View):
    """Step 4: show final results with links to created objects."""

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


class ImportJobListView(LoginRequiredMixin, View):
    """List all past import jobs for audit / history."""

    def get(self, request):
        """Render the import job history list."""
        from django.core.paginator import Paginator

        jobs_qs = ImportJob.objects.select_related("profile").all()
        paginator = Paginator(jobs_qs, 50)
        page = request.GET.get("page")
        jobs = paginator.get_page(page)
        return render(request, "netbox_data_import/importjob_list.html", {"jobs": jobs})


# ---------------------------------------------------------------------------
# ColumnTransformRule CRUD
# ---------------------------------------------------------------------------


class ColumnTransformRuleAddView(LoginRequiredMixin, View):
    """Add a column transform rule to an existing ImportProfile."""

    def get(self, request, profile_pk):
        """Render the add form for a new column transform rule."""
        profile = get_object_or_404(ImportProfile, pk=profile_pk)
        form = ColumnTransformRuleForm(initial={"profile": profile})
        return render(request, "netbox_data_import/columntransformrule_edit.html", {"form": form, "profile": profile})

    def post(self, request, profile_pk):
        """Save a new column transform rule or re-render with errors."""
        profile = get_object_or_404(ImportProfile, pk=profile_pk)
        form = ColumnTransformRuleForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Column transform rule added.")
            return redirect(profile.get_absolute_url())
        return render(request, "netbox_data_import/columntransformrule_edit.html", {"form": form, "profile": profile})


class ColumnTransformRuleEditView(LoginRequiredMixin, View):
    """Edit an existing column transform rule."""

    def get(self, request, pk):
        """Render the edit form for an existing column transform rule."""
        obj = get_object_or_404(ColumnTransformRule, pk=pk)
        form = ColumnTransformRuleForm(instance=obj)
        return render(
            request,
            "netbox_data_import/columntransformrule_edit.html",
            {"form": form, "profile": obj.profile, "object": obj},
        )

    def post(self, request, pk):
        """Save edits to an existing column transform rule or re-render with errors."""
        obj = get_object_or_404(ColumnTransformRule, pk=pk)
        form = ColumnTransformRuleForm(request.POST, instance=obj)
        if form.is_valid():
            form.save()
            messages.success(request, "Column transform rule updated.")
            return redirect(obj.profile.get_absolute_url())
        return render(
            request,
            "netbox_data_import/columntransformrule_edit.html",
            {"form": form, "profile": obj.profile, "object": obj},
        )


class ColumnTransformRuleDeleteView(LoginRequiredMixin, View):
    """Delete a column transform rule."""

    def get(self, request, pk):
        """Render the delete confirmation page for a column transform rule."""
        obj = get_object_or_404(ColumnTransformRule, pk=pk)
        return render(
            request,
            "netbox_data_import/confirm_delete.html",
            {"object": obj, "return_url": obj.profile.get_absolute_url()},
        )

    def post(self, request, pk):
        """Delete the column transform rule and redirect to the parent profile."""
        obj = get_object_or_404(ColumnTransformRule, pk=pk)
        profile_url = obj.profile.get_absolute_url()
        obj.delete()
        messages.success(request, "Column transform rule deleted.")
        return redirect(profile_url)


# ---------------------------------------------------------------------------
# Ignore / Unignore device
# ---------------------------------------------------------------------------


class IgnoreDeviceView(LoginRequiredMixin, View):
    """Mark a specific device (by source_id) as ignored for a profile."""

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


class UnignoreDeviceView(LoginRequiredMixin, View):
    """Remove a device from the ignore list."""

    def post(self, request):
        """Remove the specified device from the profile's ignore list."""
        from .models import IgnoredDevice

        profile_id = request.POST.get("profile_id")
        source_id = request.POST.get("source_id")
        next_url = _safe_next_url(request, "plugins:netbox_data_import:import_preview")

        if profile_id and source_id:
            IgnoredDevice.objects.filter(
                profile_id=profile_id,
                source_id=source_id,
            ).delete()
            messages.success(request, "Device removed from ignore list.")
        return redirect(next_url)


# ---------------------------------------------------------------------------
# Save resolution (rerere)
# ---------------------------------------------------------------------------


class SaveResolutionView(LoginRequiredMixin, View):
    """Save a manual field resolution for rerere replay."""

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


class DeviceTypeAnalysisView(LoginRequiredMixin, View):
    """Show all unique (make, model) pairs across import jobs and profiles.

    Highlights which ones have explicit DeviceTypeMapping vs auto-slugified.
    """

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


class BulkYamlImportView(LoginRequiredMixin, View):
    """Accept a YAML file and bulk-create ClassRoleMappings or DeviceTypeMappings for a profile.

    Useful for bootstrapping from contrib/ definition files.
    """

    def get(self, request, profile_pk):
        """Render the bulk YAML import form."""
        profile = get_object_or_404(ImportProfile, pk=profile_pk)
        return render(request, "netbox_data_import/bulk_yaml_import.html", {"profile": profile})

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
        except Exception as exc:
            messages.error(request, f"Failed to parse YAML: {exc}")
            return render(request, "netbox_data_import/bulk_yaml_import.html", {"profile": profile})

        if not isinstance(data, list):
            messages.error(request, "YAML must be a list of mapping objects.")
            return render(request, "netbox_data_import/bulk_yaml_import.html", {"profile": profile})

        created = 0
        skipped = 0
        errors = []

        if mapping_type == "class_role":
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
                except Exception as exc:
                    errors.append(str(exc))
        elif mapping_type == "device_type":
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
                except Exception as exc:
                    errors.append(str(exc))

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


class ExportProfileYamlView(LoginRequiredMixin, View):
    """Download all profile configuration as a single YAML file."""

    def get(self, request, pk):
        """Serialize the profile and all its mappings to YAML and return as a file download."""
        from django.http import HttpResponse
        import yaml

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
                    "source_class": m.source_class,
                    "creates_rack": m.creates_rack,
                    "role_slug": m.role_slug,
                    "ignore": m.ignore,
                }
                for m in profile.class_role_mappings.all()
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


class ImportProfileYamlView(LoginRequiredMixin, View):
    """Import a full profile YAML (as exported by ExportProfileYamlView).

    If the profile already exists (by name), merges/updates its mappings.
    """

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

        if not isinstance(data, dict) or "profile" not in data:
            messages.error(request, "YAML must contain a top-level 'profile' key.")
            return render(request, "netbox_data_import/import_profile_yaml.html")

        pdata = data["profile"]
        profile, _ = ImportProfile.objects.update_or_create(
            name=pdata["name"],
            defaults={
                "description": pdata.get("description", ""),
                "sheet_name": pdata.get("sheet_name", "Data"),
                "source_id_column": pdata.get("source_id_column", ""),
                "custom_field_name": pdata.get("custom_field_name", ""),
                "update_existing": pdata.get("update_existing", True),
                "create_missing_device_types": pdata.get("create_missing_device_types", True),
                "preview_view_mode": pdata.get("preview_view_mode", "rows"),
            },
        )

        stats = {}
        for cm in data.get("column_mappings", []):
            _, c = ColumnMapping.objects.update_or_create(
                profile=profile,
                target_field=cm["target_field"],
                defaults={"source_column": cm["source_column"]},
            )
            stats["column_mappings"] = stats.get("column_mappings", 0) + 1

        for m in data.get("class_role_mappings", []):
            ClassRoleMapping.objects.update_or_create(
                profile=profile,
                source_class=m["source_class"],
                defaults={
                    "creates_rack": m.get("creates_rack", False),
                    "role_slug": m.get("role_slug", ""),
                    "ignore": m.get("ignore", False),
                },
            )
            stats["class_role_mappings"] = stats.get("class_role_mappings", 0) + 1

        for m in data.get("device_type_mappings", []):
            DeviceTypeMapping.objects.update_or_create(
                profile=profile,
                source_make=m["source_make"],
                source_model=m["source_model"],
                defaults={
                    "netbox_manufacturer_slug": m["netbox_manufacturer_slug"],
                    "netbox_device_type_slug": m["netbox_device_type_slug"],
                },
            )
            stats["device_type_mappings"] = stats.get("device_type_mappings", 0) + 1

        for m in data.get("manufacturer_mappings", []):
            ManufacturerMapping.objects.update_or_create(
                profile=profile,
                source_make=m["source_make"],
                defaults={"netbox_manufacturer_slug": m["netbox_manufacturer_slug"]},
            )
            stats["manufacturer_mappings"] = stats.get("manufacturer_mappings", 0) + 1

        from .models import ColumnTransformRule

        for r in data.get("column_transform_rules", []):
            ColumnTransformRule.objects.update_or_create(
                profile=profile,
                source_column=r["source_column"],
                defaults={
                    "pattern": r["pattern"],
                    "group_1_target": r.get("group_1_target", ""),
                    "group_2_target": r.get("group_2_target", ""),
                },
            )
            stats["column_transform_rules"] = stats.get("column_transform_rules", 0) + 1

        summary = ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in stats.items())
        messages.success(request, f"Profile '{profile.name}' imported/updated. {summary}.")
        return redirect(profile.get_absolute_url())


# ---------------------------------------------------------------------------


class CheckDeviceNameView(LoginRequiredMixin, View):
    """AJAX endpoint: check if a device with the given name exists in NetBox.

    Returns JSON: {"exists": bool, "url": str|null, "id": int|null}.
    """

    def get(self, request):
        """Return JSON indicating whether a device with the given name exists."""
        from django.http import JsonResponse
        from dcim.models import Device

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


class SourceResolutionListView(LoginRequiredMixin, View):
    """List all saved name-split resolutions for a profile."""

    def get(self, request, profile_pk):
        """Render the list of saved source resolutions for the given profile."""
        from .models import SourceResolution

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


class SourceResolutionDeleteView(LoginRequiredMixin, View):
    """Delete a saved source resolution."""

    def get(self, request, pk):
        """Render the delete confirmation page for a source resolution."""
        from .models import SourceResolution

        obj = get_object_or_404(SourceResolution, pk=pk)
        return render(
            request,
            "netbox_data_import/confirm_delete.html",
            {
                "object": obj,
                "return_url": reverse("plugins:netbox_data_import:source_resolution_list", args=[obj.profile_id]),
            },
        )

    def post(self, request, pk):
        """Delete the source resolution and redirect to the profile's resolution list."""
        from .models import SourceResolution

        obj = get_object_or_404(SourceResolution, pk=pk)
        profile_pk = obj.profile_id
        obj.delete()
        messages.success(request, "Resolution deleted.")
        return redirect(reverse("plugins:netbox_data_import:source_resolution_list", args=[profile_pk]))


# ---------------------------------------------------------------------------
# Quick-resolve views (inline fixes from preview page)
# ---------------------------------------------------------------------------


class QuickCreateManufacturerView(LoginRequiredMixin, View):
    """Immediately create a Manufacturer in NetBox from the preview page.

    Redirects back to preview so the row changes from 'create' to a device action.
    """

    def post(self, request):
        """Create the manufacturer in NetBox and redirect back to preview."""
        from dcim.models import Manufacturer

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


class QuickResolveManufacturerView(LoginRequiredMixin, View):
    """Save a ManufacturerMapping (source make → NetBox manufacturer slug) from the preview page.

    Used when a source has inconsistent naming (e.g. 'Dell EMC' → 'dell').
    Redirects back to preview which re-runs with the mapping applied.
    """

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


class QuickResolveDeviceTypeView(LoginRequiredMixin, View):
    """Save a DeviceTypeMapping (source make/model → NetBox slugs) from the preview page.

    Optionally also creates the manufacturer and/or device type in NetBox right now.
    Redirects back to preview which re-runs and shows the resolved rows.
    """

    def post(self, request):
        """Save the device type mapping (and optionally create objects) then redirect."""
        from dcim.models import Manufacturer, DeviceType
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


class QuickAddClassRoleMappingView(LoginRequiredMixin, View):
    """Quickly add a ClassRoleMapping (ignore / role) directly from an error row in preview.

    Redirects back to preview; error rows for that class disappear on re-run.
    """

    def post(self, request):
        """Save the class→role mapping and redirect back to preview."""
        profile_id = request.POST.get("profile_id")
        profile = get_object_or_404(ImportProfile, pk=profile_id)
        source_class = request.POST.get("source_class", "").strip()
        mapping_action = request.POST.get("mapping_action", "ignore")  # "ignore" or "role"
        role_slug = request.POST.get("role_slug", "").strip()
        creates_rack = request.POST.get("creates_rack") == "1"

        if not source_class:
            messages.error(request, "Source class is required.")
            return redirect(reverse("plugins:netbox_data_import:import_preview"))

        _, created = ClassRoleMapping.objects.update_or_create(
            profile=profile,
            source_class=source_class,
            defaults={
                "ignore": mapping_action == "ignore",
                "creates_rack": creates_rack,
                "role_slug": role_slug if mapping_action == "role" else "",
            },
        )
        verb = "Created" if created else "Updated"
        if mapping_action == "ignore":
            action_label = "ignore"
        elif mapping_action == "rack":
            action_label = "creates rack"
        else:
            action_label = f"role '{role_slug}'"
        messages.success(request, f"{verb} mapping: class '{source_class}' → {action_label}")
        return redirect(reverse("plugins:netbox_data_import:import_preview"))


class MatchExistingDeviceView(LoginRequiredMixin, View):
    """Link a source row to an existing NetBox device (by device ID).

    Saves a DeviceExistingMatch; on next preview re-run the row shows action='update'.
    """

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


class SearchNetBoxObjectsView(LoginRequiredMixin, View):
    """AJAX search endpoint for NetBox objects used in preview quick-fix modals.

    GET params: type (manufacturer|device_type|device|role), q (search string).
    Returns JSON list of {id, name, slug, url} dicts.
    """

    def get(self, request):
        """Return a JSON list of matching NetBox objects for the given type and query."""
        from django.http import JsonResponse
        from dcim.models import Manufacturer, DeviceType, Device, DeviceRole

        obj_type = request.GET.get("type", "device")
        q = request.GET.get("q", "").strip()
        limit = 20

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
            for dev in Device.objects.filter(name__icontains=q).select_related("site")[:limit]:
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

        return JsonResponse({"results": results})


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


class AutoMatchDevicesView(LoginRequiredMixin, View):
    """Scan all device rows in the session and auto-match to existing NetBox devices.

    Priority: serial > asset_tag > exact name match.
    Name substring matches are recorded as probable_matches only (not auto-linked).
    """

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
