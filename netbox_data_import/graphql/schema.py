# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>

"""GraphQL queries registered with NetBox."""

import strawberry
import strawberry_django

from .types import CableClassMappingType, ImportProfileType


@strawberry.type(name="Query")
class ImportProfilesQuery:
    """Expose import profile detail and list queries."""

    import_profile: ImportProfileType = strawberry_django.field()
    import_profile_list: list[ImportProfileType] = strawberry_django.field()


@strawberry.type(name="Query")
class CableClassMappingsQuery:
    """Expose CableClass mapping detail and list queries."""

    cable_class_mapping: CableClassMappingType = strawberry_django.field()
    cable_class_mapping_list: list[CableClassMappingType] = strawberry_django.field()


schema = [ImportProfilesQuery, CableClassMappingsQuery]

__all__ = ("CableClassMappingsQuery", "ImportProfilesQuery", "schema")
