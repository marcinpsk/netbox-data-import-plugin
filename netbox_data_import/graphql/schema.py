# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>

"""GraphQL queries registered with NetBox."""

import strawberry
import strawberry_django
from strawberry.permission import BasePermission

from netbox_data_import import branching

from .types import CableClassMappingType, ImportProfileType


class MainOnly(BasePermission):
    """Refuse a plugin field on the same terms as the plugin's REST and UI views."""

    def has_permission(self, source, info, **kwargs) -> bool:
        """Raise BranchActive, whose message becomes the GraphQL error, for a refused request."""
        message = branching.request_refusal(info.context.request)
        if message is not None:
            raise branching.BranchActive(message)
        return True


@strawberry.type(name="Query")
class ImportProfilesQuery:
    """Expose import profile detail and list queries."""

    import_profile: ImportProfileType = strawberry_django.field(permission_classes=[MainOnly])
    import_profile_list: list[ImportProfileType] = strawberry_django.field(permission_classes=[MainOnly])


@strawberry.type(name="Query")
class CableClassMappingsQuery:
    """Expose CableClass mapping detail and list queries."""

    cable_class_mapping: CableClassMappingType = strawberry_django.field(permission_classes=[MainOnly])
    cable_class_mapping_list: list[CableClassMappingType] = strawberry_django.field(permission_classes=[MainOnly])


schema = [ImportProfilesQuery, CableClassMappingsQuery]

__all__ = ("CableClassMappingsQuery", "ImportProfilesQuery", "schema")
