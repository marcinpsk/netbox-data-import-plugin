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
    from strawberry_django import BaseFilterLookup

    StrFilterLookup = getattr(strawberry_django, "StrFilterLookup", None)
    if StrFilterLookup is None:
        StrFilterLookup = strawberry_django.FilterLookup

    @strawberry_django.filter_type(ImportProfile, lookups=True)
    class ImportProfileFilter(NetBoxModelFilter):
        """Filter profiles by their imported configuration fields."""

        name: StrFilterLookup[str] | None = strawberry_django.filter_field()
        description: StrFilterLookup[str] | None = strawberry_django.filter_field()
        sheet_name: StrFilterLookup[str] | None = strawberry_django.filter_field()
        source_id_column: StrFilterLookup[str] | None = strawberry_django.filter_field()
        custom_field_name: StrFilterLookup[str] | None = strawberry_django.filter_field()
        update_existing: BaseFilterLookup[bool] | None = strawberry_django.filter_field()
        create_missing_device_types: BaseFilterLookup[bool] | None = strawberry_django.filter_field()
        preview_view_mode: StrFilterLookup[str] | None = strawberry_django.filter_field()
        capture_extra_data: BaseFilterLookup[bool] | None = strawberry_django.filter_field()


__all__ = ("ImportProfileFilter",)
