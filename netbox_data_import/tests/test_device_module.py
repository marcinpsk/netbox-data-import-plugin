# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The Device Target Module turns device source rows into Synchronization Units.

Every branch the current device pass reports as a row action maps onto exactly one disposition from
section 4.2, so these tests are the mapping written down. This layer covers identity, the duplicate
checks, matching against NetBox, the operator's saved field review, and the disposition each of
those earns. Contacts and IP assignment arrive with the next layer.
"""

from django.test import TestCase

from netbox_data_import.adapters import SourceBatch
from netbox_data_import.catalog import OutputKind
from netbox_data_import.models import (
    ClassRoleMapping,
    DeviceExistingMatch,
    DeviceImportSource,
    IgnoredDevice,
    IgnoredFieldDifference,
    ImportProfile,
)
from netbox_data_import.netbox_reader import NetBoxReader
from netbox_data_import.plan import Disposition
from netbox_data_import.target_modules import DeviceModule, ExecutionContext, PreconditionFailed


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

    def _with_provenance(self, device, source_id="D-1", asset_tag="", extra_columns=None):
        """Record the provenance a previous import of this row would have left behind."""
        DeviceImportSource.objects.create(
            device=device, profile=self.profile, source_id=source_id, extra_columns=extra_columns or {}
        )
        DeviceExistingMatch.objects.create(
            profile=self.profile,
            source_id=source_id,
            netbox_device_id=device.pk,
            device_name=device.name,
            source_asset_tag=asset_tag,
        )
        return device

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
        self._with_provenance(self._device("srv-01", rack=self.rack))

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

        units = self._plan(self._row(2, "D-1", "srv-01", serial="SN-1", status="Offline"))

        self.assertEqual(units[0].changes[0].operation, "update")
        self.assertEqual(units[0].changes[0].preconditions["device_id"], device.pk)

    def test_an_asset_tag_matches_a_device_the_name_does_not(self):
        device = self._device("stored-name", rack=self.rack, asset_tag="AT-1")

        units = self._plan(self._row(2, "D-1", "srv-01", asset_tag="AT-1", status="Offline"))

        self.assertEqual(units[0].changes[0].preconditions["device_id"], device.pk)

    def test_an_explicit_binding_outranks_every_other_identifier(self):
        """The operator linked this row to this device, which is the strongest statement there is."""
        self._device("srv-01", rack=self.rack)
        bound = self._device("bound-device", rack=self.rack)
        DeviceExistingMatch.objects.create(profile=self.profile, source_id="D-1", netbox_device_id=bound.pk)

        units = self._plan(self._row(2, "D-1", "srv-01", status="Offline"))

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


class DeviceModulePlacementTest(DeviceModulePlanTestBase):
    """Where a device sits is part of what the row writes, and the rack has to have room."""

    def test_the_placement_the_row_names_reaches_the_change(self):
        units = self._plan(self._row(2, "D-1", "srv-01", u_position="5", face="Front", airflow="Front to Back"))

        payload = units[0].changes[0].payload
        self.assertEqual(payload["u_position"], 5)
        self.assertEqual(payload["face"], "front")
        self.assertEqual(payload["airflow"], "front-to-rear")

    def test_a_source_word_the_importer_already_reads_is_translated(self):
        """`Back` and `Rear` are one NetBox face, and the module reads the same table as the engine."""
        units = self._plan(self._row(2, "D-1", "srv-01", u_position="5", face="Back"))

        self.assertEqual(units[0].changes[0].payload["face"], "rear")

    def test_a_status_the_source_spells_its_own_way_is_translated(self):
        units = self._plan(self._row(2, "D-1", "srv-01", status="Live"))

        self.assertEqual(units[0].changes[0].payload["status"], "active")

    def test_a_position_with_no_rack_is_invalid(self):
        units = self._plan(self._row(2, "D-1", "srv-01", rack_name="", u_position="5", face="Front"))

        self.assertEqual(units[0].disposition, Disposition.INVALID)
        self.assertEqual(units[0].diagnostics[0].code, "device.rack_required")

    def test_a_position_with_no_face_is_invalid(self):
        units = self._plan(self._row(2, "D-1", "srv-01", u_position="5"))

        self.assertEqual(units[0].disposition, Disposition.INVALID)
        self.assertEqual(units[0].diagnostics[0].code, "device.face_required")

    def test_a_face_with_no_rack_is_dropped(self):
        """NetBox refuses a rack face on a device that is in no rack, so the plan cannot ask for one."""
        units = self._plan(self._row(2, "D-1", "srv-01", rack_name="", face="Front"))

        self.assertEqual(units[0].disposition, Disposition.ACTIONABLE)
        self.assertEqual(units[0].changes[0].payload["face"], "")

    def test_a_position_a_stored_device_already_fills_is_invalid(self):
        """The write would fail on the rack's own constraint, so planning says so first."""
        self._device("stored", rack=self.rack, position=5, face="front")

        units = self._plan(self._row(2, "D-1", "srv-01", u_position="5", face="Front"))

        self.assertEqual(units[0].disposition, Disposition.INVALID)
        self.assertEqual(units[0].diagnostics[0].code, "device.rack_position_occupied")

    def test_two_rows_claiming_one_slot_name_each_other(self):
        """The second row is refused, and the operator needs to know which row took the slot."""
        units = self._plan(
            self._row(2, "D-1", "srv-01", u_position="5", face="Front"),
            self._row(7, "D-2", "srv-02", u_position="5", face="Front"),
        )

        self.assertEqual(units[0].disposition, Disposition.ACTIONABLE)
        self.assertEqual(units[1].disposition, Disposition.INVALID)
        self.assertEqual(units[1].diagnostics[0].code, "device.rack_position_claimed")
        self.assertEqual(units[1].diagnostics[0].display["claimed_by_row"], 2)

    def test_a_device_keeping_its_own_slot_is_not_refused(self):
        """A matched device already occupies the position, and staying put is not a conflict."""
        self._with_provenance(self._device("srv-01", rack=self.rack, position=5, face="front"))

        units = self._plan(self._row(2, "D-1", "srv-01", u_position="5", face="Front"))

        self.assertEqual(units[0].disposition, Disposition.NO_OP)

    def test_a_taller_device_that_does_not_fit_is_invalid(self):
        """A 2U device at the top of the rack has nowhere to put its second unit."""
        from dcim.models import DeviceType

        DeviceType.objects.create(manufacturer=self.manufacturer, model="R760", slug="dell-r760", u_height=2)

        units = self._plan(self._row(2, "D-1", "srv-01", model="R760", u_position="42", face="Front"))

        self.assertEqual(units[0].disposition, Disposition.INVALID)
        self.assertEqual(units[0].diagnostics[0].code, "device.rack_position_occupied")


