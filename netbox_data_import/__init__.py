# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
import logging

from netbox.plugins import PluginConfig

logger = logging.getLogger(__name__)

__version__ = "1.0.3"

DATA_IMPORT_CF_NAME = "data_import_source"


def _ensure_import_custom_fields(sender, **kwargs):
    """Auto-create the data_import_source JSON custom field on Device after migrations."""
    try:
        from extras.models import CustomField
        from django.contrib.contenttypes.models import ContentType
        from dcim.models import Device

        device_ct = ContentType.objects.get_for_model(Device)
        cf, created = CustomField.objects.get_or_create(
            name=DATA_IMPORT_CF_NAME,
            defaults={
                "type": "json",
                "label": "Data Import Source",
                "description": ("Metadata set by the Data Import plugin: source_id, profile_id, profile_name."),
                "required": False,
                "weight": 9999,
            },
        )
        if created:
            cf.object_types.set([device_ct])
    except Exception:  # pragma: no cover
        logger.warning("Failed to auto-create data_import_source custom field", exc_info=True)


class NetBoxDataImportConfig(PluginConfig):
    """NetBox plugin configuration for the Data Import plugin."""

    name = "netbox_data_import"
    verbose_name = "NetBox Data Import"
    description = "NetBox plugin for importing data from external DCIM systems"
    version = __version__
    base_url = "data-import"
    author = "Marcin Zieba"
    author_email = "marcinpsk@gmail.com"
    min_version = "4.2.0"

    def ready(self):
        """Register post_migrate signal to auto-create the data_import_source custom field."""
        super().ready()
        from django.db.models.signals import post_migrate

        post_migrate.connect(_ensure_import_custom_fields, sender=self)


config = NetBoxDataImportConfig
