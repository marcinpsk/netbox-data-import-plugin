# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""DRF viewsets for the data-import plugin API."""

from django.http import Http404
from netbox.api.viewsets import NetBoxModelViewSet, NetBoxReadOnlyModelViewSet
from rest_framework import viewsets, permissions
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import DjangoModelPermissions

from ..models import (
    locked_profile_policy,
    locked_resolution_policy,
    ImportProfile,
    ColumnMapping,
    ClassRoleMapping,
    DeviceTypeMapping,
    IgnoredDevice,
    ColumnTransformRule,
    SourceResolution,
    ImportExecution,
    InferenceBackend,
    ResolutionProposal,
)
from .serializers import (
    ImportProfileSerializer,
    ColumnMappingSerializer,
    ClassRoleMappingSerializer,
    DeviceTypeMappingSerializer,
    IgnoredDeviceSerializer,
    ColumnTransformRuleSerializer,
    SourceResolutionSerializer,
    ImportExecutionSerializer,
    InferenceBackendSerializer,
    ResolutionProposalSerializer,
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
        "tags", "column_mappings", "class_role_mappings", "device_type_mappings"
    )
    serializer_class = ImportProfileSerializer


class _ProfileScopedQuerySetMixin(viewsets.GenericViewSet):
    """Restrict profile-owned rows and validate their optional profile filter."""

    def get_queryset(self):
        """Return viewable rows, filtered by a valid profile ID when supplied."""
        qs = super().get_queryset().restrict(self.request.user, "view")
        profile_id = self.request.query_params.get("profile_id")
        if profile_id is not None:
            try:
                profile_id = int(profile_id)
            except (TypeError, ValueError) as exc:
                raise ValidationError({"profile_id": "Enter a whole number."}) from exc
            qs = qs.filter(profile_id=profile_id)
        return qs


class _PluginModelViewSet(_ProfileScopedQuerySetMixin, viewsets.ModelViewSet):
    """Base class for plain-model viewsets in this plugin."""

    permission_classes = [permissions.IsAuthenticated, DjangoModelPermissionsWithView]


class ColumnMappingViewSet(_PluginModelViewSet):
    """CRUD viewset for ColumnMapping."""

    queryset = ColumnMapping.objects.select_related("profile")
    serializer_class = ColumnMappingSerializer


class ClassRoleMappingViewSet(_PluginModelViewSet):
    """CRUD viewset for ClassRoleMapping."""

    queryset = ClassRoleMapping.objects.select_related("profile", "rack_type")
    serializer_class = ClassRoleMappingSerializer


class DeviceTypeMappingViewSet(_PluginModelViewSet):
    """CRUD viewset for DeviceTypeMapping."""

    queryset = DeviceTypeMapping.objects.select_related("profile")
    serializer_class = DeviceTypeMappingSerializer


class IgnoredDeviceViewSet(_PluginModelViewSet):
    """CRUD viewset for IgnoredDevice."""

    queryset = IgnoredDevice.objects.select_related("profile")
    serializer_class = IgnoredDeviceSerializer


class ColumnTransformRuleViewSet(_PluginModelViewSet):
    """CRUD viewset for ColumnTransformRule."""

    queryset = ColumnTransformRule.objects.select_related("profile")
    serializer_class = ColumnTransformRuleSerializer


def _revalidate_against_the_stored_row(serializer):
    """Read the resolution again and check the request against the row as it now stands.

    save() writes every field, so a request that changed another field first would otherwise be
    undone. The whole validation runs again rather than validate() alone, because the field checks
    read the stored row too, and the profile lock makes this reading of it authoritative. The
    result is discarded: the values are the request's own, which the first pass already holds.
    """
    serializer.instance = SourceResolution.objects.get(pk=serializer.instance.pk)
    serializer.run_validation(serializer.initial_data)


class SourceResolutionViewSet(_PluginModelViewSet):
    """CRUD viewset for SourceResolution (rerere)."""

    queryset = SourceResolution.objects.select_related("profile")
    serializer_class = SourceResolutionSerializer

    # Each write serializes against an executing import, which holds the same profile row.
    def perform_create(self, serializer):
        """Create the resolution under the profile lock."""
        try:
            with locked_profile_policy(serializer.validated_data["profile"].pk):
                serializer.save()
        except ImportProfile.DoesNotExist:
            # The profile is read to validate the request, and can be deleted before the lock.
            raise Http404 from None

    def perform_update(self, serializer):
        """Update the resolution under its profile lock."""
        # ValidatedModelSerializer.validate() writes the request values onto the instance, so only
        # its primary key still names the stored row.
        try:
            with locked_resolution_policy(serializer.instance.pk):
                _revalidate_against_the_stored_row(serializer)
                serializer.save()
        except (SourceResolution.DoesNotExist, ImportProfile.DoesNotExist):
            raise Http404 from None

    def perform_destroy(self, instance):
        """Delete the resolution under its profile lock."""
        try:
            with locked_resolution_policy(instance.pk):
                instance.delete()
        except (SourceResolution.DoesNotExist, ImportProfile.DoesNotExist):
            raise Http404 from None


class ImportExecutionViewSet(_ProfileScopedQuerySetMixin, viewsets.ReadOnlyModelViewSet):
    """Read-only viewset for the Import Execution audit history."""

    queryset = ImportExecution.objects.select_related("profile")
    serializer_class = ImportExecutionSerializer
    permission_classes = [permissions.IsAuthenticated, DjangoModelPermissionsWithView]


class ResolutionProposalViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only viewset for Resolution Proposal history."""

    queryset = ResolutionProposal.objects.all()
    serializer_class = ResolutionProposalSerializer
    permission_classes = [permissions.IsAuthenticated, DjangoModelPermissionsWithView]

    def get_queryset(self):
        """Restrict proposal history to the viewer and an optional profile_id."""
        qs = super().get_queryset().restrict(self.request.user, "view").select_related("profile")
        profile_id = self.request.query_params.get("profile_id")
        if profile_id is not None:
            try:
                profile_id = int(profile_id)
            except ValueError:
                raise ValidationError({"profile_id": ["Enter a valid integer."]}) from None
            qs = qs.filter(profile_id=profile_id)
        return qs


class InferenceBackendViewSet(NetBoxReadOnlyModelViewSet):
    """Read-only viewset for Inference Backend rows.

    Read-only on purpose: a backend row carries the destination NetBox itself calls, and the UI form
    is the one place that validates the `api_root` trust boundary against the allowlist.
    """

    queryset = InferenceBackend.objects.prefetch_related("tags")
    serializer_class = InferenceBackendSerializer