class DeviceModuleZeroUTest(DeviceModulePlanTestBase):
    """A zero-U device type holds no rack position and no face, whatever the row says."""

    def setUp(self):
        super().setUp()
        from dcim.models import DeviceType

        DeviceType.objects.create(manufacturer=self.manufacturer, model="PDU", slug="dell-pdu", u_height=0)

    def _pdu_row(self, **extra):
        return self._row(2, "D-1", "pdu-01", model="PDU", **extra)

    def test_the_position_the_row_carries_is_dropped(self):
        units = self._plan(self._pdu_row(u_position="5", face="Front"))

        self.assertIsNone(units[0].changes[0].payload["u_position"])
        self.assertEqual(units[0].changes[0].payload["face"], "")

    def test_a_position_with_no_face_is_not_refused(self):
        """The face rule guards a real rack position, and a zero-U type has none to guard."""
        units = self._plan(self._pdu_row(u_position="5"))

        self.assertEqual(units[0].disposition, Disposition.ACTIONABLE)

    def test_two_zero_u_rows_do_not_claim_one_slot(self):
        """Neither row occupies a unit, so neither can take the other's."""
        units = self._plan(
            self._row(2, "D-1", "pdu-01", model="PDU", u_position="5", face="Front"),
            self._row(7, "D-2", "pdu-02", model="PDU", u_position="5", face="Front"),
        )

        self.assertEqual([unit.disposition for unit in units], [Disposition.ACTIONABLE] * 2)


