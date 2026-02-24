# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View
from netbox.views import generic
from .models import ImportProfile, ColumnMapping, ClassRoleMapping, DeviceTypeMapping
from .forms import (
    ImportProfileForm,
    ColumnMappingForm,
    ClassRoleMappingForm,
    DeviceTypeMappingForm,
    ImportSetupForm,
)
from .tables import (
    ImportProfileTable,
    ColumnMappingTable,
    ClassRoleMappingTable,
    DeviceTypeMappingTable,
)
from .filters import ImportProfileFilterSet


# ---------------------------------------------------------------------------
# ImportProfile
# ---------------------------------------------------------------------------

class ImportProfileListView(generic.ObjectListView):
    queryset = ImportProfile.objects.prefetch_related("column_mappings", "class_role_mappings", "device_type_mappings")
    table = ImportProfileTable
    filterset = ImportProfileFilterSet


class ImportProfileView(generic.ObjectView):
    queryset = ImportProfile.objects.prefetch_related("column_mappings", "class_role_mappings", "device_type_mappings")

    def get_extra_context(self, request, instance):
        column_table = ColumnMappingTable(instance.column_mappings.all())
        class_role_table = ClassRoleMappingTable(instance.class_role_mappings.all())
        device_type_table = DeviceTypeMappingTable(instance.device_type_mappings.all())
        return {
            "column_table": column_table,
            "class_role_table": class_role_table,
            "device_type_table": device_type_table,
        }


class ImportProfileEditView(generic.ObjectEditView):
    queryset = ImportProfile.objects.all()
    form = ImportProfileForm


class ImportProfileDeleteView(generic.ObjectDeleteView):
    queryset = ImportProfile.objects.all()


# ---------------------------------------------------------------------------
# ColumnMapping CRUD
# ---------------------------------------------------------------------------

class ColumnMappingAddView(LoginRequiredMixin, View):
    def get(self, request, profile_pk):
        profile = get_object_or_404(ImportProfile, pk=profile_pk)
        form = ColumnMappingForm(initial={"profile": profile})
        return render(request, "netbox_data_import/columnmapping_edit.html", {"form": form, "profile": profile})

    def post(self, request, profile_pk):
        profile = get_object_or_404(ImportProfile, pk=profile_pk)
        form = ColumnMappingForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Column mapping added.")
            return redirect(profile.get_absolute_url())
        return render(request, "netbox_data_import/columnmapping_edit.html", {"form": form, "profile": profile})


class ColumnMappingEditView(LoginRequiredMixin, View):
    def get(self, request, pk):
        obj = get_object_or_404(ColumnMapping, pk=pk)
        form = ColumnMappingForm(instance=obj)
        return render(request, "netbox_data_import/columnmapping_edit.html", {"form": form, "profile": obj.profile, "object": obj})

    def post(self, request, pk):
        obj = get_object_or_404(ColumnMapping, pk=pk)
        form = ColumnMappingForm(request.POST, instance=obj)
        if form.is_valid():
            form.save()
            messages.success(request, "Column mapping updated.")
            return redirect(obj.profile.get_absolute_url())
        return render(request, "netbox_data_import/columnmapping_edit.html", {"form": form, "profile": obj.profile, "object": obj})


class ColumnMappingDeleteView(LoginRequiredMixin, View):
    def get(self, request, pk):
        obj = get_object_or_404(ColumnMapping, pk=pk)
        return render(request, "netbox_data_import/confirm_delete.html", {"object": obj, "return_url": obj.profile.get_absolute_url()})

    def post(self, request, pk):
        obj = get_object_or_404(ColumnMapping, pk=pk)
        profile_url = obj.profile.get_absolute_url()
        obj.delete()
        messages.success(request, "Column mapping deleted.")
        return redirect(profile_url)


# ---------------------------------------------------------------------------
# ClassRoleMapping CRUD
# ---------------------------------------------------------------------------

class ClassRoleMappingAddView(LoginRequiredMixin, View):
    def get(self, request, profile_pk):
        profile = get_object_or_404(ImportProfile, pk=profile_pk)
        form = ClassRoleMappingForm(initial={"profile": profile})
        return render(request, "netbox_data_import/classrolemapping_edit.html", {"form": form, "profile": profile})

    def post(self, request, profile_pk):
        profile = get_object_or_404(ImportProfile, pk=profile_pk)
        form = ClassRoleMappingForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Class→Role mapping added.")
            return redirect(profile.get_absolute_url())
        return render(request, "netbox_data_import/classrolemapping_edit.html", {"form": form, "profile": profile})


