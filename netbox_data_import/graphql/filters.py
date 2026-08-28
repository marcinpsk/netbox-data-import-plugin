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
    # The lookup class moved between strawberry-django releases, so it resolves at import time.
    StrFilterLookup = getattr(strawberry_django, "StrFilterLookup", None)
    if StrFilterLookup is None:
        StrFilterLookup = strawberry_django.FilterLookup

    @strawberry_django.filter_type(ImportProfile, lookups=True)
    class ImportProfileFilter(NetBoxModelFilter):  # type: ignore[no-redef]
        """Filter profiles by their imported configuration fields."""

        name: StrFilterLookup[str] | None = strawberry_django.filter_field()  # type: ignore[valid-type]
        description: StrFilterLookup[str] | None = strawberry_django.filter_field()  # type: ignore[valid-type]
        source_adapter: StrFilterLookup[str] | None = strawberry_django.filter_field()  # type: ignore[valid-type]


__all__ = ("ImportProfileFilter",)
