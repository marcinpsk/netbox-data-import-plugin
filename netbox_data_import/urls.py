# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
from django.urls import path
from . import views

urlpatterns = [
    # Import Profiles
    path("profiles/", views.ImportProfileListView.as_view(), name="importprofile_list"),
    path("profiles/add/", views.ImportProfileEditView.as_view(), name="importprofile_add"),
    path("profiles/import/", views.ImportProfileBulkImportView.as_view(), name="importprofile_bulk_import"),
    path("profiles/<int:pk>/", views.ImportProfileView.as_view(), name="importprofile"),
    path("profiles/<int:pk>/edit/", views.ImportProfileEditView.as_view(), name="importprofile_edit"),
    path("profiles/<int:pk>/delete/", views.ImportProfileDeleteView.as_view(), name="importprofile_delete"),
    # Column Mappings
    path("profiles/<int:profile_pk>/columns/add/", views.ColumnMappingAddView.as_view(), name="columnmapping_add"),
    path("column-mappings/<int:pk>/edit/", views.ColumnMappingEditView.as_view(), name="columnmapping_edit"),
    path("column-mappings/<int:pk>/delete/", views.ColumnMappingDeleteView.as_view(), name="columnmapping_delete"),
    # Class → Role Mappings
    path(
        "profiles/<int:profile_pk>/class-roles/add/",
        views.ClassRoleMappingAddView.as_view(),
        name="classrolemapping_add",
    ),
    path("class-role-mappings/<int:pk>/edit/", views.ClassRoleMappingEditView.as_view(), name="classrolemapping_edit"),
    path(
        "class-role-mappings/<int:pk>/delete/",
        views.ClassRoleMappingDeleteView.as_view(),
        name="classrolemapping_delete",
    ),
    # Device Type Mappings
    path(
        "profiles/<int:profile_pk>/device-types/add/",
        views.DeviceTypeMappingAddView.as_view(),
        name="devicetypemapping_add",
    ),
    path(
        "device-type-mappings/<int:pk>/edit/", views.DeviceTypeMappingEditView.as_view(), name="devicetypemapping_edit"
    ),
    path(
        "device-type-mappings/<int:pk>/delete/",
        views.DeviceTypeMappingDeleteView.as_view(),
        name="devicetypemapping_delete",
    ),
    # Column Transform Rules
    path(
        "profiles/<int:profile_pk>/transforms/add/",
        views.ColumnTransformRuleAddView.as_view(),
        name="columntransformrule_add",
    ),
    path(
        "column-transforms/<int:pk>/edit/", views.ColumnTransformRuleEditView.as_view(), name="columntransformrule_edit"
    ),
    path(
        "column-transforms/<int:pk>/delete/",
        views.ColumnTransformRuleDeleteView.as_view(),
        name="columntransformrule_delete",
    ),
    # Import Wizard
    path("import/", views.ImportSetupView.as_view(), name="import_setup"),
    path("import/preview/", views.ImportPreviewView.as_view(), name="import_preview"),
    path("import/run/", views.ImportRunView.as_view(), name="import_run"),
    path("import/results/", views.ImportResultsView.as_view(), name="import_results"),
    # Ignore / Unignore device
    path("ignore-device/", views.IgnoreDeviceView.as_view(), name="ignore_device"),
    path("unignore-device/", views.UnignoreDeviceView.as_view(), name="unignore_device"),
    # Quick-resolve views
    path("remove-extra-ip/", views.RemoveExtraIpView.as_view(), name="remove_extra_ip"),
    path("sync-device-field/", views.SyncDeviceFieldView.as_view(), name="sync_device_field"),
    # Save resolution (rerere)
    path("save-resolution/", views.SaveResolutionView.as_view(), name="save_resolution"),
    # Source resolutions list (per profile)
    path(
        "profiles/<int:profile_pk>/resolutions/",
        views.SourceResolutionListView.as_view(),
        name="source_resolution_list",
    ),
    path(
        "source-resolutions/<int:pk>/delete/",
        views.SourceResolutionDeleteView.as_view(),
        name="source_resolution_delete",
    ),
    # Device type analysis
    path("analysis/", views.DeviceTypeAnalysisView.as_view(), name="device_type_analysis"),
    path("analysis/<int:profile_pk>/", views.DeviceTypeAnalysisView.as_view(), name="device_type_analysis_profile"),
    # Bulk YAML import
    path("profiles/<int:profile_pk>/bulk-yaml/", views.BulkYamlImportView.as_view(), name="bulk_yaml_import"),
    # Profile YAML export / full-profile import
    path("profiles/<int:pk>/export-yaml/", views.ExportProfileYamlView.as_view(), name="exportprofile_yaml"),
    path("import-profile-yaml/", views.ImportProfileYamlView.as_view(), name="import_profile_yaml"),
    # AJAX helpers
    path("check-device/", views.CheckDeviceNameView.as_view(), name="check_device"),
    path("search-objects/", views.SearchNetBoxObjectsView.as_view(), name="search_objects"),
    # Quick-resolve views (POST from preview inline fix buttons)
    path("quick-create-manufacturer/", views.QuickCreateManufacturerView.as_view(), name="quick_create_manufacturer"),
    path(
        "quick-resolve-manufacturer/", views.QuickResolveManufacturerView.as_view(), name="quick_resolve_manufacturer"
    ),
    path("quick-resolve-device-type/", views.QuickResolveDeviceTypeView.as_view(), name="quick_resolve_device_type"),
    path("quick-add-class-mapping/", views.QuickAddClassRoleMappingView.as_view(), name="quick_add_class_mapping"),
    path("quick-add-column-mapping/", views.QuickAddColumnMappingView.as_view(), name="quick_add_column_mapping"),
    path("quick-create-role/", views.QuickCreateDeviceRoleView.as_view(), name="quick_create_role"),
    path("match-existing-device/", views.MatchExistingDeviceView.as_view(), name="match_existing_device"),
    path("auto-match-devices/", views.AutoMatchDevicesView.as_view(), name="auto_match_devices"),
    # Import Job history
    path("jobs/", views.ImportJobListView.as_view(), name="importjob_list"),
]
