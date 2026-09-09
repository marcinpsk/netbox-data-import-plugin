# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
from typing import Any

from netbox.plugins import PluginConfig

__version__ = "2.0.0"


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

    default_settings: dict[str, Any] = {
        # A deployment that names no allowlist reaches no origin, rather than every origin.
        "inference_backend_origin_allowlist": [],
    }

    @classmethod
    def validate(cls, user_config, netbox_version):
        """Reject a malformed Inference Backend configuration before the application serves a request."""
        super().validate(user_config, netbox_version)

        from django.core.exceptions import ImproperlyConfigured

        from .inference_settings import InvalidInferenceConfiguration, validate_plugin_settings

        try:
            validate_plugin_settings(user_config)
        except InvalidInferenceConfiguration as exc:
            raise ImproperlyConfigured(f"Plugin {cls.__module__} has an invalid configuration: {exc}") from exc

    def ready(self):
        """Import the jobs module, which NetBox does not load, so its @system_job registration runs."""
        super().ready()

        from . import jobs


config = NetBoxDataImportConfig
