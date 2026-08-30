# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>

"""GraphQL filter input for import profiles."""

import strawberry_django

from netbox_data_import.models import ImportProfile

_FILTER_BASE_MODULE = "netbox.graphql.filters"


try:
    from netbox.graphql.filters import NetBoxModelFilter
except ModuleNotFoundError as exc:  # pragma: no cover
    missing = exc.name or ""
    if missing != _FILTER_BASE_MODULE and not _FILTER_BASE_MODULE.startswith(f"{missing}."):
        raise
    ImportProfileFilter = None
else:  # pragma: no cover

    @strawberry_django.filter_type(ImportProfile, lookups=True)
    class ImportProfileFilter(NetBoxModelFilter):  # type: ignore[no-redef]
        """Filter profiles by their imported configuration fields."""

        name: strawberry_django.StrFilterLookup | None = strawberry_django.filter_field()
        description: strawberry_django.StrFilterLookup | None = strawberry_django.filter_field()
        source_adapter: strawberry_django.StrFilterLookup | None = strawberry_django.filter_field()


__all__ = ("ImportProfileFilter",)
