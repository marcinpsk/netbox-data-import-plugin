# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Verify that every rack row action maps to one section 4.2 disposition."""

from django.contrib.auth import get_user_model
from django.test import TestCase

from netbox_data_import.adapters import SourceBatch
from netbox_data_import.catalog import CATALOG, OutputKind
from netbox_data_import.models import ClassRoleMapping, IgnoredDevice, ImportProfile
from netbox_data_import.netbox_reader import NetBoxReader
from netbox_data_import.plan import Disposition
from netbox_data_import.target_modules import ExecutionContext, PreconditionFailed, RackModule


class RackModuleRowMixin:
    """Provide the source-row helpers shared by rack module tests."""

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


class RackModulePlanTestBase(RackModuleRowMixin, TestCase):
    """Provide the target state and source-row helpers for rack planning tests."""

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

    def _plan(self, *rows):
        return RackModule().plan(self._batch(*rows), self.profile, CATALOG, self.reader)


class RackModulePlanTest(RackModulePlanTestBase):
    """One rack row in, one Synchronization Unit out, with the disposition its state earns."""

    def test_a_new_rack_is_actionable_and_carries_one_change(self):
        """The row names a rack the site does not have, so the unit has work to do."""
        units = self._plan(self._row(2, "RACK-1", "cab-01"))

        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].disposition, Disposition.ACTIONABLE)
        self.assertEqual(len(units[0].changes), 1)
        self.assertEqual(units[0].changes[0].operation, "create")
        self.assertEqual(units[0].changes[0].payload["name"], "cab-01")
        self.assertEqual(units[0].changes[0].payload["u_height"], 42)

    def test_a_nonfinite_height_falls_back_to_the_default(self):
        """An infinite spreadsheet value must not abort planning for the whole workbook."""
        units = self._plan(self._row(2, "RACK-1", "cab-01", u_height="Infinity"))

        self.assertEqual(units[0].disposition, Disposition.ACTIONABLE)
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

    def test_update_existing_false_leaves_a_differing_rack_alone(self):
        """The profile policy disables updates, so an existing rack has no executable work."""
        from dcim.models import Rack

        Rack.objects.create(name="cab-01", site=self.site, u_height=20)
        self.profile.adapter_config = {**self.profile.adapter_config, "update_existing": False}
        self.profile.save(update_fields=["adapter_config"])

        units = self._plan(self._row(2, "RACK-1", "cab-01"))

        self.assertEqual(units[0].disposition, Disposition.NO_OP)
        self.assertEqual(units[0].changes, ())

    def test_an_ignored_source_id_is_excluded(self):
        """`excluded` is reserved for operator policy, which is exactly what an ignore is."""
        IgnoredDevice.objects.create(profile=self.profile, source_id="RACK-1")

        units = self._plan(self._row(2, "RACK-1", "cab-01"))

        self.assertEqual(units[0].disposition, Disposition.EXCLUDED)
        self.assertEqual(units[0].changes, ())

    def test_a_null_like_ignored_source_id_is_still_excluded(self):
        """Cutover parity keeps the rack pass's empty-ID ignore match after null normalization."""
        IgnoredDevice.objects.create(profile=self.profile, source_id="#N/A")

        unit = self._plan(self._row(2, "#N/A", "cab-01"))[0]

        self.assertEqual(unit.disposition, Disposition.EXCLUDED)
        self.assertEqual(unit.diagnostics[0].code, "rack.ignored")

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

    def test_a_rack_the_actor_cannot_view_is_refused_without_add_permission(self):
        """Planning refuses the apparent create when the actor cannot add racks."""
        from dcim.models import Rack

        from netbox_data_import.tests.helpers import user_with_object_permission

        Rack.objects.create(name="cab-01", site=self.site, u_height=42)
        actor = user_with_object_permission("rack-module-actor", [(Rack, ["view"], {"name": "somewhere-else"})])
        scoped = NetBoxReader.for_actor(actor).for_target(site=self.site)

        units = RackModule().plan(self._batch(self._row(2, "RACK-1", "cab-01")), self.profile, CATALOG, scoped)

        self.assertEqual(units[0].disposition, Disposition.INVALID)
        self.assertEqual(units[0].diagnostics[0].code, "rack.add_permission")