class DeviceModuleApplyTest(DeviceModulePlanTestBase):
    """Applying one Planned Change writes the device the plan described, or refuses to."""

    def setUp(self):
        super().setUp()
        from netbox_data_import.tests.helpers import user_with_object_permission
        from dcim.models import Device

        self.actor = user_with_object_permission("device-module-writer", [(Device, ("add", "change", "view"), {})])
        self.context = ExecutionContext(actor=self.actor, reader=self.reader, profile=self.profile)

    def _only_change(self, *rows):
        """Return the single change the given rows plan."""
        units = self._plan(*rows)
        self.assertEqual(len(units), 1, [unit.diagnostics for unit in units])
        self.assertEqual(len(units[0].changes), 1, units[0].disposition)
        return units[0].changes[0]

    def test_a_create_change_writes_the_device_the_plan_described(self):
        change = self._only_change(
            self._row(2, "D-1", "srv-01", serial="SN-1", asset_tag="AT-1", u_position="5", face="Front")
        )

        device = DeviceModule().apply(change, self.context)

        device.refresh_from_db()
        self.assertEqual(device.name, "srv-01")
        self.assertEqual(device.serial, "SN-1")
        self.assertEqual(device.asset_tag, "AT-1")
        self.assertEqual(device.rack_id, self.rack.pk)
        self.assertEqual(device.position, 5)
        self.assertEqual(device.face, "front")
        self.assertEqual(device.device_type_id, self.device_type.pk)
        self.assertEqual(device.role_id, self.role.pk)

    def test_an_update_change_reconciles_the_stored_device(self):
        stored = self._device("srv-01", rack=self.rack, serial="OLD")

        change = self._only_change(self._row(2, "D-1", "srv-01", serial="NEW"))
        DeviceModule().apply(change, self.context)

        stored.refresh_from_db()
        self.assertEqual(stored.serial, "NEW")

    def test_a_vanished_device_is_refused_rather_than_recreated(self):
        """The plan named a device to update, and creating a new one instead is not that."""
        from dcim.models import Device

        stored = self._device("srv-01", rack=self.rack, serial="OLD")
        change = self._only_change(self._row(2, "D-1", "srv-01", serial="NEW"))
        Device.objects.filter(pk=stored.pk).delete()

        with self.assertRaises(PreconditionFailed):
            DeviceModule().apply(change, self.context)

    def test_a_device_type_that_moved_since_the_plan_is_refused(self):
        """The type decides the height, so the placement the plan checked no longer holds."""
        from dcim.models import Device, DeviceType

        stored = self._device("srv-01", rack=self.rack, serial="OLD")
        change = self._only_change(self._row(2, "D-1", "srv-01", serial="NEW"))
        other = DeviceType.objects.create(manufacturer=self.manufacturer, model="R760", slug="dell-r760", u_height=1)
        Device.objects.filter(pk=stored.pk).update(device_type=other)

        with self.assertRaises(PreconditionFailed):
            DeviceModule().apply(change, self.context)

    def test_an_actor_without_the_add_permission_is_refused(self):
        """An ObjectPermission constraint is only decided against the saved row."""
        from dcim.models import Device

        from netbox_data_import.object_permissions import ObjectPermissionDenied
        from netbox_data_import.tests.helpers import user_with_object_permission

        blocked = user_with_object_permission(
            "device-module-blocked", [(Device, ("add", "view"), {"name": "only-this-name"})]
        )
        change = self._only_change(self._row(2, "D-1", "srv-01"))

        with self.assertRaises(ObjectPermissionDenied):
            DeviceModule().apply(change, ExecutionContext(actor=blocked, reader=self.reader, profile=self.profile))

    def test_a_zero_u_device_is_written_without_a_position(self):
        """NetBox refuses a position on a zero-U type, so the plan never carries one."""
        from dcim.models import DeviceType

        DeviceType.objects.create(manufacturer=self.manufacturer, model="PDU", slug="dell-pdu", u_height=0)

        change = self._only_change(self._row(2, "D-1", "pdu-01", model="PDU", u_position="5", face="Front"))
        device = DeviceModule().apply(change, self.context)

        device.refresh_from_db()
        self.assertIsNone(device.position)
        self.assertEqual(device.face, "")


