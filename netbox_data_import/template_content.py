# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
from netbox.plugins import PluginTemplateExtension

from .models import stored_import_source

IP_FIELD_LABELS = {
    "primary_ip4": "Primary IPv4",
    "primary_ip6": "Primary IPv6",
    "oob_ip": "Out-of-band IP",
}


class DeviceImportDataExtension(PluginTemplateExtension):
    """Adds an import data card to the Device detail page.

    NetBox appends plugin content to the end of a column, so the closest supported position to
    the Tags panel is the left column, below the rack elevation drawing that ends the right one.
    """

    models = ["dcim.device"]

    def left_page(self):
        """Render import data card for the Device detail page left column."""
        obj = self.context.get("object")
        import_source = stored_import_source(obj)
        if import_source is None:
            return ""

        # Show which of the stored IPs NetBox now holds natively.
        ip_status = {}
        for field, value in import_source.unassigned_ips.items():
            native = getattr(obj, field, None)
            ip_status[field] = {
                "label": IP_FIELD_LABELS.get(field, field),
                "value": value,
                "in_netbox": bool(native),
                "native_value": str(native.address) if native is not None and hasattr(native, "address") else "",
            }

        return self.render(
            "netbox_data_import/device_import_data.html",
            extra_context={
                "import_source": import_source,
                "extra_columns": import_source.extra_columns,
                "ip_status": ip_status,
                "device": obj,
            },
        )


template_extensions = [DeviceImportDataExtension]
