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
        if not import_data:
            return ""
        extra = import_data.get("extra") or {}
        return self.render(
            "netbox_data_import/device_import_data.html",
            extra_context={
                "import_data": import_data,
                "extra_columns": extra,
            },
        )


template_extensions = [DeviceImportDataExtension]
