# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
import django_filters
from netbox.filtersets import NetBoxModelFilterSet
from .models import ImportProfile


class ImportProfileFilterSet(NetBoxModelFilterSet):
    """FilterSet for ImportProfile, supporting name substring search."""

    q = django_filters.CharFilter(method="search", label="Search")

    class Meta:
        model = ImportProfile
        fields = ["name", "sheet_name", "update_existing", "create_missing_device_types"]

    def search(self, queryset, name, value):
        """Filter profiles by name substring."""
        return queryset.filter(name__icontains=value)
