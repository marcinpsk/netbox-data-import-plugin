# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
from django.urls import path
from . import views

urlpatterns = [
    # Import Profiles
    path("profiles/", views.ImportProfileListView.as_view(), name="importprofile_list"),
    path("profiles/add/", views.ImportProfileEditView.as_view(), name="importprofile_add"),
    path("profiles/<int:pk>/", views.ImportProfileView.as_view(), name="importprofile"),
    path("profiles/<int:pk>/edit/", views.ImportProfileEditView.as_view(), name="importprofile_edit"),
    path("profiles/<int:pk>/delete/", views.ImportProfileDeleteView.as_view(), name="importprofile_delete"),

    # Column Mappings
    path("profiles/<int:profile_pk>/columns/add/", views.ColumnMappingAddView.as_view(), name="columnmapping_add"),
    path("column-mappings/<int:pk>/edit/", views.ColumnMappingEditView.as_view(), name="columnmapping_edit"),
    path("column-mappings/<int:pk>/delete/", views.ColumnMappingDeleteView.as_view(), name="columnmapping_delete"),

    # Class → Role Mappings
    path("profiles/<int:profile_pk>/class-roles/add/", views.ClassRoleMappingAddView.as_view(), name="classrolemapping_add"),
    path("class-role-mappings/<int:pk>/edit/", views.ClassRoleMappingEditView.as_view(), name="classrolemapping_edit"),
    path("class-role-mappings/<int:pk>/delete/", views.ClassRoleMappingDeleteView.as_view(), name="classrolemapping_delete"),

    # Device Type Mappings
    path("profiles/<int:profile_pk>/device-types/add/", views.DeviceTypeMappingAddView.as_view(), name="devicetypemapping_add"),
    path("device-type-mappings/<int:pk>/edit/", views.DeviceTypeMappingEditView.as_view(), name="devicetypemapping_edit"),
    path("device-type-mappings/<int:pk>/delete/", views.DeviceTypeMappingDeleteView.as_view(), name="devicetypemapping_delete"),

    # Import Wizard
    path("import/", views.ImportSetupView.as_view(), name="import_setup"),
    path("import/preview/", views.ImportPreviewView.as_view(), name="import_preview"),
    path("import/run/", views.ImportRunView.as_view(), name="import_run"),
    path("import/results/", views.ImportResultsView.as_view(), name="import_results"),
]