class RackModuleApplyTest(RackModuleRowMixin, TestCase):
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
        self.context = ExecutionContext(actor=self.actor, reader=self.reader, profile=self.profile)

    def _only_change(self, *rows):
        units = RackModule().plan(self._batch(*rows), self.profile, CATALOG, self.reader)
        return units[0].changes[0]

    def test_a_create_change_writes_the_rack_the_plan_described(self):
        """The payload is the whole instruction, so applying it needs no second look at the source."""
        from dcim.models import Rack

        change = self._only_change(self._row(2, "RACK-1", "apply-cab-01"))

        rack = RackModule().apply(change, self.context)

        self.assertEqual(rack.name, "apply-cab-01")
        self.assertEqual(rack.site, self.site)
        self.assertEqual(Rack.objects.filter(name="apply-cab-01", site=self.site).count(), 1)

    def test_an_update_change_reconciles_the_stored_rack(self):
        """The planned height replaces the stored one."""
        from dcim.models import Rack

        Rack.objects.create(name="apply-cab-02", site=self.site, u_height=20)
        change = self._only_change(self._row(2, "RACK-2", "apply-cab-02"))
        self.assertEqual(change.operation, "update")

        rack = RackModule().apply(change, self.context)

        rack.refresh_from_db()
        self.assertEqual(rack.u_height, 42)

    def test_a_rack_type_mapping_reaches_the_written_rack(self):
        """The class mapping selects the Rack Type for both planning and execution."""
        from dcim.models import Manufacturer, RackType

        manufacturer = Manufacturer.objects.create(name="Rack Module Vendor", slug="rack-module-vendor")
        rack_type = RackType.objects.create(
            manufacturer=manufacturer,
            model="Distribution",
            slug="distribution",
            u_height=42,
        )
        mapping = self.profile.class_role_mappings.get(source_class="Cabinet")
        mapping.rack_type = rack_type
        mapping.save(update_fields=["rack_type"])

        rack = RackModule().apply(self._only_change(self._row(2, "RACK-TYPE", "typed-cab")), self.context)

        self.assertEqual(rack.rack_type, rack_type)

    def test_a_precondition_that_no_longer_holds_is_refused(self):
        """Section 4.6: the module rechecks its preconditions inside the transaction."""
        from dcim.models import Rack

        rack = Rack.objects.create(name="apply-cab-03", site=self.site, u_height=20)
        change = self._only_change(self._row(2, "RACK-3", "apply-cab-03"))
        rack.u_height = 30
        rack.save(update_fields=["u_height"])

        with self.assertRaises(PreconditionFailed):
            RackModule().apply(change, self.context)

    def test_a_vanished_rack_is_refused_rather_than_recreated(self):
        """An update whose target is gone is stale state, not an invitation to create."""
        from dcim.models import Rack

        rack = Rack.objects.create(name="apply-cab-04", site=self.site, u_height=20)
        change = self._only_change(self._row(2, "RACK-4", "apply-cab-04"))
        rack.delete()

        with self.assertRaises(PreconditionFailed):
            RackModule().apply(change, self.context)

    def test_an_actor_without_the_change_permission_is_refused(self):
        """The module enforces the permission itself; planning visibility is not permission to write."""
        from dcim.models import Rack

        from netbox_data_import.object_permissions import ObjectPermissionDenied
        from netbox_data_import.tests.helpers import user_with_object_permission

        Rack.objects.create(name="apply-cab-05", site=self.site, u_height=20)
        change = self._only_change(self._row(2, "RACK-5", "apply-cab-05"))
        viewer = user_with_object_permission("rack-apply-viewer", [(Rack, ["view"], None)])
        context = ExecutionContext(
            actor=viewer, reader=NetBoxReader.for_actor(viewer).for_target(site=self.site), profile=self.profile
        )

        with self.assertRaises(ObjectPermissionDenied):
            RackModule().apply(change, context)


