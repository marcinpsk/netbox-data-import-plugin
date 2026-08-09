# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>

"""GraphQL queries registered with NetBox."""

import strawberry
import strawberry_django

from .types import ImportProfileType


@strawberry.type(name="Query")
class ImportProfilesQuery:
    """Expose import profile detail and list queries."""

    import_profile: ImportProfileType = strawberry_django.field()
    import_profile_list: list[ImportProfileType] = strawberry_django.field()


schema = [ImportProfilesQuery]

__all__ = ("ImportProfilesQuery", "schema")
