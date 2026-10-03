# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
from typing import Any

from netbox.plugins import PluginConfig

from .preview_limits import MAX_PLAN_BYTES

__version__ = "2.10.0"


class NetBoxDataImportConfig(PluginConfig):
    """NetBox plugin configuration for the Data Import plugin."""

    name = "netbox_data_import"
    verbose_name = "NetBox Data Import"
    description = "NetBox plugin for importing data from external DCIM systems"
    version = __version__
    base_url = "data-import"
    author = "Marcin Zieba"
    author_email = "marcinpsk@gmail.com"
    min_version = "4.6.9"
    graphql_schema = "graphql.schema.schema"
    middleware = ["netbox_data_import.branching.BranchRefusalMiddleware"]

    default_settings: dict[str, Any] = {
        # A deployment that names no allowlist reaches no origin, rather than every origin.
        "inference_backend_origin_allowlist": [],
        "preview_max_plan_bytes": MAX_PLAN_BYTES,
    }

    @classmethod
    def validate(cls, user_config, netbox_version):
        """Reject a malformed plugin configuration before the application serves a request."""
        super().validate(user_config, netbox_version)

        from django.core.exceptions import ImproperlyConfigured

        from .inference_settings import InvalidInferenceConfiguration, validate_plugin_settings
        from .preview_limits import InvalidPreviewConfiguration, preview_plan_byte_limit

        try:
            validate_plugin_settings(user_config)
            preview_plan_byte_limit(user_config["preview_max_plan_bytes"])
        except (InvalidInferenceConfiguration, InvalidPreviewConfiguration) as exc:
            raise ImproperlyConfigured(f"Plugin {cls.__module__} has an invalid configuration: {exc}") from exc

    def ready(self):
        """Import the modules NetBox does not load, and register the Cable display vocabulary and netbox-branching."""
        super().ready()

        from . import branching, cable_disclosure, jobs, termination_proposal

        cable_disclosure.register()
        branching.register()


config = NetBoxDataImportConfig
