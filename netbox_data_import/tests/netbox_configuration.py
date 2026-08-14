# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""NetBox configuration isolated from unrelated devcontainer plugins."""

from netbox import configuration as _configuration


for _name in dir(_configuration):
    if _name.isupper():
        globals()[_name] = getattr(_configuration, _name)

PLUGINS = ["netbox_data_import"]
PLUGINS_CONFIG = {
    "netbox_data_import": getattr(_configuration, "PLUGINS_CONFIG", {}).get("netbox_data_import", {}),
}