class DeviceModuleFieldReviewTest(DeviceModulePlanTestBase):
    """An ignored difference means leave the field alone, so neither the plan nor the write carries it."""

    def _ignore(self, device, target_field, file_value, netbox_value, source_id="D-1"):
        """Record the exact review an operator saves from the preview."""
        IgnoredFieldDifference.objects.create(
            profile=self.profile,
            source_id=source_id,
            netbox_device_id=device.pk,
            target_field=target_field,
            file_snapshot={"canonical": file_value, "display": file_value},
            netbox_snapshot={"canonical": netbox_value, "display": netbox_value},
        )

    def test_a_row_whose_only_difference_is_ignored_is_a_no_op(self):
        """Planning a change the review already settled would execute a write the operator refused."""
        device = self._with_provenance(self._device("srv-01", rack=self.rack, serial="OLD"))
        self._ignore(device, "serial", "NEW", "OLD")

        units = self._plan(self._row(2, "D-1", "srv-01", serial="NEW"))

        self.assertEqual(units[0].disposition, Disposition.NO_OP, units[0].diagnostics)
        self.assertEqual(units[0].changes, ())

    def test_an_ignored_field_keeps_the_stored_value_in_the_payload(self):
        """The guard has to stop the ignored field only, not every field the row carries."""
        self._ignore(self._device("srv-01", rack=self.rack, serial="OLD", asset_tag="AT-OLD"), "serial", "NEW", "OLD")

        units = self._plan(self._row(2, "D-1", "srv-01", serial="NEW", asset_tag="AT-NEW"))

        self.assertEqual(units[0].disposition, Disposition.ACTIONABLE)
        payload = units[0].changes[0].payload
        self.assertEqual(payload["asset_tag"], "AT-NEW")
        self.assertEqual(payload["serial"], "OLD")

    def test_a_review_saved_against_another_device_does_not_apply(self):
        """A review names one device, so it cannot settle a difference on a different one."""
        self._device("srv-01", rack=self.rack, serial="OLD")
        self._ignore(self._device("other-device", rack=self.rack), "serial", "NEW", "OLD")

        units = self._plan(self._row(2, "D-1", "srv-01", serial="NEW"))

        self.assertEqual(units[0].disposition, Disposition.ACTIONABLE)
        self.assertEqual(units[0].changes[0].payload["serial"], "NEW")

    def test_a_review_whose_values_moved_on_does_not_apply(self):
        """The review pins one exact pair, so a new file value is a new difference."""
        self._ignore(self._device("srv-01", rack=self.rack, serial="OLD"), "serial", "NEW", "OLD")

        units = self._plan(self._row(2, "D-1", "srv-01", serial="NEWER"))

        self.assertEqual(units[0].disposition, Disposition.ACTIONABLE)
        self.assertEqual(units[0].changes[0].payload["serial"], "NEWER")


class DeviceModuleContactTest(DeviceModulePlanTestBase):
    """A device row carries a primary contact, and the plan decides it before anything is written."""

    def setUp(self):
        """Give the profile the contact role that makes contact resolution active."""
        super().setUp()
        from tenancy.models import ContactRole

        self.contact_role = ContactRole.objects.create(name="Primary Contact", slug="primary-contact")
        self.profile.adapter_config = {
            **self.profile.adapter_config,
            "primary_contact_role": self.contact_role.name,
            "primary_contact_lookup_field": "email",
        }
        self.profile.save(update_fields=["adapter_config"])
        self.actor = self._contact_writer()
        self.context = ExecutionContext(actor=self.actor, reader=self.reader, profile=self.profile)

    @staticmethod
    def _contact_writer():
        """Return an actor allowed to write the device and the contact it is assigned."""
        from dcim.models import Device
        from tenancy.models import Contact, ContactAssignment

        from netbox_data_import.tests.helpers import user_with_object_permission

        return user_with_object_permission(
            "device-module-contact-writer",
            [
                (Device, ("add", "change", "view"), {}),
                (Contact, ("add", "view"), {}),
                (ContactAssignment, ("add", "change", "view"), {}),
            ],
        )

    def test_applying_a_create_assigns_the_contact_the_plan_carried(self):
        """A plan that names a contact but never assigns it has written half the row."""
        from django.contrib.contenttypes.models import ContentType
        from tenancy.models import ContactAssignment

        units = self._plan(self._row(2, "D-1", "srv-01", primary_contact="owner@example.invalid"))

        device = DeviceModule().apply(units[0].changes[0], self.context)

        assignment = ContactAssignment.objects.get(
            object_type=ContentType.objects.get_for_model(device),
            object_id=device.pk,
            role=self.contact_role,
        )
        self.assertEqual(assignment.contact.email, "owner@example.invalid")

    def _assign(self, device, email):
        """Let the real resolver establish the state a later plan has to call unchanged."""
        from netbox_data_import.contact_resolution import PrimaryContactResolver

        review = PrimaryContactResolver.review(device, {"primary_contact": email}, self.profile, None)
        PrimaryContactResolver.apply(device, self.profile, review, None)

    def test_a_contact_the_device_already_holds_is_not_a_change(self):
        """Re-importing a settled contact must not make an otherwise identical row actionable."""
        device = self._with_provenance(self._device("srv-01", rack=self.rack))
        self._assign(device, "owner@example.invalid")

        units = self._plan(self._row(2, "D-1", "srv-01", primary_contact="owner@example.invalid"))

        self.assertEqual(units[0].disposition, Disposition.NO_OP, units[0].diagnostics)

    def test_a_contact_the_device_does_not_hold_makes_the_row_actionable(self):
        """The row writes an assignment NetBox does not have, so it is work the plan must carry."""
        self._device("srv-01", rack=self.rack)

        units = self._plan(self._row(2, "D-1", "srv-01", primary_contact="owner@example.invalid"))

        self.assertEqual(units[0].disposition, Disposition.ACTIONABLE)
        contact = units[0].changes[0].payload["contact"]
        self.assertEqual(contact["values"]["email"], "owner@example.invalid")

    def test_a_create_carries_the_contact_its_row_names(self):
        """A created device has no contact until the plan says which one it gets."""
        units = self._plan(self._row(2, "D-1", "srv-01", primary_contact="owner@example.invalid"))

        self.assertEqual(units[0].changes[0].operation, "create")
        self.assertEqual(units[0].changes[0].payload["contact"]["values"]["email"], "owner@example.invalid")

    def test_a_row_that_needs_a_contact_decision_is_refused(self):
        """Guessing which candidate column supplies a Contact field is the operator's call."""
        row = self._row(2, "D-1", "srv-01")
        row["_candidate_values"] = {"contact": {"Owner": "OBO"}}

        units = self._plan(row)

        self.assertEqual(units[0].disposition, Disposition.INVALID, units[0].diagnostics)
        self.assertEqual(units[0].diagnostics[0].code, "device.contact_resolution_required")
        self.assertEqual(units[0].diagnostics[0].display["candidate_values"], {"Owner": "OBO"})

    def test_a_row_with_no_contact_values_plans_no_contact(self):
        """A profile with a contact role must not invent a contact for a row that names none."""
        self._with_provenance(self._device("srv-01", rack=self.rack))

        units = self._plan(self._row(2, "D-1", "srv-01"))

        self.assertEqual(units[0].disposition, Disposition.NO_OP, units[0].diagnostics)


