# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
from netbox.plugins.navigation import PluginMenu

menu = PluginMenu(
    label="Data Import",
    groups=(),
    icon_class="mdi mdi-database-import",
)
