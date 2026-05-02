# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
from netbox.plugins import PluginTemplateExtension


class DeviceImportDataExtension(PluginTemplateExtension):
    """Adds an import data card to the Device detail page right panel."""

    models = ["dcim.device"]

    def right_page(self):
        """Render import data card for the Device detail page right column."""
        obj = self.context.get("object")
        if not obj:
            return ""
        import_data = obj.cf.get("data_import_source") if obj.cf else None
        if not isinstance(import_data, dict) or not import_data:
            return ""
        extra = import_data.get("extra") or {}
        ip_data = import_data.get("_ip") or {}

        # Check which IP fields are already natively assigned in NetBox
        ip_status = {}
        ip_field_labels = {
            "primary_ip4": "Primary IPv4",
            "primary_ip6": "Primary IPv6",
            "oob_ip": "Out-of-band IP",
        }
        for field, value in ip_data.items():
            native = getattr(obj, field, None)
            native_str = str(native.address) if native and hasattr(native, "address") else ""
            ip_status[field] = {
                "label": ip_field_labels.get(field, field),
                "value": value,
                "in_netbox": bool(native),
                "native_value": native_str,
            }

        return self.render(
            "netbox_data_import/device_import_data.html",
            extra_context={
                "import_data": import_data,
                "extra_columns": extra,
                "ip_status": ip_status,
                "device": obj,
            },
        )


template_extensions = [DeviceImportDataExtension]