class DeviceModuleDoesNotRenameTest(DeviceModulePlanTestBase):
    """An import reconciles a device it matched; it does not rename it.

    The name is how a row finds a device, so writing it back would let a source spelling silently
    retitle a NetBox device an operator named deliberately. Renaming is an explicit action, not a
    side effect of an import.
    """

    def setUp(self):
        """Give this test an actor allowed to write the device it plans."""
        super().setUp()
        from dcim.models import Device

        from netbox_data_import.tests.helpers import user_with_object_permission

        self.actor = user_with_object_permission("device-module-rename", [(Device, ("add", "change", "view"), {})])
        self.context = ExecutionContext(actor=self.actor, reader=self.reader, profile=self.profile)

    def test_a_name_the_row_spells_differently_is_not_work_on_its_own(self):
        """The row reconciles a device it matched by serial, and every other field agrees."""
        self._with_provenance(self._device("stored-name", rack=self.rack, serial="SN-1"))

        units = self._plan(self._row(2, "D-1", "srv-01", serial="SN-1"))

        self.assertEqual(units[0].disposition, Disposition.NO_OP, units[0].diagnostics)

    def test_applying_a_change_leaves_the_stored_name_alone(self):
        """A row that carries real work must still not rename the device while it does it."""
        device = self._device("stored-name", rack=self.rack, serial="SN-1", asset_tag="AT-OLD")

        units = self._plan(self._row(2, "D-1", "srv-01", serial="SN-1", asset_tag="AT-NEW"))
        DeviceModule().apply(units[0].changes[0], self.context)

        device.refresh_from_db()
        self.assertEqual(device.asset_tag, "AT-NEW")
        self.assertEqual(device.name, "stored-name")

    def test_a_created_device_still_takes_the_name_its_row_carries(self):
        """Nothing exists to protect on a create, so the row names the device it makes."""
        units = self._plan(self._row(2, "D-1", "srv-01"))

        device = DeviceModule().apply(units[0].changes[0], self.context)

        self.assertEqual(device.name, "srv-01")


