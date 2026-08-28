# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The Device Target Module turns device source rows into Synchronization Units.

Every branch the current device pass reports as a row action maps onto exactly one disposition from
section 4.2, so these tests are the mapping written down. This layer covers identity, the duplicate
checks, matching against NetBox, and the disposition each of those earns. Field review, contacts
and IP assignment arrive with the next layer.
"""

from django.test import TestCase

from netbox_data_import.adapters import SourceBatch
from netbox_data_import.catalog import OutputKind
from netbox_data_import.models import ClassRoleMapping, DeviceExistingMatch, IgnoredDevice, ImportProfile
from netbox_data_import.netbox_reader import NetBoxReader
from netbox_data_import.plan import Disposition
from netbox_data_import.target_modules import DeviceModule


class DeviceModulePlanTestBase(TestCase):
    """The site, the device type and the role every device row in these tests resolves to."""

    def setUp(self):
        """Create the target state a well-formed row plans against."""
        from dcim.models import DeviceRole, DeviceType, Manufacturer, Rack, Site

        self.site = Site.objects.create(name="Device Module Site", slug="device-module-site")
        self.rack = Rack.objects.create(name="dm-rack", site=self.site, u_height=42)
        self.manufacturer = Manufacturer.objects.create(name="Dell", slug="dell")
        self.device_type = DeviceType.objects.create(
            manufacturer=self.manufacturer, model="R660", slug="dell-r660", u_height=1
        )
        self.role = DeviceRole.objects.create(name="Server", slug="server")
        self.profile = ImportProfile.objects.create(
            name="Device Module Profile", adapter_config={"sheet_name": "Data", "update_existing": True}
        )
        ClassRoleMapping.objects.create(profile=self.profile, source_class="Cabinet", creates_rack=True)
        ClassRoleMapping.objects.create(
            profile=self.profile, source_class="Server", creates_rack=False, role_slug="server"
        )
        self.reader = NetBoxReader.unrestricted().for_target(site=self.site)

    def _batch(self, *rows):
        """Wrap rows the way the flat adapter hands them over."""
        return SourceBatch(output_kinds=frozenset({OutputKind.DEVICE_SOURCE_ROW}), rows=tuple(rows))

    def _row(self, number, source_id, device_name, device_class="Server", **extra):
        """Return one well-formed device row, before the test spoils whatever it is about."""
        row = {
            "_row_number": number,
            "source_id": source_id,
            "device_class": device_class,
            "device_name": device_name,
            "rack_name": "dm-rack",
            "make": "Dell",
            "model": "R660",
            "serial": "",
            "asset_tag": "",
        }
        row.update(extra)
        return row

    def _plan(self, *rows):
        return DeviceModule().plan(self._batch(*rows), self.profile, None, self.reader)

    def _device(self, name, **fields):
        """Create a NetBox device that a row can match."""
        from dcim.models import Device

        values = {
            "name": name,
            "site": self.site,
            "device_type": self.device_type,
            "role": self.role,
            "status": "active",
        }
        values.update(fields)
        return Device.objects.create(**values)


class DeviceModuleSelectionTest(DeviceModulePlanTestBase):
    """Class policy decides which rows belong to this module."""

    def test_a_rack_row_produces_no_device_unit(self):
        """A class that creates a rack belongs to the Rack module, not this one."""
        self.assertEqual(self._plan(self._row(2, "R-1", "dm-rack", device_class="Cabinet")), [])

    def test_an_unmapped_class_produces_no_unit(self):
        """A class the profile says nothing about is not this module's row to plan."""
        self.assertEqual(self._plan(self._row(2, "D-1", "srv-01", device_class="Unmapped")), [])

    def test_a_class_the_profile_ignores_produces_no_unit(self):
        """`ignore` is a policy answer for the whole class, so no unit is owed."""
        ClassRoleMapping.objects.create(profile=self.profile, source_class="Spare", creates_rack=False, ignore=True)
        self.assertEqual(self._plan(self._row(2, "D-1", "srv-01", device_class="Spare")), [])


