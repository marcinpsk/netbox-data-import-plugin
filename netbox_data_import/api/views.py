# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""DRF viewsets for the data-import plugin API."""

from django.http import Http404
from netbox.api.viewsets import NetBoxModelViewSet
from rest_framework import mixins, permissions, viewsets
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import DjangoModelPermissions

from ..field_keys import SELECT_TERMINATION_TASK, parse_termination_field_key
from ..models import (
    locked_profile_policy,
    locked_resolution_policy,
    ImportProfile,
    CableClassMapping,
    CableImportSource,
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
from ..object_permissions import (
    ObjectPermissionDenied,
    delete_permission_scoped_objects,
    save_permission_scoped_object,
)
from .serializers import (
    ImportProfileSerializer,
    CableClassMappingSerializer,
    CableImportSourceSerializer,
    ColumnMappingSerializer,
    ClassRoleMappingSerializer,
    DeviceTypeMappingSerializer,
    IgnoredDeviceSerializer,
    ColumnTransformRuleSerializer,
    SourceResolutionSerializer,
    ImportExecutionSerializer,
    InferenceBackendSerializer,
    ResolutionProposalHistorySerializer,
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

    def perform_create(self, serializer):
        """Create one policy row inside its profile and object-permission scope."""
        model = serializer.Meta.model
        values = serializer.model_cleaned_values()
        profile = values.pop("profile")
        try:
            result = save_permission_scoped_object(
                self.request.user,
                model,
                {"pk": None, "profile": profile},
                values,
                on_existing="reject",
            )
        except ImportProfile.DoesNotExist:
            raise Http404 from None
        except ObjectPermissionDenied as exc:
            raise PermissionDenied from exc
        serializer.instance = result.instance

    def perform_update(self, serializer):
        """Update one policy row under every affected profile and object scope."""
        model = serializer.Meta.model
        row_pk = serializer.instance.pk
        stored_profile_id = model.objects.filter(pk=row_pk).values_list("profile_id", flat=True).first()
        if stored_profile_id is None:
            raise Http404
        requested_profile = serializer.validated_data.get("profile")
        requested_profile_id = requested_profile.pk if requested_profile is not None else stored_profile_id
        try:
            with locked_profile_policy(stored_profile_id, requested_profile_id):
                serializer.instance = model.objects.get(pk=row_pk, profile_id=stored_profile_id)
                serializer.run_validation(serializer.initial_data)
                result = save_permission_scoped_object(
                    self.request.user,
                    model,
                    {"pk": row_pk, "profile_id": stored_profile_id},
                    serializer.model_cleaned_values(),
                )
        except (model.DoesNotExist, ImportProfile.DoesNotExist):
            raise Http404 from None
        except ObjectPermissionDenied as exc:
            raise PermissionDenied from exc
        serializer.instance = result.instance

    def perform_destroy(self, instance):
        """Delete one policy row under its current profile and object scope."""
        model = type(instance)
        profile_id = model.objects.filter(pk=instance.pk).values_list("profile_id", flat=True).first()
        if profile_id is None:
            raise Http404
        try:
            with locked_profile_policy(profile_id):
                deleted = delete_permission_scoped_objects(
                    self.request.user,
                    model.objects.filter(pk=instance.pk, profile_id=profile_id),
                )
                if deleted != 1:
                    raise Http404
        except ImportProfile.DoesNotExist:
            raise Http404 from None
        except ObjectPermissionDenied as exc:
            raise PermissionDenied from exc


class ColumnMappingViewSet(_PluginModelViewSet):
    """CRUD viewset for ColumnMapping."""

    queryset = ColumnMapping.objects.select_related("profile")
    serializer_class = ColumnMappingSerializer


class CableClassMappingViewSet(_PluginModelViewSet):
    """CRUD viewset for CableClassMapping."""

    queryset = CableClassMapping.objects.select_related("profile")
    serializer_class = CableClassMappingSerializer


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
    serializer.instance = type(serializer.instance).objects.get(pk=serializer.instance.pk)
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


class CableImportSourceViewSet(_ProfileScopedQuerySetMixin, viewsets.ReadOnlyModelViewSet):
    """Read-only viewset for per-Cable import provenance."""

    queryset = CableImportSource.objects.select_related("cable", "profile")
    serializer_class = CableImportSourceSerializer
    permission_classes = [permissions.IsAuthenticated, DjangoModelPermissionsWithView]

    def get_queryset(self):
        """Apply the profile scope and an optional Cable ID filter."""
        qs = super().get_queryset()
        cable_id = self.request.query_params.get("cable_id")
        if cable_id is not None:
            try:
                cable_id = int(cable_id)
            except (TypeError, ValueError) as exc:
                raise ValidationError({"cable_id": "Enter a whole number."}) from exc
            qs = qs.filter(cable_id=cable_id)
        return qs


class ResolutionProposalViewSet(_ProfileScopedQuerySetMixin, viewsets.ReadOnlyModelViewSet):
    """Read-only viewset for Resolution Proposal history."""

    queryset = ResolutionProposal.objects.select_related("profile")
    serializer_class = ResolutionProposalSerializer
    permission_classes = [permissions.IsAuthenticated, DjangoModelPermissionsWithView]


class ResolutionProposalHistoryViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """Read one field's complete paginated history with workspace permissions."""

    queryset = ResolutionProposal.objects.none()
    serializer_class = ResolutionProposalHistorySerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        """Return one profile field's history when the actor can view that profile."""
        profile_values = self.request.query_params.getlist("profile_id")
        field_values = self.request.query_params.getlist("field_key")
        if len(profile_values) != 1 or not profile_values[0]:
            raise ValidationError({"profile_id": "Provide one whole-number profile ID."})
        if len(field_values) != 1 or not field_values[0]:
            raise ValidationError({"field_key": "Provide one canonical termination field key."})
        try:
            profile_id = int(profile_values[0])
        except (TypeError, ValueError) as exc:
            raise ValidationError({"profile_id": "Provide one whole-number profile ID."}) from exc
        try:
            parse_termination_field_key(field_values[0])
        except ValueError as exc:
            raise ValidationError({"field_key": "Provide one canonical termination field key."}) from exc
        if not ImportProfile.objects.restrict(self.request.user, "view").filter(pk=profile_id).exists():
            raise Http404
        return (
            ResolutionProposal.objects.filter(
                profile_id=profile_id,
                task_type=SELECT_TERMINATION_TASK,
                field_key=field_values[0],
            )
            .select_related("profile")
            .order_by("-created", "-pk")
        )


class InferenceBackendViewSet(NetBoxModelViewSet):
    """CRUD viewset whose configuration fields derive from the Inference Backend form."""

    queryset = InferenceBackend.objects.prefetch_related("tags")
    serializer_class = InferenceBackendSerializer
