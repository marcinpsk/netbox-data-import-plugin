# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
from netbox.plugins import PluginConfig

__version__ = "0.1.0"


class NetBoxDataImportConfig(PluginConfig):
    name = "netbox_data_import"
    verbose_name = "NetBox Data Import"
    description = "NetBox plugin for importing data from external DCIM systems"
    version = __version__
    base_url = "data-import"
    author = "Marcin Zieba"
    author_email = "marcinpsk@gmail.com"
    min_version = "4.2.0"

    def ready(self):
        from . import signals  # noqa: F401
        super().ready()


config = NetBoxDataImportConfig