class DeviceModuleIdentityTest(DeviceModulePlanTestBase):
    """A unit needs an identity that survives replanning, and a name to write."""

    def test_a_row_with_no_device_name_is_invalid(self):
        """A device without a name is not something this can create or match."""
        units = self._plan(self._row(2, "D-1", ""))

        self.assertEqual(units[0].disposition, Disposition.INVALID)
        self.assertEqual(units[0].diagnostics[0].code, "device.missing_name")

    def test_identity_is_stable_across_replanning(self):
        """The row number moves when a sheet gains a row, so the identity cannot read it."""
        first = self._plan(self._row(2, "D-1", "srv-01"))[0].identity
        second = self._plan(self._row(9, "D-1", "srv-01"))[0].identity

        self.assertEqual(first, second)

    def test_a_row_with_no_source_id_is_identified_by_its_name(self):
        """A profile can leave the source ID column unmapped, and the unit still needs an identity."""
        units = self._plan(self._row(2, "", "srv-01"))

        self.assertEqual(units[0].identity, "device:name:srv-01")

    def test_an_ignored_source_id_is_excluded(self):
        """`excluded` is the disposition reserved for operator-configured policy."""
        IgnoredDevice.objects.create(profile=self.profile, source_id="D-1")

        units = self._plan(self._row(2, "D-1", "srv-01"))

        self.assertEqual(units[0].disposition, Disposition.EXCLUDED)
        self.assertEqual(units[0].diagnostics[0].code, "device.ignored")


class DeviceModuleDuplicateTest(DeviceModulePlanTestBase):
    """Two rows claiming one identity is a source defect, so both are refused."""

    def test_a_duplicate_source_id_in_one_file_is_invalid(self):
        units = self._plan(self._row(2, "D-1", "srv-01"), self._row(3, "D-1", "srv-02"))

        self.assertEqual([unit.disposition for unit in units], [Disposition.INVALID] * 2)
        self.assertEqual(units[0].diagnostics[0].code, "device.duplicate_source_id")

    def test_a_duplicate_serial_in_one_file_is_invalid(self):
        units = self._plan(
            self._row(2, "D-1", "srv-01", serial="SN-1"),
            self._row(3, "D-2", "srv-02", serial="SN-1"),
        )

        self.assertEqual([unit.disposition for unit in units], [Disposition.INVALID] * 2)
        self.assertEqual(units[0].diagnostics[0].code, "device.duplicate_serial")

    def test_a_duplicate_asset_tag_in_one_file_is_invalid(self):
        units = self._plan(
            self._row(2, "D-1", "srv-01", asset_tag="AT-1"),
            self._row(3, "D-2", "srv-02", asset_tag="at-1"),
        )

        self.assertEqual([unit.disposition for unit in units], [Disposition.INVALID] * 2)
        self.assertEqual(units[0].diagnostics[0].code, "device.duplicate_asset_tag")

    def test_a_duplicate_name_does_not_refuse_the_row(self):
        """Two rows can name one device legitimately, so the name only stops auto-matching."""
        self._device("srv-01")

        units = self._plan(self._row(2, "D-1", "srv-01"), self._row(3, "D-2", "srv-01"))

        self.assertEqual([unit.disposition for unit in units], [Disposition.ACTIONABLE] * 2)
        self.assertEqual([unit.changes[0].operation for unit in units], ["create"] * 2)

    def test_the_diagnostic_names_every_row_the_conflict_involves(self):
        """The operator picks which row gives the value up, so both row numbers have to be there."""
        units = self._plan(
            self._row(2, "D-1", "srv-01", serial="SN-1"),
            self._row(7, "D-2", "srv-02", serial="SN-1"),
        )

        # The plan freezes its values, so the row numbers arrive as a tuple.
        self.assertEqual(units[0].diagnostics[0].display["rows"], (2, 7))


class DeviceModuleDependencyTest(DeviceModulePlanTestBase):
    """A device needs a type and a role, and neither is this module's to create."""

    def test_a_device_type_netbox_does_not_have_is_blocked(self):
        """`blocked` is the disposition for an unmet dependency, not `invalid`."""
        units = self._plan(self._row(2, "D-1", "srv-01", make="Acme", model="Widget"))

        self.assertEqual(units[0].disposition, Disposition.BLOCKED)
        self.assertEqual(units[0].diagnostics[0].code, "device.device_type_missing")
        self.assertEqual(units[0].diagnostics[0].display["dt_slug"], "acme-widget")

    def test_a_role_netbox_does_not_have_is_blocked(self):
        """The class maps to a role slug, and the role behind it still has to exist."""
        ClassRoleMapping.objects.create(
            profile=self.profile, source_class="Switch", creates_rack=False, role_slug="network-switch"
        )

        units = self._plan(self._row(2, "D-1", "sw-01", device_class="Switch"))

        self.assertEqual(units[0].disposition, Disposition.BLOCKED)
        self.assertEqual(units[0].diagnostics[0].code, "device.role_missing")

    def test_a_rack_the_row_names_but_netbox_does_not_have_is_blocked(self):
        """A device row cannot create the rack it is placed in."""
        units = self._plan(self._row(2, "D-1", "srv-01", rack_name="no-such-rack"))

        self.assertEqual(units[0].disposition, Disposition.BLOCKED)
        self.assertEqual(units[0].diagnostics[0].code, "device.rack_missing")


