# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Target Module edge contracts preserved from the replaced fixed passes."""

import datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from netbox_data_import.adapters import SourceBatch
from netbox_data_import.catalog import CATALOG, OutputKind
from netbox_data_import.models import (
    ClassRoleMapping,
    DeviceExistingMatch,
    DeviceImportSource,
    IgnoredFieldDifference,
    ImportProfile,
)
from netbox_data_import.netbox_reader import NetBoxReader
from netbox_data_import.plan import Disposition, PlannedChange
from netbox_data_import.target_modules import (
    DeviceModule,
    ExecutionContext,
    PreconditionFailed,
    RackModule,
    _assign_ips,
    _bind_source,
    _display_value,
)
from netbox_data_import.tests.helpers import user_with_object_permission


class TargetModuleJsonBoundaryTest(SimpleTestCase):
    """Source display values and comparisons stay detached and JSON-safe."""

    def test_display_value_covers_nonfinite_decimal_temporal_and_nested_values(self):
        """Every scalar shape an adapter can emit has deterministic plan display data."""
        self.assertEqual(_display_value(float("inf")), "inf")
        self.assertEqual(_display_value(Decimal("NaN")), "NaN")
        self.assertEqual(_display_value(Decimal(2)), 2)
        self.assertEqual(_display_value(Decimal("2.5")), 2.5)
        self.assertEqual(_display_value(datetime.date(2026, 1, 2)), "2026-01-02")
        self.assertEqual(_display_value({1: (Decimal("3.5"),)}), {"1": [3.5]})
        self.assertEqual(_display_value(object()).startswith("<object object"), True)


class SourceBatchRowContractTest(SimpleTestCase):
    """Every declared output kind states the row type its readers may assume."""

    #: The row type each output kind carries. A new kind must be added here and to SourceBatch.
    ROW_TYPE_BY_KIND = {
        OutputKind.DEVICE_SOURCE_ROW: dict,
        OutputKind.RACK_SOURCE_ROW: dict,
        OutputKind.SOURCE_TRACE: "source_trace",
    }

    def test_every_output_kind_is_covered_by_the_row_contract(self):
        """A new output kind fails here until SourceBatch states the row type it carries."""
        declared = {
            value for name, value in vars(OutputKind).items() if not name.startswith("_") and isinstance(value, str)
        }

        self.assertEqual(declared, set(self.ROW_TYPE_BY_KIND))

    def test_each_output_kind_excludes_a_row_of_the_other_type(self):
        """SourceBatch reports a foreign row instead of passing it to a reader that cannot read it."""
        from netbox_data_import.source_trace import SourceTrace

        for kind, row_type in self.ROW_TYPE_BY_KIND.items():
            with self.subTest(kind=kind):
                # The other family's row, so a swapped expected type fails here. It is never read.
                foreign = {"device": "DEV-A"} if row_type == "source_trace" else SourceTrace.__new__(SourceTrace)
                batch = SourceBatch(output_kinds=frozenset({kind}), rows=(foreign,))

                self.assertEqual(batch.rows, ())
                self.assertEqual([item.code for item in batch.diagnostics], ["source.row_type_unexpected"])


