# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
from netbox.api.routers import NetBoxRouter

router = NetBoxRouter()
app_name = "netbox_data_import"
urlpatterns = router.urls