class DeviceModuleMatchTest(DeviceModulePlanTestBase):
    """Matching decides whether the row creates a device or reconciles one."""

    def test_an_unmatched_row_is_actionable_as_a_create(self):
        units = self._plan(self._row(2, "D-1", "srv-01"))

        self.assertEqual(units[0].disposition, Disposition.ACTIONABLE)
        self.assertEqual(units[0].changes[0].operation, "create")
        self.assertEqual(units[0].changes[0].payload["name"], "srv-01")

    def test_a_matching_device_is_a_no_op(self):
        """NetBox already holds what the row asks for, so nothing should execute."""
        self._device("srv-01", rack=self.rack)

        units = self._plan(self._row(2, "D-1", "srv-01"))

        self.assertEqual(units[0].disposition, Disposition.NO_OP)
        self.assertEqual(units[0].changes, ())

    def test_a_differing_device_is_actionable_as_an_update(self):
        self._device("srv-01", rack=self.rack, serial="OLD")

        units = self._plan(self._row(2, "D-1", "srv-01", serial="NEW"))

        self.assertEqual(units[0].disposition, Disposition.ACTIONABLE)
        self.assertEqual(units[0].changes[0].operation, "update")
        self.assertEqual(units[0].changes[0].payload["serial"], "NEW")

    def test_a_serial_matches_a_device_the_name_does_not(self):
        """The serial is a stronger identifier than the name, so it decides first."""
        device = self._device("stored-name", rack=self.rack, serial="SN-1")

        units = self._plan(self._row(2, "D-1", "srv-01", serial="SN-1"))

        self.assertEqual(units[0].changes[0].operation, "update")
        self.assertEqual(units[0].changes[0].preconditions["device_id"], device.pk)

    def test_an_asset_tag_matches_a_device_the_name_does_not(self):
        device = self._device("stored-name", rack=self.rack, asset_tag="AT-1")

        units = self._plan(self._row(2, "D-1", "srv-01", asset_tag="AT-1"))

        self.assertEqual(units[0].changes[0].preconditions["device_id"], device.pk)

    def test_an_explicit_binding_outranks_every_other_identifier(self):
        """The operator linked this row to this device, which is the strongest statement there is."""
        self._device("srv-01", rack=self.rack)
        bound = self._device("bound-device", rack=self.rack)
        DeviceExistingMatch.objects.create(profile=self.profile, source_id="D-1", netbox_device_id=bound.pk)

        units = self._plan(self._row(2, "D-1", "srv-01"))

        self.assertEqual(units[0].changes[0].preconditions["device_id"], bound.pk)

    def test_an_ambiguous_serial_refuses_the_row_rather_than_guessing(self):
        """Two stored devices carry the serial, so no automatic answer is the safe one."""
        self._device("first", rack=self.rack, serial="SN-1")
        self._device("second", rack=self.rack, serial="SN-1")

        units = self._plan(self._row(2, "D-1", "srv-01", serial="SN-1"))

        self.assertEqual(units[0].disposition, Disposition.INVALID)
        self.assertEqual(units[0].diagnostics[0].code, "device.ambiguous_serial")

    def test_a_device_at_another_site_is_not_matched_by_name(self):
        """The name is only unique inside a site, so a global name match would cross sites."""
        from dcim.models import Site

        other = Site.objects.create(name="Other Site", slug="other-site")
        self._device("srv-01", site=other)

        units = self._plan(self._row(2, "D-1", "srv-01"))

        self.assertEqual(units[0].changes[0].operation, "create")

    def test_a_device_the_actor_cannot_view_is_not_matched(self):
        """Planning must not report target state the operator cannot see."""
        from dcim.models import Device, Rack

        from netbox_data_import.tests.helpers import user_with_object_permission

        self._device("srv-01", rack=self.rack)
        # The rack stays visible, so the only thing the actor cannot see is the device itself.
        actor = user_with_object_permission(
            "device-module-blind",
            [(Device, ("view",), {"name": "nothing-matches-this"}), (Rack, ("view",), {})],
        )
        reader = NetBoxReader.for_actor(actor).for_target(site=self.site)

        units = DeviceModule().plan(self._batch(self._row(2, "D-1", "srv-01")), self.profile, None, reader)

        self.assertEqual(units[0].changes[0].operation, "create")
