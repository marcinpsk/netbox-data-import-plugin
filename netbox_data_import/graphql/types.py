# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>

"""GraphQL object type for import profiles."""

from typing import TYPE_CHECKING, Annotated

import strawberry
import strawberry_django
from netbox.graphql.types import NetBoxObjectType

from netbox_data_import.models import ImportProfile

from .filters import ImportProfileFilter

if TYPE_CHECKING:
    from tenancy.graphql.types import ContactRoleType

_type_kwargs = {"fields": "__all__", "pagination": True}
if ImportProfileFilter is not None:  # pragma: no cover
    _type_kwargs["filters"] = ImportProfileFilter


@strawberry_django.type(ImportProfile, **_type_kwargs)
class ImportProfileType(NetBoxObjectType):
    """One saved source-file import configuration."""

    primary_contact_role: Annotated["ContactRoleType", strawberry.lazy("tenancy.graphql.types")] | None


__all__ = ("ImportProfileType",)
