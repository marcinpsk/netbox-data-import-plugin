# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""URL router for the data-import plugin API."""

from netbox.api.routers import NetBoxRouter

from .views import (
    ImportProfileViewSet,
    ColumnMappingViewSet,
    ClassRoleMappingViewSet,
    DeviceTypeMappingViewSet,
    IgnoredDeviceViewSet,
    ColumnTransformRuleViewSet,
    SourceResolutionViewSet,
    ImportJobViewSet,
)

router = NetBoxRouter()
router.register("profiles", ImportProfileViewSet)
router.register("column-mappings", ColumnMappingViewSet)
router.register("class-role-mappings", ClassRoleMappingViewSet)
router.register("device-type-mappings", DeviceTypeMappingViewSet)
router.register("ignored-devices", IgnoredDeviceViewSet)
router.register("column-transforms", ColumnTransformRuleViewSet)
router.register("source-resolutions", SourceResolutionViewSet)
router.register("jobs", ImportJobViewSet)

app_name = "netbox_data_import"
urlpatterns = router.urls