class RackModuleEdgeTest(RackModuleRowMixin, TestCase):
    """The narrower paths through rack planning and writing."""

    def setUp(self):
        """A site, a location inside it, and a rack-creating profile."""
        from dcim.models import Location, Site

        self.site = Site.objects.create(name="Rack Edge Site", slug="rack-edge-site")
        self.location = Location.objects.create(name="Rack Edge Room", slug="rack-edge-room", site=self.site)
        self.profile = ImportProfile.objects.create(
            name="Rack Edge Profile", adapter_config={"sheet_name": "Data", "update_existing": True}
        )
        ClassRoleMapping.objects.create(profile=self.profile, source_class="Cabinet", creates_rack=True)

    def test_a_row_with_no_source_id_is_identified_by_its_name(self):
        """Not every source carries an identity column, so the name is the fallback key."""
        reader = NetBoxReader.unrestricted().for_target(site=self.site)

        units = RackModule().plan(self._batch(self._row(2, "", "edge-cab-01")), self.profile, CATALOG, reader)

        self.assertEqual(units[0].identity, "rack:name:edge-cab-01")

    def test_an_unreadable_height_falls_back_to_the_default(self):
        """A rack height that is not a number must not fail the whole batch."""
        reader = NetBoxReader.unrestricted().for_target(site=self.site)

        units = RackModule().plan(
            self._batch(self._row(2, "E-1", "edge-cab-02", u_height="tall")), self.profile, CATALOG, reader
        )

        self.assertEqual(units[0].changes[0].payload["u_height"], 42)

    def test_a_reader_with_no_target_refuses_an_invalid_rack(self):
        """Without a target site, planning refuses the invalid rack candidate."""
        from dcim.models import Rack

        Rack.objects.create(name="edge-cab-03", site=self.site, u_height=42)
        units = RackModule().plan(
            self._batch(self._row(2, "E-3", "edge-cab-03")),
            self.profile,
            CATALOG,
            NetBoxReader.unrestricted(),
        )

        self.assertEqual(units[0].disposition, Disposition.INVALID)
        self.assertEqual(units[0].diagnostics[0].code, "rack.validation_failed")

    def test_a_location_bound_reader_only_matches_racks_in_that_location(self):
        """The operator chose a location, so a rack elsewhere in the site is not this rack."""
        from dcim.models import Rack

        Rack.objects.create(name="edge-cab-04", site=self.site, u_height=42)
        reader = NetBoxReader.unrestricted().for_target(site=self.site, location=self.location)

        units = RackModule().plan(self._batch(self._row(2, "E-4", "edge-cab-04")), self.profile, CATALOG, reader)

        self.assertEqual(units[0].changes[0].operation, "create")
        self.assertEqual(units[0].changes[0].payload["location_id"], self.location.pk)

    def test_a_site_only_import_does_not_match_a_rack_in_a_location(self):
        """A blank target location means an unlocated rack, not any rack in the site."""
        from dcim.models import Rack

        Rack.objects.create(name="edge-cab-located", site=self.site, location=self.location, u_height=42)
        reader = NetBoxReader.unrestricted().for_target(site=self.site)

        units = RackModule().plan(
            self._batch(self._row(2, "E-LOCATION", "edge-cab-located")), self.profile, CATALOG, reader
        )

        self.assertEqual(units[0].changes[0].operation, "create")

    def test_a_created_rack_carries_the_serial_and_tenant_the_plan_named(self):
        """Both are optional in the payload, and both have to reach the written row."""
        from tenancy.models import Tenant

        tenant = Tenant.objects.create(name="Rack Edge Tenant", slug="rack-edge-tenant")
        actor = get_user_model().objects.create_superuser(
            username="rack-edge-actor", email="rack-edge@example.invalid", password="testpass"
        )
        reader = NetBoxReader.for_actor(actor).for_target(site=self.site, tenant=tenant)
        units = RackModule().plan(
            self._batch(self._row(2, "E-5", "edge-cab-05", serial="EDGE-SERIAL")),
            self.profile,
            CATALOG,
            reader,
        )

        rack = RackModule().apply(
            units[0].changes[0], ExecutionContext(actor=actor, reader=reader, profile=self.profile)
        )

        self.assertEqual(rack.serial, "EDGE-SERIAL")
        self.assertEqual(rack.tenant, tenant)

    def test_a_serial_difference_alone_makes_the_unit_actionable(self):
        """Height is not the only field the row reconciles."""
        from dcim.models import Rack

        Rack.objects.create(name="edge-cab-06", site=self.site, u_height=42, serial="OLD")
        reader = NetBoxReader.unrestricted().for_target(site=self.site)

        units = RackModule().plan(
            self._batch(self._row(2, "E-6", "edge-cab-06", serial="NEW")), self.profile, CATALOG, reader
        )

        self.assertEqual(units[0].changes[0].operation, "update")
        self.assertEqual(units[0].changes[0].payload["serial"], "NEW")


class RackModuleReviewFindingTest(RackModulePlanTestBase):
    """Two defects a review found: the duplicate-name key, and the tenant the write assigns."""

    def test_two_rows_naming_one_rack_through_device_name_are_both_refused(self):
        """The duplicate check reads the same name the unit takes, so the fallback counts too."""
        units = self._plan(
            {"_row_number": 2, "source_id": "", "device_class": "Cabinet", "device_name": "fallback-rack"},
            {"_row_number": 3, "source_id": "", "device_class": "Cabinet", "device_name": "fallback-rack"},
        )

        self.assertEqual([unit.disposition for unit in units], [Disposition.INVALID] * 2)
        self.assertEqual(units[0].diagnostics[0].code, "rack.duplicate_name")

    def test_a_rack_in_another_tenant_is_work(self):
        """The write assigns the target tenant, so a rack outside it is not a no-op."""
        from dcim.models import Rack
        from tenancy.models import Tenant

        tenant = Tenant.objects.create(name="Rack Tenant", slug="rack-tenant")
        Rack.objects.create(name="tenant-rack", site=self.site, u_height=42)
        self.reader = NetBoxReader.unrestricted().for_target(site=self.site, tenant=tenant)

        units = self._plan(
            {
                "_row_number": 2,
                "source_id": "R-T",
                "device_class": "Cabinet",
                "rack_name": "tenant-rack",
                "u_height": 42,
            }
        )

        self.assertEqual(units[0].disposition, Disposition.ACTIONABLE, units[0].diagnostics)
        self.assertEqual(units[0].changes[0].payload["tenant_id"], tenant.pk)