class DeviceModuleProvenanceTest(DeviceModulePlanTestBase):
    """A device the import wrote carries its source, and that is how the next run finds it again.

    The stored source ID is the identifier that survives a renamed device, a changed serial and a
    re-cut asset tag, so it is the one most rows reconcile on.
    """

    def setUp(self):
        """Give this test an actor allowed to write the devices it plans."""
        super().setUp()
        from dcim.models import Device

        from netbox_data_import.tests.helpers import user_with_object_permission

        self.actor = user_with_object_permission("device-module-provenance", [(Device, ("add", "change", "view"), {})])
        self.context = ExecutionContext(actor=self.actor, reader=self.reader, profile=self.profile)

    def test_a_stored_source_id_matches_the_device_it_was_written_on(self):
        """Neither the name nor the serial agrees, so only the stored source can find this device."""
        device = self._device("stored-name", rack=self.rack)
        DeviceImportSource.objects.create(device=device, profile=self.profile, source_id="D-1")

        units = self._plan(self._row(2, "D-1", "srv-01", status="Offline"))

        self.assertEqual(units[0].changes[0].preconditions["device_id"], device.pk)

    def test_one_source_id_stored_on_two_devices_refuses_the_row(self):
        """Two devices claim this row, and picking one silently would write to the wrong device."""
        for name in ("first", "second"):
            DeviceImportSource.objects.create(
                device=self._device(name, rack=self.rack), profile=self.profile, source_id="D-1"
            )

        units = self._plan(self._row(2, "D-1", "srv-01"))

        self.assertEqual(units[0].disposition, Disposition.INVALID, units[0].diagnostics)
        self.assertEqual(units[0].diagnostics[0].code, "device.ambiguous_stored_source_id")

    def test_a_saved_field_review_matches_the_device_it_was_saved_against(self):
        """The operator reviewed a field on this device, which names the device this row reconciles."""
        device = self._device("stored-name", rack=self.rack)
        IgnoredFieldDifference.objects.create(
            profile=self.profile,
            source_id="D-1",
            netbox_device_id=device.pk,
            target_field="serial",
            file_snapshot={"canonical": "NEW", "display": "NEW"},
            netbox_snapshot={"canonical": "OLD", "display": "OLD"},
        )

        units = self._plan(self._row(2, "D-1", "srv-01", status="Offline"))

        self.assertEqual(units[0].changes[0].preconditions["device_id"], device.pk)

    def test_applying_a_create_stores_the_source_the_row_carried(self):
        """Without the record the next import cannot find the device this one just made."""
        units = self._plan(self._row(2, "D-1", "srv-01"))

        device = DeviceModule().apply(units[0].changes[0], self.context)

        stored = DeviceImportSource.objects.get(device=device)
        self.assertEqual(stored.source_id, "D-1")
        self.assertEqual(stored.profile, self.profile)

    def test_applying_a_create_binds_the_source_to_the_device(self):
        """The binding is what makes the match explicit rather than inferred on the next run."""
        units = self._plan(self._row(2, "D-1", "srv-01", asset_tag="AT-1"))

        device = DeviceModule().apply(units[0].changes[0], self.context)

        binding = DeviceExistingMatch.objects.get(profile=self.profile, source_id="D-1")
        self.assertEqual(binding.netbox_device_id, device.pk)
        self.assertEqual(binding.device_name, device.name)
        self.assertEqual(binding.source_asset_tag, "AT-1")


class DeviceModuleProvenanceIsWorkTest(DeviceModulePlanTestBase):
    """A device that holds every field but no provenance still has its source to record."""

    def test_a_matched_device_with_no_stored_provenance_is_work(self):
        """The next import finds this device only if this one records what wrote it."""
        self._device("srv-01", rack=self.rack)

        units = self._plan(self._row(2, "D-1", "srv-01"))

        self.assertEqual(units[0].disposition, Disposition.ACTIONABLE, units[0].diagnostics)

    def test_a_device_whose_provenance_is_current_is_a_no_op(self):
        """Everything the row writes is already written, provenance included."""
        self._with_provenance(self._device("srv-01", rack=self.rack))

        units = self._plan(self._row(2, "D-1", "srv-01"))

        self.assertEqual(units[0].disposition, Disposition.NO_OP, units[0].diagnostics)

    def test_a_stale_binding_name_is_work(self):
        """The binding records the device name, so a renamed device leaves it to be refreshed."""
        device = self._with_provenance(self._device("srv-01", rack=self.rack))
        DeviceExistingMatch.objects.filter(profile=self.profile, source_id="D-1").update(device_name="old-name")

        units = self._plan(self._row(2, "D-1", "srv-01"))

        self.assertEqual(units[0].disposition, Disposition.ACTIONABLE, units[0].diagnostics)
        self.assertEqual(units[0].changes[0].preconditions["device_id"], device.pk)
