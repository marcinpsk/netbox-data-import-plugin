# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The Rack Target Module turns rack source rows into Synchronization Units.

Every branch the current rack pass reports as a row action maps onto exactly one disposition from
section 4.2, so these tests are the mapping written down.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase

from netbox_data_import.adapters import SourceBatch
from netbox_data_import.catalog import OutputKind
from netbox_data_import.models import ClassRoleMapping, IgnoredDevice, ImportProfile
from netbox_data_import.netbox_reader import NetBoxReader
from netbox_data_import.plan import Disposition
from netbox_data_import.target_modules import ExecutionContext, PreconditionFailed, RackModule


class RackModulePlanTest(TestCase):
    """One rack row in, one Synchronization Unit out, with the disposition its state earns."""

    def setUp(self):
        """A site, a rack-creating class, and a profile that maps to it."""
        from dcim.models import Site

        self.site = Site.objects.create(name="Rack Module Site", slug="rack-module-site")
        self.profile = ImportProfile.objects.create(
            name="Rack Module Profile", adapter_config={"sheet_name": "Data", "update_existing": True}
        )
        ClassRoleMapping.objects.create(profile=self.profile, source_class="Cabinet", creates_rack=True)
        ClassRoleMapping.objects.create(profile=self.profile, source_class="Server", creates_rack=False)
        self.reader = NetBoxReader.unrestricted().for_target(site=self.site)

    def _batch(self, *rows):
        """Wrap rows the way the flat adapter hands them over."""
        return SourceBatch(output_kinds=frozenset({OutputKind.RACK_SOURCE_ROW}), rows=tuple(rows))

    def _row(self, number, source_id, rack_name, device_class="Cabinet", **extra):
        row = {
            "_row_number": number,
            "source_id": source_id,
            "device_class": device_class,
            "rack_name": rack_name,
            "u_height": 42,
            "serial": "",
        }
        row.update(extra)
        return row

    def _plan(self, *rows):
        return RackModule().plan(self._batch(*rows), self.profile, None, self.reader)

    def test_a_new_rack_is_actionable_and_carries_one_change(self):
        """The row names a rack the site does not have, so the unit has work to do."""
        units = self._plan(self._row(2, "RACK-1", "cab-01"))

        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].disposition, Disposition.ACTIONABLE)
        self.assertEqual(len(units[0].changes), 1)
        self.assertEqual(units[0].changes[0].operation, "create")
        self.assertEqual(units[0].changes[0].payload["name"], "cab-01")
        self.assertEqual(units[0].changes[0].payload["u_height"], 42)

    def test_a_row_whose_class_does_not_create_a_rack_produces_no_unit(self):
        """Rack policy decides which rows are the Rack module's, and a device row is not."""
        self.assertEqual(self._plan(self._row(2, "SRV-1", "cab-01", device_class="Server")), [])

    def test_a_matching_rack_is_a_no_op(self):
        """NetBox already holds what the row asks for, so nothing should execute."""
        from dcim.models import Rack

        Rack.objects.create(name="cab-01", site=self.site, u_height=42)

        units = self._plan(self._row(2, "RACK-1", "cab-01"))

        self.assertEqual(units[0].disposition, Disposition.NO_OP)
        self.assertEqual(units[0].changes, ())

    def test_a_differing_rack_is_actionable_as_an_update(self):
        """The stored height differs, so the unit carries the write that reconciles it."""
        from dcim.models import Rack

        Rack.objects.create(name="cab-01", site=self.site, u_height=20)

        units = self._plan(self._row(2, "RACK-1", "cab-01"))

        self.assertEqual(units[0].disposition, Disposition.ACTIONABLE)
        self.assertEqual(units[0].changes[0].operation, "update")
        self.assertEqual(units[0].changes[0].payload["u_height"], 42)

    def test_an_ignored_source_id_is_excluded(self):
        """`excluded` is reserved for operator policy, which is exactly what an ignore is."""
        IgnoredDevice.objects.create(profile=self.profile, source_id="RACK-1")

        units = self._plan(self._row(2, "RACK-1", "cab-01"))

        self.assertEqual(units[0].disposition, Disposition.EXCLUDED)
        self.assertEqual(units[0].changes, ())

    def test_a_row_with_no_rack_name_is_invalid(self):
        """An unsupported source construct is invalid, never excluded."""
        units = self._plan(self._row(2, "RACK-1", ""))

        self.assertEqual(units[0].disposition, Disposition.INVALID)
        self.assertTrue(any(d.severity == "error" for d in units[0].diagnostics))

    def test_a_duplicate_rack_name_in_one_file_is_invalid(self):
        """Two rows claiming one rack cannot both be planned, so neither is."""
        units = self._plan(self._row(2, "RACK-1", "cab-01"), self._row(3, "RACK-2", "cab-01"))

        self.assertEqual([unit.disposition for unit in units], [Disposition.INVALID, Disposition.INVALID])
        self.assertTrue(all(any(d.code == "rack.duplicate_name" for d in unit.diagnostics) for unit in units))

    def test_a_duplicate_source_id_in_one_file_is_invalid(self):
        """The source identity has to name one rack, or later replanning cannot match it."""
        units = self._plan(self._row(2, "RACK-1", "cab-01"), self._row(3, "RACK-1", "cab-02"))

        self.assertEqual([unit.disposition for unit in units], [Disposition.INVALID, Disposition.INVALID])
        self.assertTrue(all(any(d.code == "rack.duplicate_source_id" for d in unit.diagnostics) for unit in units))

    def test_identity_is_stable_across_replanning(self):
        """Section 4.3: the identity survives replanning and does not come from the row number."""
        first = self._plan(self._row(2, "RACK-1", "cab-01"))
        second = self._plan(self._row(9, "RACK-1", "cab-01"))

        self.assertEqual(first[0].identity, second[0].identity)

    def test_a_rack_the_actor_cannot_view_is_not_matched(self):
        """Planning reads through the scoped reader, so an invisible rack is not a match."""
        from dcim.models import Rack

        from netbox_data_import.tests.helpers import user_with_object_permission

        Rack.objects.create(name="cab-01", site=self.site, u_height=42)
        actor = user_with_object_permission("rack-module-actor", [(Rack, ["view"], {"name": "somewhere-else"})])
        scoped = NetBoxReader.for_actor(actor).for_target(site=self.site)

        units = RackModule().plan(self._batch(self._row(2, "RACK-1", "cab-01")), self.profile, None, scoped)

        self.assertEqual(units[0].changes[0].operation, "create")


