# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Override plugin settings in a test without dropping the entries of the other plugins."""

from django.conf import settings
from django.test import override_settings
from django.test.signals import setting_changed

_DISPATCH_UID = "netbox_data_import.tests.plugins_config"
_USE_THE_HELPER = "Build it with override_plugins_config()."


class MergedPluginsConfig(dict):
    """A ``PLUGINS_CONFIG`` value that ``override_plugins_config()`` built from the current one."""


class DroppedPluginSettings(AssertionError):
    """A test replaced ``PLUGINS_CONFIG`` with a value that can drop the entry of an installed plugin."""


def override_plugins_config(**entries) -> override_settings:
    """Replace the entry of each named plugin, and keep the entries of all other plugins."""
    return override_settings(PLUGINS_CONFIG=MergedPluginsConfig({**settings.PLUGINS_CONFIG, **entries}))


def _refuse_a_dropped_entry(*, setting, value, enter, **kwargs) -> None:
    if setting != "PLUGINS_CONFIG" or not enter:
        return
    # NetBox gives every plugin in PLUGINS an entry at startup, so each one must survive an override.
    missing = sorted(set(settings.PLUGINS) - set(value or ()))
    if missing:
        raise DroppedPluginSettings(f"The PLUGINS_CONFIG override drops {', '.join(missing)}. {_USE_THE_HELPER}")
    # A bare dict drops nothing on a stack with one plugin, but it drops netbox_branching on the branching stack.
    if not isinstance(value, MergedPluginsConfig):
        raise DroppedPluginSettings(f"The PLUGINS_CONFIG override is a bare value. {_USE_THE_HELPER}")


def connect() -> None:
    """Refuse every ``PLUGINS_CONFIG`` override that is not built from the current settings."""
    setting_changed.connect(_refuse_a_dropped_entry, dispatch_uid=_DISPATCH_UID)


def disconnect() -> None:
    """Stop the check that ``connect()`` started."""
    setting_changed.disconnect(dispatch_uid=_DISPATCH_UID)
