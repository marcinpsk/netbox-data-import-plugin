# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Override plugin settings in a test without dropping the entries of the other plugins."""

from django.conf import settings
from django.test import override_settings


def override_plugins_config(**entries) -> override_settings:
    """Replace the entry of each named plugin, and keep the entries of all other plugins."""
    return override_settings(PLUGINS_CONFIG={**settings.PLUGINS_CONFIG, **entries})