class TargetModuleDatabaseEdgeTest(TestCase):
    """Target mutations reject identities and dependencies that appeared after planning."""

    def setUp(self):
        """Create one complete target and unrestricted execution context."""
        from dcim.models import DeviceRole, DeviceType, Manufacturer, Rack, Site

        self.site = Site.objects.create(name="Target Edge Site", slug="target-edge-site")
        self.rack = Rack.objects.create(name="target-edge-rack", site=self.site, u_height=42)
        self.manufacturer = Manufacturer.objects.create(name="Target Edge Make", slug="target-edge-make")
        self.device_type = DeviceType.objects.create(
            manufacturer=self.manufacturer,
            model="Target Edge Model",
            slug="target-edge-make-target-edge-model",
            u_height=1,
        )
        self.role = DeviceRole.objects.create(name="Target Edge Role", slug="target-edge-role")
        self.profile = ImportProfile.objects.create(
            name="Target Edge Profile",
            adapter_config={"sheet_name": "Data", "update_existing": True},
        )
        ClassRoleMapping.objects.create(
            profile=self.profile,
            source_class="Server",
            role_slug=self.role.slug,
        )
        ClassRoleMapping.objects.create(profile=self.profile, source_class="Cabinet", creates_rack=True)
        self.actor = get_user_model().objects.create_superuser(
            username="target-edge-operator",
            email="target-edge@example.invalid",
            password="testpass",
        )
        self.reader = NetBoxReader.for_actor(self.actor).for_target(site=self.site)
        self.context = ExecutionContext(actor=self.actor, reader=self.reader, profile=self.profile)

    def test_rack_and_device_difference_checks_use_persisted_target_rows(self):
        """Each writable relation and scalar can independently make an update actionable."""
        from dcim.models import Device, Location, Rack, RackType

        location = Location.objects.create(name="Target Edge Room", slug="target-edge-room", site=self.site)
        rack_type = RackType.objects.create(
            manufacturer=self.manufacturer,
            model="Target Edge Rack",
            slug="target-edge-rack-type",
            u_height=42,
        )
        typed_candidate = Rack(site=self.site, u_height=42, rack_type=rack_type)
        located_candidate = Rack(site=self.site, location=location, u_height=42)
        self.assertTrue(RackModule._differs(self.rack, typed_candidate))
        self.assertTrue(RackModule._differs(self.rack, located_candidate))

        device = Device.objects.create(
            name="target-edge-difference-device",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
            rack=self.rack,
            position=6,
            face="front",
            airflow="front-to-rear",
            serial="SERIAL",
            asset_tag="ASSET",
        )
        payload = self._payload(
            rack_id=self.rack.pk,
            u_position=6,
            face="rear",
            airflow="front-to-rear",
            serial="SERIAL",
            asset_tag="ASSET",
        )
        self.assertTrue(DeviceModule._differs(device, payload))
        payload["face"] = "front"
        payload["airflow"] = "rear-to-front"
        self.assertTrue(DeviceModule._differs(device, payload))
        payload["airflow"] = "front-to-rear"
        payload["rack_name"] = self.rack.name
        self.assertTrue(DeviceModule._differs(device, payload))

    def _payload(self, **values):
        """Return the minimum complete Device payload used by apply preconditions."""
        payload = {
            "name": "target-edge-device",
            "device_type_id": self.device_type.pk,
            "role_id": self.role.pk,
            "role_slug": self.role.slug,
            "rack_id": self.rack.pk,
            "rack_name": None,
            "site_id": self.site.pk,
            "location_id": None,
            "tenant_id": None,
            "u_position": None,
            "face": "",
            "status": "active",
            "airflow": "",
            "serial": "",
            "asset_tag": "",
            "source_id": "",
            "extra_columns": {},
            "ip_fields": {},
            "contact": None,
        }
        payload.update(values)
        return payload

    def _change(self, operation, payload):
        """Return one isolated Device module change."""
        return PlannedChange(
            identity=f"device:edge:{operation}",
            target_module=DeviceModule.key,
            operation=operation,
            payload=payload,
            preconditions={"device_id": None},
        )

    def _device_row(self, **values):
        """Return one well-formed Device source row."""
        row = {
            "_row_number": 2,
            "source_id": "TARGET-EDGE-DEVICE",
            "device_class": "Server",
            "device_name": "target-edge-device",
            "rack_name": self.rack.name,
            "make": self.manufacturer.name,
            "model": self.device_type.model,
            "serial": "",
            "asset_tag": "",
        }
        row.update(values)
        return row

    def _plan_device(self, actor, row):
        """Plan one Device row in the supplied actor's target scope."""
        batch = SourceBatch(output_kinds=frozenset({OutputKind.DEVICE_SOURCE_ROW}), rows=(row,))
        reader = NetBoxReader.for_actor(actor).for_target(site=self.site)
        return DeviceModule().plan(batch, self.profile, CATALOG, reader)[0]

    def test_role_dependency_create_rejects_a_row_that_appeared_after_planning(self):
        """A Device Role dependency never becomes a silent no-op."""
        existing_role = self._change(
            "create_role",
            {"name": self.role.name, "slug": self.role.slug, "color": self.role.color},
        )
        with self.assertRaises(PreconditionFailed):
            DeviceModule().apply(existing_role, self.context)

    def test_device_create_rejects_a_role_dependency_that_is_still_absent(self):
        """A Device change cannot run before its planned Device Role dependency."""
        missing_role = self._change(
            "create",
            self._payload(role_id=None, role_slug="missing-target-edge-role"),
        )
        with self.assertRaises(PreconditionFailed):
            DeviceModule().apply(missing_role, self.context)

    def test_create_identity_checks_cover_binding_provenance_asset_and_name(self):
        """Every strong target identity blocks a planned create if it appears late."""
        from dcim.models import Device

        existing = Device.objects.create(
            name="identity-existing",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
            asset_tag="IDENTITY-ASSET",
        )
        DeviceExistingMatch.objects.create(
            profile=self.profile,
            source_id="IDENTITY-LINK",
            netbox_device_id=existing.pk,
            device_name=existing.name,
        )
        DeviceImportSource.objects.create(device=existing, profile=self.profile, source_id="IDENTITY-PROVENANCE")

        cases = (
            ({"source_id": "IDENTITY-LINK"}, "Device link"),
            ({"source_id": "IDENTITY-PROVENANCE"}, "stored source ID"),
            ({"asset_tag": "identity-asset"}, "asset tag"),
            ({"name": existing.name}, "target site"),
        )
        for values, message in cases:
            with self.subTest(values=values):
                conflict = DeviceModule._create_identity_conflict(self._payload(**values), self.profile)
                self.assertIn(message, conflict)

    def test_source_binding_refuses_a_different_device(self):
        """Execution cannot move one source ID to a different Device."""
        from dcim.models import Device

        first = Device.objects.create(
            name="binding-first",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
        )
        second = Device.objects.create(
            name="binding-second",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
        )
        _bind_source(self.profile, "BINDING-EDGE", first, "")

        with self.assertRaises(PreconditionFailed):
            _bind_source(self.profile, "BINDING-EDGE", second, "")

    def test_ip_assignment_updates_a_device_field_for_an_address_it_already_holds(self):
        """An already-held address moves the Device field without creating an IPAddress."""
        from dcim.models import Device, Interface
        from ipam.models import IPAddress

        device = Device.objects.create(
            name="target-edge-held-address",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
        )
        interface = Interface.objects.create(device=device, name="mgmt", type="1000base-t")
        held = IPAddress.objects.create(address="198.18.0.20/32", assigned_object=interface)

        unassigned = _assign_ips(device, {"primary_ip4": "198.18.0.20/32"}, self.actor)

        self.assertEqual(unassigned, {})
        device.refresh_from_db()
        self.assertEqual(device.primary_ip4_id, held.pk)
        self.assertEqual(IPAddress.objects.filter(address="198.18.0.20/32").count(), 1)

    def test_rack_create_refuses_a_late_duplicate(self):
        """A Rack created after planning invalidates the create precondition."""
        from dcim.models import Rack

        batch = SourceBatch(
            output_kinds=frozenset({OutputKind.RACK_SOURCE_ROW}),
            rows=(
                {
                    "_row_number": 2,
                    "source_id": "LATE-RACK",
                    "device_class": "Cabinet",
                    "rack_name": "late-rack",
                    "u_height": 42,
                    "serial": "",
                },
            ),
        )
        change = RackModule().plan(batch, self.profile, CATALOG, self.reader)[0].changes[0]
        Rack.objects.create(name="late-rack", site=self.site, u_height=42)

        with self.assertRaises(PreconditionFailed):
            RackModule().apply(change, self.context)

    def test_rack_planning_refuses_an_update_without_change_permission(self):
        """A visible differing Rack is not actionable without target change permission."""
        from dcim.models import Rack

        viewer = user_with_object_permission("rack-edge-viewer", [(Rack, ["view"], None)])
        scoped = NetBoxReader.for_actor(viewer).for_target(site=self.site)
        batch = SourceBatch(
            output_kinds=frozenset({OutputKind.RACK_SOURCE_ROW}),
            rows=(
                {
                    "_row_number": 2,
                    "source_id": "TARGET-EDGE-RACK",
                    "device_class": "Cabinet",
                    "rack_name": self.rack.name,
                    "u_height": 20,
                    "serial": "",
                },
            ),
        )

        unit = RackModule().plan(batch, self.profile, CATALOG, scoped)[0]

        self.assertEqual(unit.disposition, Disposition.BLOCKED)
        self.assertEqual(unit.diagnostics[0].code, "rack.change_permission")

    def test_rack_create_permission_is_checked_against_the_candidate(self):
        """A constrained add grant must cover the Rack the plan would create."""
        from dcim.models import Rack

        actor = user_with_object_permission(
            "rack-edge-creator",
            [
                (Rack, ["view"], None),
                (Rack, ["add"], {"name": "permitted-rack"}),
            ],
        )
        scoped = NetBoxReader.for_actor(actor).for_target(site=self.site)
        batch = SourceBatch(
            output_kinds=frozenset({OutputKind.RACK_SOURCE_ROW}),
            rows=(
                {
                    "_row_number": 2,
                    "source_id": "PERMITTED-RACK",
                    "device_class": "Cabinet",
                    "rack_name": "permitted-rack",
                    "u_height": 42,
                    "serial": "",
                },
                {
                    "_row_number": 3,
                    "source_id": "SCOPED-RACK",
                    "device_class": "Cabinet",
                    "rack_name": "outside-rack-scope",
                    "u_height": 42,
                    "serial": "",
                },
            ),
        )

        permitted, blocked = RackModule().plan(batch, self.profile, CATALOG, scoped)

        self.assertEqual(permitted.disposition, Disposition.ACTIONABLE)
        self.assertEqual(blocked.disposition, Disposition.BLOCKED)
        self.assertEqual(blocked.diagnostics[0].code, "rack.add_permission")

    def test_rack_update_permission_is_checked_against_the_candidate(self):
        """A constrained change grant must cover the Rack state the plan would write."""
        from dcim.models import Rack

        actor = user_with_object_permission(
            "rack-edge-editor",
            [
                (Rack, ["view"], None),
                (Rack, ["change"], {"u_height": self.rack.u_height}),
            ],
        )
        scoped = NetBoxReader.for_actor(actor).for_target(site=self.site)
        batch = SourceBatch(
            output_kinds=frozenset({OutputKind.RACK_SOURCE_ROW}),
            rows=(
                {
                    "_row_number": 2,
                    "source_id": "SCOPED-RACK-UPDATE",
                    "device_class": "Cabinet",
                    "rack_name": self.rack.name,
                    "u_height": 20,
                    "serial": "",
                },
            ),
        )

        unit = RackModule().plan(batch, self.profile, CATALOG, scoped)[0]

        self.assertEqual(unit.disposition, Disposition.BLOCKED)
        self.assertEqual(unit.diagnostics[0].code, "rack.change_permission")

    def test_missing_device_type_and_role_dependencies_are_explicit_diagnostics(self):
        """Missing Device Type and Device Role dependencies block the unit that needs them."""
        from dcim.models import Device, Rack

        viewer = user_with_object_permission(
            "dependency-edge-viewer",
            [(Rack, ["view"], None), (Device, ["view"], None)],
        )

        unit = self._plan_device(viewer, self._device_row(make="Unseen Make", model="Unseen Model"))
        self.assertEqual(unit.diagnostics[0].code, "device.device_type_missing")

        ClassRoleMapping.objects.create(profile=self.profile, source_class="No Role")
        unit = self._plan_device(viewer, self._device_row(device_class="No Role"))
        self.assertEqual(unit.diagnostics[0].code, "device.role_unconfigured")

        ClassRoleMapping.objects.create(profile=self.profile, source_class="Missing Role", role_slug="missing-role")
        unit = self._plan_device(viewer, self._device_row(device_class="Missing Role"))
        self.assertEqual(unit.diagnostics[0].code, "device.role_permission")

    def test_device_create_and_update_require_their_own_permissions(self):
        """Visibility is not permission to add or reconcile a Device."""
        from dcim.models import Device, Rack

        viewer = user_with_object_permission(
            "device-write-edge-viewer",
            [(Rack, ["view"], None), (Device, ["view"], None)],
        )
        create = self._plan_device(viewer, self._device_row())
        self.assertEqual(create.disposition, Disposition.BLOCKED)
        self.assertEqual(create.diagnostics[0].code, "device.add_permission")

        stored = Device.objects.create(
            name="target-edge-device",
            site=self.site,
            rack=self.rack,
            device_type=self.device_type,
            role=self.role,
            serial="OLD",
        )
        DeviceExistingMatch.objects.create(
            profile=self.profile,
            source_id="TARGET-EDGE-DEVICE",
            netbox_device_id=stored.pk,
            device_name=stored.name,
        )
        update = self._plan_device(viewer, self._device_row(serial="NEW"))
        self.assertEqual(update.disposition, Disposition.BLOCKED)
        self.assertEqual(update.diagnostics[0].code, "device.change_permission")

    def test_device_create_permission_is_checked_against_the_candidate(self):
        """A constrained add grant must cover the Device the plan would create."""
        from dcim.models import Device, Rack

        actor = user_with_object_permission(
            "device-edge-creator",
            [
                (Rack, ["view"], None),
                (Device, ["view"], None),
                (Device, ["add"], {"name": "permitted-device"}),
            ],
        )

        permitted = self._plan_device(actor, self._device_row(device_name="permitted-device"))
        blocked = self._plan_device(actor, self._device_row(device_name="outside-device-scope"))

        self.assertEqual(permitted.disposition, Disposition.ACTIONABLE)
        self.assertEqual(blocked.disposition, Disposition.BLOCKED)
        self.assertEqual(blocked.diagnostics[0].code, "device.add_permission")

    def test_device_validation_precedes_create_and_update_permission_diagnostics(self):
        """An invalid Device needs data repair, not a wider add or change grant."""
        from dcim.models import Device, Rack

        actor = user_with_object_permission(
            "device-edge-invalid-writer",
            [
                (Rack, ["view"], None),
                (Device, ["view"], None),
                (Device, ["add", "change"], {"serial": "permitted"}),
            ],
        )
        invalid_serial = "x" * 101

        create = self._plan_device(
            actor,
            self._device_row(
                source_id="INVALID-DEVICE-CREATE", device_name="invalid-device-create", serial=invalid_serial
            ),
        )

        stored = Device.objects.create(
            name="invalid-device-update",
            site=self.site,
            rack=self.rack,
            device_type=self.device_type,
            role=self.role,
        )
        DeviceExistingMatch.objects.create(
            profile=self.profile,
            source_id="INVALID-DEVICE-UPDATE",
            netbox_device_id=stored.pk,
            device_name=stored.name,
        )
        update = self._plan_device(
            actor,
            self._device_row(
                source_id="INVALID-DEVICE-UPDATE",
                device_name=stored.name,
                serial=invalid_serial,
            ),
        )

        for unit in (create, update):
            self.assertEqual(unit.disposition, Disposition.INVALID)
            self.assertEqual(unit.diagnostics[0].code, "device.validation_failed")

    def test_device_create_permission_is_checked_with_planned_relations(self):
        """Device permission checks see the exact planned Rack and Device Role."""
        from dcim.models import Device, DeviceRole, Rack

        rack_actor = user_with_object_permission(
            "planned-rack-device-creator",
            [
                (Rack, ["view"], None),
                (Rack, ["add"], {"name": "planned-rack"}),
                (Device, ["view"], None),
                (Device, ["add"], {"rack__name": "planned-rack"}),
            ],
        )
        rack_batch = SourceBatch(
            output_kinds=frozenset({OutputKind.RACK_SOURCE_ROW, OutputKind.DEVICE_SOURCE_ROW}),
            rows=(
                {
                    "_row_number": 2,
                    "source_id": "PLANNED-RACK",
                    "device_class": "Cabinet",
                    "rack_name": "planned-rack",
                    "u_height": 42,
                    "serial": "",
                },
                self._device_row(
                    _row_number=3,
                    source_id="PLANNED-RACK-DEVICE",
                    device_name="planned-rack-device",
                    rack_name="planned-rack",
                ),
            ),
        )
        rack_reader = NetBoxReader.for_actor(rack_actor).for_target(site=self.site)

        rack_unit = DeviceModule().plan(rack_batch, self.profile, CATALOG, rack_reader)[0]

        self.assertEqual(rack_unit.disposition, Disposition.ACTIONABLE, rack_unit.diagnostics)

        null_rack_actor = user_with_object_permission(
            "null-rack-device-creator",
            [
                (Rack, ["view"], None),
                (Rack, ["add"], {"name": "planned-rack"}),
                (Device, ["view"], None),
                (Device, ["add"], {"rack__isnull": True}),
            ],
        )
        null_rack_reader = NetBoxReader.for_actor(null_rack_actor).for_target(site=self.site)

        null_rack_unit = DeviceModule().plan(rack_batch, self.profile, CATALOG, null_rack_reader)[0]

        self.assertEqual(null_rack_unit.disposition, Disposition.BLOCKED)
        self.assertEqual(null_rack_unit.diagnostics[0].code, "device.add_permission")

        ClassRoleMapping.objects.create(
            profile=self.profile,
            source_class="Planned Role",
            role_slug="planned-role",
        )
        role_actor = user_with_object_permission(
            "planned-role-device-creator",
            [
                (Rack, ["view"], None),
                (Device, ["view"], None),
                (Device, ["add"], {"role__slug": "planned-role"}),
                (DeviceRole, ["add"], {"slug": "planned-role"}),
            ],
        )

        role_unit = self._plan_device(
            role_actor,
            self._device_row(
                source_id="PLANNED-ROLE-DEVICE",
                device_class="Planned Role",
                device_name="planned-role-device",
            ),
        )

        self.assertEqual(role_unit.disposition, Disposition.ACTIONABLE, role_unit.diagnostics)

        null_role_actor = user_with_object_permission(
            "null-role-device-creator",
            [
                (Rack, ["view"], None),
                (Device, ["view"], None),
                (Device, ["add"], {"role__isnull": True}),
                (DeviceRole, ["add"], {"slug": "planned-role"}),
            ],
        )

        null_role_unit = self._plan_device(
            null_role_actor,
            self._device_row(
                source_id="NULL-ROLE-DEVICE",
                device_class="Planned Role",
                device_name="null-role-device",
            ),
        )

        self.assertEqual(null_role_unit.disposition, Disposition.BLOCKED)
        self.assertEqual(null_role_unit.diagnostics[0].code, "device.add_permission")

    def test_device_permission_uses_an_existing_racks_planned_final_state(self):
        """A Device constraint sees the Rack update that its batch will apply first."""
        from dcim.models import Device, Rack

        actor = user_with_object_permission(
            "planned-rack-update-device-creator",
            [
                (Rack, ["view", "change"], None),
                (Device, ["view"], None),
                (Device, ["add"], {"rack__u_height": self.rack.u_height}),
            ],
        )
        batch = SourceBatch(
            output_kinds=frozenset({OutputKind.RACK_SOURCE_ROW, OutputKind.DEVICE_SOURCE_ROW}),
            rows=(
                {
                    "_row_number": 2,
                    "source_id": "PLANNED-RACK-UPDATE",
                    "device_class": "Cabinet",
                    "rack_name": self.rack.name,
                    "u_height": 20,
                    "serial": "",
                },
                self._device_row(
                    _row_number=3,
                    source_id="PLANNED-RACK-UPDATE-DEVICE",
                    device_name="planned-rack-update-device",
                    rack_name=self.rack.name,
                ),
            ),
        )
        reader = NetBoxReader.for_actor(actor).for_target(site=self.site)

        rack_unit = RackModule().plan(batch, self.profile, CATALOG, reader)[0]
        device_unit = DeviceModule().plan(batch, self.profile, CATALOG, reader)[0]

        self.assertEqual(rack_unit.disposition, Disposition.ACTIONABLE, rack_unit.diagnostics)
        self.assertEqual(device_unit.disposition, Disposition.BLOCKED)
        self.assertEqual(device_unit.diagnostics[0].code, "device.add_permission")

        final_actor = user_with_object_permission(
            "planned-final-rack-device-creator",
            [
                (Rack, ["view", "change"], None),
                (Device, ["view"], None),
                (Device, ["add"], {"rack__u_height": 20}),
            ],
        )
        final_reader = NetBoxReader.for_actor(final_actor).for_target(site=self.site)

        final_unit = DeviceModule().plan(batch, self.profile, CATALOG, final_reader)[0]

        self.assertEqual(final_unit.disposition, Disposition.ACTIONABLE, final_unit.diagnostics)
        self.assertIn(rack_unit.changes[0].identity, final_unit.changes[-1].dependencies)

    def test_device_create_does_not_depend_on_a_converged_typed_rack(self):
        """A Rack Type-normalized Rack has no update for a Device create to depend on."""
        from dcim.models import RackType

        rack_type = RackType.objects.create(
            manufacturer=self.manufacturer,
            model="Converged Rack Type",
            slug="converged-rack-type",
            u_height=20,
        )
        ClassRoleMapping.objects.filter(profile=self.profile, source_class="Cabinet").update(rack_type=rack_type)
        self.rack.rack_type = rack_type
        self.rack.copy_racktype_attrs()
        self.rack.save()
        batch = SourceBatch(
            output_kinds=frozenset({OutputKind.RACK_SOURCE_ROW, OutputKind.DEVICE_SOURCE_ROW}),
            rows=(
                {
                    "_row_number": 2,
                    "source_id": "CONVERGED-TYPED-RACK",
                    "device_class": "Cabinet",
                    "rack_name": self.rack.name,
                    "u_height": 1000,
                    "serial": "",
                },
                self._device_row(
                    _row_number=3,
                    source_id="DEVICE-IN-CONVERGED-TYPED-RACK",
                    device_name="device-in-converged-typed-rack",
                ),
            ),
        )

        device_unit = DeviceModule().plan(batch, self.profile, CATALOG, self.reader)[0]

        self.assertEqual(device_unit.disposition, Disposition.ACTIONABLE, device_unit.diagnostics)
        self.assertNotIn(
            "rack:source:CONVERGED-TYPED-RACK:update",
            device_unit.changes[-1].dependencies,
        )

    def test_device_placement_uses_an_existing_racks_planned_final_height(self):
        """A Device cannot use a unit that the preceding Rack update removes."""
        batch = SourceBatch(
            output_kinds=frozenset({OutputKind.RACK_SOURCE_ROW, OutputKind.DEVICE_SOURCE_ROW}),
            rows=(
                {
                    "_row_number": 2,
                    "source_id": "PLANNED-RACK-HEIGHT",
                    "device_class": "Cabinet",
                    "rack_name": self.rack.name,
                    "u_height": 20,
                    "serial": "",
                },
                self._device_row(
                    _row_number=3,
                    source_id="DEVICE-ABOVE-PLANNED-RACK",
                    device_name="device-above-planned-rack",
                    u_position="30",
                    face="Front",
                ),
            ),
        )

        rack_unit = RackModule().plan(batch, self.profile, CATALOG, self.reader)[0]
        device_unit = DeviceModule().plan(batch, self.profile, CATALOG, self.reader)[0]

        self.assertEqual(rack_unit.disposition, Disposition.ACTIONABLE, rack_unit.diagnostics)
        self.assertEqual(device_unit.disposition, Disposition.INVALID)
        self.assertEqual(device_unit.diagnostics[0].code, "device.rack_position_occupied")

    def test_device_placement_uses_a_new_racks_planned_height(self):
        """A Device cannot use a unit above a Rack that the same batch creates."""
        rack_name = "planned-new-rack-height"
        batch = SourceBatch(
            output_kinds=frozenset({OutputKind.RACK_SOURCE_ROW, OutputKind.DEVICE_SOURCE_ROW}),
            rows=(
                {
                    "_row_number": 2,
                    "source_id": "PLANNED-NEW-RACK-HEIGHT",
                    "device_class": "Cabinet",
                    "rack_name": rack_name,
                    "u_height": 20,
                    "serial": "",
                },
                self._device_row(
                    _row_number=3,
                    source_id="DEVICE-ABOVE-PLANNED-NEW-RACK",
                    device_name="device-above-planned-new-rack",
                    rack_name=rack_name,
                    u_position="30",
                    face="Front",
                ),
            ),
        )

        rack_unit = RackModule().plan(batch, self.profile, CATALOG, self.reader)[0]
        device_unit = DeviceModule().plan(batch, self.profile, CATALOG, self.reader)[0]

        self.assertEqual(rack_unit.disposition, Disposition.ACTIONABLE, rack_unit.diagnostics)
        self.assertEqual(device_unit.disposition, Disposition.INVALID)
        self.assertEqual(device_unit.diagnostics[0].code, "device.rack_position_occupied")

    def test_device_placement_uses_a_new_rack_types_starting_unit(self):
        """A Device cannot use a unit below a new Rack Type's starting unit."""
        from dcim.models import RackType

        rack_type = RackType.objects.create(
            manufacturer=self.manufacturer,
            model="Raised Starting Unit",
            slug="raised-starting-unit",
            u_height=20,
            starting_unit=10,
        )
        ClassRoleMapping.objects.filter(profile=self.profile, source_class="Cabinet").update(rack_type=rack_type)
        rack_name = "planned-new-typed-rack"
        batch = SourceBatch(
            output_kinds=frozenset({OutputKind.RACK_SOURCE_ROW, OutputKind.DEVICE_SOURCE_ROW}),
            rows=(
                {
                    "_row_number": 2,
                    "source_id": "PLANNED-NEW-TYPED-RACK",
                    "device_class": "Cabinet",
                    "rack_name": rack_name,
                    "u_height": 20,
                    "serial": "",
                },
                self._device_row(
                    _row_number=3,
                    source_id="DEVICE-BELOW-PLANNED-RACK-TYPE",
                    device_name="device-below-planned-rack-type",
                    rack_name=rack_name,
                    u_position="5",
                    face="Front",
                ),
            ),
        )

        rack_unit = RackModule().plan(batch, self.profile, CATALOG, self.reader)[0]
        device_unit = DeviceModule().plan(batch, self.profile, CATALOG, self.reader)[0]

        self.assertEqual(rack_unit.disposition, Disposition.ACTIONABLE, rack_unit.diagnostics)
        self.assertEqual(device_unit.disposition, Disposition.INVALID)
        self.assertEqual(device_unit.diagnostics[0].code, "device.rack_position_occupied")

    def test_planned_role_create_permission_is_checked_against_the_candidate(self):
        """A constrained Device Role add grant must cover the role the plan creates."""
        from dcim.models import Device, DeviceRole, Rack

        ClassRoleMapping.objects.create(
            profile=self.profile,
            source_class="Excluded Role",
            role_slug="excluded-role",
        )
        actor = user_with_object_permission(
            "excluded-role-creator",
            [
                (Rack, ["view"], None),
                (Device, ["view", "add"], None),
                (DeviceRole, ["add"], {"slug": "permitted-role"}),
            ],
        )

        unit = self._plan_device(
            actor,
            self._device_row(device_class="Excluded Role"),
        )

        self.assertEqual(unit.disposition, Disposition.BLOCKED)
        self.assertEqual(unit.diagnostics[0].code, "device.role_permission")

    def test_planned_relation_permissions_match_the_state_execution_writes(self):
        """Matching Rack and Device Role constraints stay valid through execution."""
        from dcim.models import Device, DeviceRole, Rack

        ClassRoleMapping.objects.create(
            profile=self.profile,
            source_class="Execution Role",
            role_slug="execution-role",
        )
        actor = user_with_object_permission(
            "planned-relation-executor",
            [
                (Rack, ["view"], None),
                (Rack, ["add"], {"name": "execution-rack"}),
                (Device, ["view"], None),
                (
                    Device,
                    ["add"],
                    {"rack__name": "execution-rack", "role__slug": "execution-role"},
                ),
                (DeviceRole, ["add"], {"slug": "execution-role"}),
            ],
        )
        batch = SourceBatch(
            output_kinds=frozenset({OutputKind.RACK_SOURCE_ROW, OutputKind.DEVICE_SOURCE_ROW}),
            rows=(
                {
                    "_row_number": 2,
                    "source_id": "EXECUTION-RACK",
                    "device_class": "Cabinet",
                    "rack_name": "execution-rack",
                    "u_height": 42,
                    "serial": "",
                },
                self._device_row(
                    _row_number=3,
                    source_id="EXECUTION-DEVICE",
                    device_class="Execution Role",
                    device_name="execution-device",
                    rack_name="execution-rack",
                ),
            ),
        )
        reader = NetBoxReader.for_actor(actor).for_target(site=self.site)
        context = ExecutionContext(actor=actor, reader=reader, profile=self.profile)
        rack_unit = RackModule().plan(batch, self.profile, CATALOG, reader)[0]
        device_unit = DeviceModule().plan(batch, self.profile, CATALOG, reader)[0]

        self.assertEqual(rack_unit.disposition, Disposition.ACTIONABLE, rack_unit.diagnostics)
        self.assertEqual(device_unit.disposition, Disposition.ACTIONABLE, device_unit.diagnostics)

        RackModule().apply(rack_unit.changes[0], context)
        for change in device_unit.changes:
            DeviceModule().apply(change, context)

        device = Device.objects.get(name="execution-device")
        self.assertEqual(device.rack.name, "execution-rack")
        self.assertEqual(device.role.slug, "execution-role")

    def test_create_permissions_include_the_source_id_custom_field(self):
        """Planning checks the same source ID custom field that execution writes."""
        from django.contrib.contenttypes.models import ContentType
        from dcim.models import Device, Rack
        from extras.models import CustomField

        custom_field = CustomField.objects.create(name="edge_source_id", type="text")
        custom_field.object_types.add(
            ContentType.objects.get_for_model(Device),
            ContentType.objects.get_for_model(Rack),
        )
        self.profile.adapter_config = {
            **self.profile.adapter_config,
            "custom_field_name": custom_field.name,
        }
        self.profile.save(update_fields=["adapter_config"])

        rack_actor = user_with_object_permission(
            "source-field-rack-creator",
            [
                (Rack, ["view"], None),
                (Rack, ["add"], {"custom_field_data__edge_source_id": "PERMITTED-RACK-ID"}),
            ],
        )
        rack_reader = NetBoxReader.for_actor(rack_actor).for_target(site=self.site)
        rack_batch = SourceBatch(
            output_kinds=frozenset({OutputKind.RACK_SOURCE_ROW}),
            rows=(
                {
                    "_row_number": 2,
                    "source_id": "PERMITTED-RACK-ID",
                    "device_class": "Cabinet",
                    "rack_name": "source-field-rack",
                    "u_height": 42,
                    "serial": "",
                },
            ),
        )

        rack_unit = RackModule().plan(rack_batch, self.profile, CATALOG, rack_reader)[0]

        self.assertEqual(rack_unit.disposition, Disposition.ACTIONABLE, rack_unit.diagnostics)

        device_actor = user_with_object_permission(
            "source-field-device-creator",
            [
                (Rack, ["view"], None),
                (Device, ["view"], None),
                (Device, ["add"], {"custom_field_data__edge_source_id": "PERMITTED-DEVICE-ID"}),
            ],
        )

        device_unit = self._plan_device(
            device_actor,
            self._device_row(source_id="PERMITTED-DEVICE-ID", device_name="source-field-device"),
        )

        self.assertEqual(device_unit.disposition, Disposition.ACTIONABLE, device_unit.diagnostics)

    def test_a_device_type_slug_collision_is_not_treated_as_an_existing_target(self):
        """A derived slug owned by a different model blocks implicit reuse."""
        from dcim.models import DeviceType

        DeviceType.objects.create(
            manufacturer=self.manufacturer,
            model="Different Stored Model",
            slug="target-edge-make-colliding-model",
            u_height=1,
        )
        type_collision = self._plan_device(
            self.actor,
            self._device_row(model="Colliding Model"),
        )
        self.assertEqual(type_collision.diagnostics[0].code, "device.device_type_slug_collision")

    def test_zero_u_ignored_placement_is_reported_instead_of_written(self):
        """A stale invalid zero-U placement review cannot preserve forbidden rack fields."""
        from dcim.models import Device, DeviceType

        zero_u = DeviceType.objects.create(
            manufacturer=self.manufacturer,
            model="Target Edge Zero U",
            slug="target-edge-make-target-edge-zero-u",
            u_height=0,
        )
        device = Device.objects.create(
            name="zero-u-reviewed-device",
            site=self.site,
            device_type=zero_u,
            role=self.role,
        )
        Device.objects.filter(pk=device.pk).update(rack=self.rack, position=5, face="front")
        DeviceExistingMatch.objects.create(
            profile=self.profile,
            source_id="ZERO-U-REVIEW",
            netbox_device_id=device.pk,
            device_name=device.name,
        )
        IgnoredFieldDifference.objects.create(
            profile=self.profile,
            source_id="ZERO-U-REVIEW",
            netbox_device_id=device.pk,
            target_field="u_position",
            file_snapshot={"canonical": "", "display": ""},
            netbox_snapshot={"canonical": "5", "display": "5"},
        )

        unit = self._plan_device(
            self.actor,
            self._device_row(
                source_id="ZERO-U-REVIEW",
                device_name=device.name,
                model=zero_u.model,
                u_position=None,
                face="front",
            ),
        )

        self.assertEqual(unit.disposition, Disposition.INVALID)
        self.assertEqual(unit.diagnostics[0].code, "device.zero_u_review_conflict")

    def test_precondition_state_without_profile_and_airflow_write_are_supported(self):
        """A module can snapshot a Device alone and apply an explicit airflow value."""
        from dcim.models import Device

        device = Device.objects.create(
            name="airflow-edge-device",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
        )
        state = DeviceModule._precondition_state(device)
        self.assertNotIn("provenance", state)

        change = self._change(
            "create",
            self._payload(name="new-airflow-edge-device", airflow="front-to-rear"),
        )
        created = DeviceModule().apply(change, self.context)
        self.assertEqual(created.airflow, "front-to-rear")
