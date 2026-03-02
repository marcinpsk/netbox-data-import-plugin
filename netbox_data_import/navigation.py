# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
from netbox.plugins.navigation import PluginMenu, PluginMenuButton, PluginMenuItem

menu = PluginMenu(
    label="Data Import",
    groups=(
        (
            "Import",
            (
                PluginMenuItem(
                    link="plugins:netbox_data_import:import_setup",
                    link_text="Run Import",
                    buttons=(
                        PluginMenuButton(
                            link="plugins:netbox_data_import:import_setup",
                            title="Run Import",
                            icon_class="mdi mdi-database-import",
                        ),
                    ),
                ),
                PluginMenuItem(
                    link="plugins:netbox_data_import:importjob_list",
                    link_text="Import History",
                ),
            ),
        ),
        (
            "Configuration",
            (
                PluginMenuItem(
                    link="plugins:netbox_data_import:importprofile_list",
                    link_text="Import Profiles",
                    buttons=(
                        PluginMenuButton(
                            link="plugins:netbox_data_import:importprofile_add",
                            title="Add",
                            icon_class="mdi mdi-plus-thick",
                        ),
                    ),
                ),
                PluginMenuItem(
                    link="plugins:netbox_data_import:device_type_analysis",
                    link_text="Device Type Analysis",
                ),
            ),
        ),
    ),
    icon_class="mdi mdi-database-import",
)
