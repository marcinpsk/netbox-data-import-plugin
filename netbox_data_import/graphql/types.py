# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>

"""GraphQL object types for import profile configuration."""

import strawberry_django
from netbox.graphql.types import BaseObjectType, NetBoxObjectType

from netbox_data_import.models import CableClassMapping, ImportProfile

from .filters import ImportProfileFilter

_type_kwargs = {"fields": "__all__", "pagination": True}
if ImportProfileFilter is not None:  # pragma: no cover
    _type_kwargs["filters"] = ImportProfileFilter


@strawberry_django.type(ImportProfile, **_type_kwargs)
class ImportProfileType(NetBoxObjectType):
    """One saved source-file import configuration."""


@strawberry_django.type(
    CableClassMapping,
    fields=[
        "id",
        "profile",
        "cable_class",
        "cable_type_resolved",
        "cable_type",
        "cable_profile_resolved",
        "cable_profile",
    ],
    pagination=True,
)
class CableClassMappingType(BaseObjectType):
    """One trace CableClass policy decision."""

    profile: ImportProfileType


__all__ = ("CableClassMappingType", "ImportProfileType")
