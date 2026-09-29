# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The changelog snapshot guard fails plugin updates without a current snapshot, and nothing else."""

import types

from django.test import TestCase

from netbox_data_import.tests import snapshot_guard
from netbox_data_import.tests.snapshot_guard import MissingChangelogSnapshot


def _save(instance):
    instance.save()


def _create_cable(a_end, b_end):
    from dcim.models import Cable

    Cable(a_terminations=[a_end], b_terminations=[b_end]).save()


def _from_module(function, module_name):
    """Return *function* running with the given module name, so the guard sees a frame of that module."""
    return types.FunctionType(function.__code__, {"__name__": module_name, "__builtins__": __builtins__})


_plugin_save = _from_module(_save, "netbox_data_import._guard_probe")
_netbox_save = _from_module(_save, "dcim._guard_probe")
_plugin_create_cable = _from_module(_create_cable, "netbox_data_import._guard_probe")


class SnapshotGuardTest(TestCase):
    """Only plugin code must snapshot, and its snapshot must match the stored row."""

    def setUp(self):
        from dcim.models import Site

        self.site = Site.objects.create(name="Guard Site", slug="guard-site")

    def tearDown(self):
        snapshot_guard.take_violations()
        super().tearDown()

    def _fetched(self):
        from dcim.models import Site

        return Site.objects.get(pk=self.site.pk)

    def test_a_plugin_update_without_a_snapshot_fails(self):
        site = self._fetched()
        site.description = "changed"

        with self.assertRaises(MissingChangelogSnapshot) as raised:
            _plugin_save(site)

        self.assertIn(f"dcim.Site pk={site.pk} was saved with no prechange snapshot", str(raised.exception))
        self.assertIn(f"{__file__}:", str(raised.exception))
        self.assertEqual(snapshot_guard.take_violations(), [str(raised.exception)])

    def test_a_plugin_update_with_a_stale_snapshot_fails(self):
        site = self._fetched()
        site.snapshot()
        moved = self._fetched()
        moved.description = "written by someone else"
        moved.save()
        site.description = "changed"

        with self.assertRaisesMessage(
            MissingChangelogSnapshot, "was saved with a stale prechange snapshot (differs in description)"
        ):
            _plugin_save(site)

    def test_a_reused_snapshot_is_stale_at_the_second_save(self):
        site = self._fetched()
        site.snapshot()
        site.description = "first"
        _plugin_save(site)
        site.description = "second"

        with self.assertRaisesMessage(
            MissingChangelogSnapshot, "was saved with a stale prechange snapshot (differs in description)"
        ):
            _plugin_save(site)

    def test_a_plugin_update_with_a_current_snapshot_passes(self):
        site = self._fetched()
        site.snapshot()
        site.description = "changed"

        _plugin_save(site)

        self.assertEqual(snapshot_guard.take_violations(), [])

    def test_test_code_and_netbox_code_may_save_without_a_snapshot(self):
        site = self._fetched()
        site.description = "test fixture"
        _save(site)
        site.description = "netbox internal"
        _netbox_save(site)

        self.assertEqual(snapshot_guard.take_violations(), [])

    def test_a_plugin_create_needs_no_snapshot(self):
        from dcim.models import Site

        _plugin_save(Site(name="Created Site", slug="created-site"))

        self.assertEqual(snapshot_guard.take_violations(), [])

    def test_a_second_plugin_save_after_a_create_needs_a_snapshot(self):
        from dcim.models import Site

        site = Site(name="Created Site", slug="created-site")
        _plugin_save(site)
        site.description = "changed"

        with self.assertRaisesMessage(MissingChangelogSnapshot, "was saved with no prechange snapshot"):
            _plugin_save(site)

    def test_netbox_saves_inside_a_plugin_call_are_not_the_plugin_update(self):
        """NetBox saves both interfaces when the plugin creates a Cable, and those saves are NetBox's."""
        from dcim.models import Device, Interface
        from netbox_data_import.tests.helpers import make_dcim_objects

        site, _manufacturer, device_type, role = make_dcim_objects("probe-")
        device = Device.objects.create(name="guard-device", site=site, device_type=device_type, role=role)
        a_end = Interface.objects.create(device=device, name="eth0", type="1000base-t")
        b_end = Interface.objects.create(device=device, name="eth1", type="1000base-t")

        _plugin_create_cable(a_end, b_end)

        self.assertEqual(snapshot_guard.take_violations(), [])
