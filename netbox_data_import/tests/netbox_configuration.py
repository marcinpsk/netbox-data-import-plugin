# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""NetBox configuration isolated from unrelated devcontainer plugins."""

from netbox import configuration as _configuration


for _name in dir(_configuration):
    if _name.isupper():
        globals()[_name] = getattr(_configuration, _name)

PLUGINS = ["netbox_data_import"]
# netbox-branching is the one other plugin the suite runs beside, when the base configuration lists it.
if "netbox_branching" in getattr(_configuration, "PLUGINS", []):
    PLUGINS.append("netbox_branching")
PLUGINS_CONFIG = {
    "netbox_data_import": getattr(_configuration, "PLUGINS_CONFIG", {}).get("netbox_data_import", {}),
}