class RackModuleApplyTest(TestCase):
    """Applying one Planned Change writes exactly what the plan said, or refuses."""

    def setUp(self):
        """A site and a profile whose Cabinet class creates racks."""
        from dcim.models import Site

        self.site = Site.objects.create(name="Rack Apply Site", slug="rack-apply-site")
        self.profile = ImportProfile.objects.create(
            name="Rack Apply Profile", adapter_config={"sheet_name": "Data", "update_existing": True}
        )
        ClassRoleMapping.objects.create(profile=self.profile, source_class="Cabinet", creates_rack=True)
        self.actor = get_user_model().objects.create_superuser(
            username="rack-apply-actor", email="rack-apply@example.invalid", password="testpass"
        )
        self.reader = NetBoxReader.for_actor(self.actor).for_target(site=self.site)
        self.context = ExecutionContext(actor=self.actor, reader=self.reader)

    def _only_change(self, *rows):
        batch = SourceBatch(output_kinds=frozenset({OutputKind.RACK_SOURCE_ROW}), rows=tuple(rows))
        units = RackModule().plan(batch, self.profile, None, self.reader)
        return units[0].changes[0]

    def _row(self, source_id, rack_name, **extra):
        row = {
            "_row_number": 2,
            "source_id": source_id,
            "device_class": "Cabinet",
            "rack_name": rack_name,
            "u_height": 42,
            "serial": "",
        }
        row.update(extra)
        return row

    def test_a_create_change_writes_the_rack_the_plan_described(self):
        """The payload is the whole instruction, so applying it needs no second look at the source."""
        from dcim.models import Rack

        change = self._only_change(self._row("RACK-1", "apply-cab-01"))

        rack = RackModule().apply(change, self.context)

        self.assertEqual(rack.name, "apply-cab-01")
        self.assertEqual(rack.site, self.site)
        self.assertEqual(Rack.objects.filter(name="apply-cab-01", site=self.site).count(), 1)

    def test_an_update_change_reconciles_the_stored_rack(self):
        """The planned height replaces the stored one."""
        from dcim.models import Rack

        Rack.objects.create(name="apply-cab-02", site=self.site, u_height=20)
        change = self._only_change(self._row("RACK-2", "apply-cab-02"))
        self.assertEqual(change.operation, "update")

        rack = RackModule().apply(change, self.context)

        rack.refresh_from_db()
        self.assertEqual(rack.u_height, 42)

    def test_a_precondition_that_no_longer_holds_is_refused(self):
        """Section 4.6: the module rechecks its preconditions inside the transaction."""
        from dcim.models import Rack

        rack = Rack.objects.create(name="apply-cab-03", site=self.site, u_height=20)
        change = self._only_change(self._row("RACK-3", "apply-cab-03"))
        rack.u_height = 30
        rack.save(update_fields=["u_height"])

        with self.assertRaises(PreconditionFailed):
            RackModule().apply(change, self.context)

    def test_a_vanished_rack_is_refused_rather_than_recreated(self):
        """An update whose target is gone is stale state, not an invitation to create."""
        from dcim.models import Rack

        rack = Rack.objects.create(name="apply-cab-04", site=self.site, u_height=20)
        change = self._only_change(self._row("RACK-4", "apply-cab-04"))
        rack.delete()

        with self.assertRaises(PreconditionFailed):
            RackModule().apply(change, self.context)

    def test_an_actor_without_the_change_permission_is_refused(self):
        """The module enforces the permission itself; planning visibility is not permission to write."""
        from dcim.models import Rack

        from netbox_data_import.object_permissions import ObjectPermissionDenied
        from netbox_data_import.tests.helpers import user_with_object_permission

        Rack.objects.create(name="apply-cab-05", site=self.site, u_height=20)
        change = self._only_change(self._row("RACK-5", "apply-cab-05"))
        viewer = user_with_object_permission("rack-apply-viewer", [(Rack, ["view"], None)])
        context = ExecutionContext(actor=viewer, reader=NetBoxReader.for_actor(viewer).for_target(site=self.site))

        with self.assertRaises(ObjectPermissionDenied):
            RackModule().apply(change, context)
