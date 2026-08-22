# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
from netbox.plugins import PluginConfig

__version__ = "1.5.2"


class NetBoxDataImportConfig(PluginConfig):
    """NetBox plugin configuration for the Data Import plugin."""

    name = "netbox_data_import"
    verbose_name = "NetBox Data Import"
    description = "NetBox plugin for importing data from external DCIM systems"
    version = __version__
    base_url = "data-import"
    author = "Marcin Zieba"
    author_email = "marcinpsk@gmail.com"
    min_version = "4.6.0"
    graphql_schema = "graphql.schema.schema"

    def ready(self):
        """Import the jobs module so its @system_job registration runs at startup.

        NetBox loads a fixed set of plugin resources and `jobs` is not one of them, so without this
        the retention schedule would never be registered.
        """
        super().ready()

        from . import jobs  # noqa: F401


config = NetBoxDataImportConfig