class ClassRoleMappingEditView(LoginRequiredMixin, View):
    def get(self, request, pk):
        obj = get_object_or_404(ClassRoleMapping, pk=pk)
        form = ClassRoleMappingForm(instance=obj)
        return render(request, "netbox_data_import/classrolemapping_edit.html", {"form": form, "profile": obj.profile, "object": obj})

    def post(self, request, pk):
        obj = get_object_or_404(ClassRoleMapping, pk=pk)
        form = ClassRoleMappingForm(request.POST, instance=obj)
        if form.is_valid():
            form.save()
            messages.success(request, "Class→Role mapping updated.")
            return redirect(obj.profile.get_absolute_url())
        return render(request, "netbox_data_import/classrolemapping_edit.html", {"form": form, "profile": obj.profile, "object": obj})


class ClassRoleMappingDeleteView(LoginRequiredMixin, View):
    def get(self, request, pk):
        obj = get_object_or_404(ClassRoleMapping, pk=pk)
        return render(request, "netbox_data_import/confirm_delete.html", {"object": obj, "return_url": obj.profile.get_absolute_url()})

    def post(self, request, pk):
        obj = get_object_or_404(ClassRoleMapping, pk=pk)
        profile_url = obj.profile.get_absolute_url()
        obj.delete()
        messages.success(request, "Class→Role mapping deleted.")
        return redirect(profile_url)


# ---------------------------------------------------------------------------
# DeviceTypeMapping CRUD
# ---------------------------------------------------------------------------

class DeviceTypeMappingAddView(LoginRequiredMixin, View):
    def get(self, request, profile_pk):
        profile = get_object_or_404(ImportProfile, pk=profile_pk)
        form = DeviceTypeMappingForm(initial={"profile": profile})
        return render(request, "netbox_data_import/devicetypemapping_edit.html", {"form": form, "profile": profile})

    def post(self, request, profile_pk):
        profile = get_object_or_404(ImportProfile, pk=profile_pk)
        form = DeviceTypeMappingForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Device type mapping added.")
            return redirect(profile.get_absolute_url())
        return render(request, "netbox_data_import/devicetypemapping_edit.html", {"form": form, "profile": profile})


class DeviceTypeMappingEditView(LoginRequiredMixin, View):
    def get(self, request, pk):
        obj = get_object_or_404(DeviceTypeMapping, pk=pk)
        form = DeviceTypeMappingForm(instance=obj)
        return render(request, "netbox_data_import/devicetypemapping_edit.html", {"form": form, "profile": obj.profile, "object": obj})

    def post(self, request, pk):
        obj = get_object_or_404(DeviceTypeMapping, pk=pk)
        form = DeviceTypeMappingForm(request.POST, instance=obj)
        if form.is_valid():
            form.save()
            messages.success(request, "Device type mapping updated.")
            return redirect(obj.profile.get_absolute_url())
        return render(request, "netbox_data_import/devicetypemapping_edit.html", {"form": form, "profile": obj.profile, "object": obj})


class DeviceTypeMappingDeleteView(LoginRequiredMixin, View):
    def get(self, request, pk):
        obj = get_object_or_404(DeviceTypeMapping, pk=pk)
        return render(request, "netbox_data_import/confirm_delete.html", {"object": obj, "return_url": obj.profile.get_absolute_url()})

    def post(self, request, pk):
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
        form = ImportSetupForm()
        return render(request, "netbox_data_import/import_setup.html", {"form": form})

    def post(self, request):
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
        session_data = request.session.get("import_result")
        if not session_data:
            messages.warning(request, "No import in progress. Please start a new import.")
            return redirect(reverse("plugins:netbox_data_import:import_setup"))

        from . import engine

        result = engine.ImportResult.from_session_dict(session_data)
        ctx = request.session.get("import_context", {})
        return render(request, "netbox_data_import/import_preview.html", {
            "result": result,
            "filename": ctx.get("filename", ""),
            "profile_id": ctx.get("profile_id"),
        })


class ImportRunView(LoginRequiredMixin, View):
    """Step 3: run the real import (dry_run=False)."""

    def post(self, request):
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

        request.session["import_result"] = result.to_session_dict()
        messages.success(request, f"Import complete: {result.counts.get('devices_created', 0)} devices created, "
                                   f"{result.counts.get('racks_created', 0)} racks created.")
        return redirect(reverse("plugins:netbox_data_import:import_results"))


class ImportResultsView(LoginRequiredMixin, View):
    """Step 4: show final results with links to created objects."""

    def get(self, request):
        session_data = request.session.get("import_result")
        if not session_data:
            return redirect(reverse("plugins:netbox_data_import:import_setup"))

        from . import engine

        result = engine.ImportResult.from_session_dict(session_data)
        return render(request, "netbox_data_import/import_results.html", {"result": result})


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
