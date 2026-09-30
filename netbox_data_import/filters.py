# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
from django.db.models import Q
from netbox.filtersets import NetBoxModelFilterSet
from .models import ImportProfile, InferenceBackend


class ImportProfileFilterSet(NetBoxModelFilterSet):
    """FilterSet for ImportProfile, supporting name substring search."""

    class Meta:
        model = ImportProfile
        fields = ["name", "source_adapter"]

    def search(self, queryset, name, value):
        """Filter profiles by name substring."""
        return queryset.filter(name__icontains=value)


class InferenceBackendFilterSet(NetBoxModelFilterSet):
    """FilterSet for InferenceBackend, supporting key and display name search."""

    class Meta:
        model = InferenceBackend
        fields = ["backend_key", "display_name", "adapter_type", "response_mode", "enabled"]

    def search(self, queryset, name, value):
        """Filter backends by key or display name substring."""
        return queryset.filter(Q(backend_key__icontains=value) | Q(display_name__icontains=value))
