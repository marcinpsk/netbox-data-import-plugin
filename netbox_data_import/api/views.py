# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""DRF viewsets for the data-import plugin API."""

from netbox.api.viewsets import NetBoxModelViewSet
from rest_framework import viewsets, permissions
from rest_framework.permissions import DjangoModelPermissions

from ..models import (
    ImportProfile,
    ColumnMapping,
    ClassRoleMapping,
    DeviceTypeMapping,
    IgnoredDevice,
    ColumnTransformRule,
    SourceResolution,
    ImportJob,
)
from .serializers import (
    ImportProfileSerializer,
    ColumnMappingSerializer,
    ClassRoleMappingSerializer,
    DeviceTypeMappingSerializer,
    IgnoredDeviceSerializer,
    ColumnTransformRuleSerializer,
    SourceResolutionSerializer,
    ImportJobSerializer,
)


class DjangoModelPermissionsWithView(DjangoModelPermissions):
    """Extends DjangoModelPermissions to require view_* permission for GET requests.

    The stock DjangoModelPermissions does not map GET to any model permission,
    so list/retrieve endpoints are accessible to any authenticated user.  This
    subclass closes that gap.
    """

    perms_map = {
        **DjangoModelPermissions.perms_map,
        "GET": ["%(app_label)s.view_%(model_name)s"],
        "HEAD": ["%(app_label)s.view_%(model_name)s"],
        "OPTIONS": [],
    }


class ImportProfileViewSet(NetBoxModelViewSet):
    """CRUD viewset for ImportProfile (NetBoxModel)."""

    queryset = ImportProfile.objects.prefetch_related(
        "tags",
        "column_mappings",
        "class_role_mappings",
        "device_type_mappings",
    )
    serializer_class = ImportProfileSerializer


class _PluginModelViewSet(viewsets.ModelViewSet):
    """Base class for plain-model viewsets in this plugin."""

    permission_classes = [permissions.IsAuthenticated, DjangoModelPermissionsWithView]


class ColumnMappingViewSet(_PluginModelViewSet):
    """CRUD viewset for ColumnMapping."""

    queryset = ColumnMapping.objects.select_related("profile")
    serializer_class = ColumnMappingSerializer

    def get_queryset(self):
        """Filter by profile_id query param if provided."""
        qs = super().get_queryset()
        profile_id = self.request.query_params.get("profile_id")
        if profile_id:
            qs = qs.filter(profile_id=profile_id)
        return qs


class ClassRoleMappingViewSet(_PluginModelViewSet):
    """CRUD viewset for ClassRoleMapping."""

    queryset = ClassRoleMapping.objects.select_related("profile")
    serializer_class = ClassRoleMappingSerializer

    def get_queryset(self):
        """Filter by profile_id query param if provided."""
        qs = super().get_queryset()
        profile_id = self.request.query_params.get("profile_id")
        if profile_id:
            qs = qs.filter(profile_id=profile_id)
        return qs


class DeviceTypeMappingViewSet(_PluginModelViewSet):
    """CRUD viewset for DeviceTypeMapping."""

    queryset = DeviceTypeMapping.objects.select_related("profile")
    serializer_class = DeviceTypeMappingSerializer

    def get_queryset(self):
        """Filter by profile_id query param if provided."""
        qs = super().get_queryset()
        profile_id = self.request.query_params.get("profile_id")
        if profile_id:
            qs = qs.filter(profile_id=profile_id)
        return qs


class IgnoredDeviceViewSet(_PluginModelViewSet):
    """CRUD viewset for IgnoredDevice."""

    queryset = IgnoredDevice.objects.select_related("profile")
    serializer_class = IgnoredDeviceSerializer

    def get_queryset(self):
        """Filter by profile_id query param if provided."""
        qs = super().get_queryset()
        profile_id = self.request.query_params.get("profile_id")
        if profile_id:
            qs = qs.filter(profile_id=profile_id)
        return qs


class ColumnTransformRuleViewSet(_PluginModelViewSet):
    """CRUD viewset for ColumnTransformRule."""

    queryset = ColumnTransformRule.objects.select_related("profile")
    serializer_class = ColumnTransformRuleSerializer

    def get_queryset(self):
        """Filter by profile_id query param if provided."""
        qs = super().get_queryset()
        profile_id = self.request.query_params.get("profile_id")
        if profile_id:
            qs = qs.filter(profile_id=profile_id)
        return qs


class SourceResolutionViewSet(_PluginModelViewSet):
    """CRUD viewset for SourceResolution (rerere)."""

    queryset = SourceResolution.objects.select_related("profile")
    serializer_class = SourceResolutionSerializer

    def get_queryset(self):
        """Filter by profile_id query param if provided."""
        qs = super().get_queryset()
        profile_id = self.request.query_params.get("profile_id")
        if profile_id:
            qs = qs.filter(profile_id=profile_id)
        return qs


class ImportJobViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only viewset for ImportJob history."""

    queryset = ImportJob.objects.select_related("profile")
    serializer_class = ImportJobSerializer
    permission_classes = [permissions.IsAuthenticated, DjangoModelPermissionsWithView]

    def get_queryset(self):
        """Filter by profile_id query param if provided."""
        qs = super().get_queryset()
        profile_id = self.request.query_params.get("profile_id")
        if profile_id:
            qs = qs.filter(profile_id=profile_id)
        return qs
