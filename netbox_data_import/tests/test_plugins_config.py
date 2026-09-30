# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""A PLUGINS_CONFIG override keeps the entries of the other plugins, and the session guard refuses one that can drop them."""

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from netbox_data_import.tests.plugins_config import DroppedPluginSettings, MergedPluginsConfig, override_plugins_config


class PluginsConfigOverrideTest(SimpleTestCase):
    """The helper replaces only the named entries, and the guard refuses every other override."""

    def test_the_helper_replaces_the_named_entry_and_keeps_the_others(self):
        with override_plugins_config(other_plugin={"key": 1}), override_plugins_config(netbox_data_import={}):
            self.assertEqual(settings.PLUGINS_CONFIG["other_plugin"], {"key": 1})
            self.assertEqual(settings.PLUGINS_CONFIG["netbox_data_import"], {})

    def test_a_bare_dict_is_refused_and_the_settings_are_restored(self):
        before = settings.PLUGINS_CONFIG

        with self.assertRaisesMessage(DroppedPluginSettings, "override_plugins_config()"):
            with override_plugins_config(other_plugin={"key": 1}):
                with override_settings(PLUGINS_CONFIG={**settings.PLUGINS_CONFIG}):
                    self.fail("The guard let a bare PLUGINS_CONFIG value through.")

        self.assertIs(settings.PLUGINS_CONFIG, before)

    def test_a_bare_dict_on_a_test_class_is_refused(self):
        @override_settings(PLUGINS_CONFIG={"netbox_data_import": {}})
        class BareOverride(SimpleTestCase):
            def test_nothing(self):
                """Never runs: the class setup refuses the override."""

        self.addCleanup(BareOverride.doClassCleanups)
        with self.assertRaisesMessage(DroppedPluginSettings, "override_plugins_config()"):
            BareOverride.setUpClass()

    def test_an_override_that_drops_an_installed_plugin_names_it(self):
        with self.assertRaisesRegex(DroppedPluginSettings, r"drops (\w+, )*netbox_data_import\b"):
            with override_settings(PLUGINS_CONFIG=MergedPluginsConfig()):
                self.fail("The guard let an override without the plugin's own entry through.")
