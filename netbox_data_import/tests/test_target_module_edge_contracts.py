# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Target Module edge contracts preserved from the replaced fixed passes."""

import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from netbox_data_import.adapters import SourceBatch
from netbox_data_import.catalog import OutputKind
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
        self.assertEqual(_display_value(Decimal("2")), 2)
        self.assertEqual(_display_value(Decimal("2.5")), 2.5)
        self.assertEqual(_display_value(datetime.date(2026, 1, 2)), "2026-01-02")
        self.assertEqual(_display_value({1: (Decimal("3.5"),)}), {"1": [3.5]})
        self.assertEqual(_display_value(object()).startswith("<object object"), True)

    def test_rack_and_device_difference_checks_cover_relation_and_text_fields(self):
        """Each writable relation and scalar can independently make an update actionable."""
        rack = SimpleNamespace(u_height=42, rack_type_id=1, location_id=2, tenant_id=None, serial="")
        self.assertTrue(RackModule._differs(rack, 42, "", 3, None))
        self.assertTrue(RackModule._differs(rack, 42, "", 1, SimpleNamespace(pk=3)))

        device = SimpleNamespace(
            device_type_id=1,
            role_id=2,
            rack_id=3,
            location_id=4,
            tenant_id=5,
            position=6,
            status="active",
            face="front",
            airflow="front-to-rear",
            serial="SERIAL",
            asset_tag="ASSET",
        )
        payload = {
            "device_type_id": 1,
            "role_id": 2,
            "rack_name": None,
            "rack_id": 3,
            "location_id": 4,
            "tenant_id": 5,
            "u_position": 6,
            "status": "active",
            "face": "rear",
            "airflow": "front-to-rear",
            "serial": "SERIAL",
            "asset_tag": "ASSET",
            "ip_fields": {},
        }
        self.assertTrue(DeviceModule._differs(device, payload))
        payload["face"] = "front"
        payload["airflow"] = "rear-to-front"
        self.assertTrue(DeviceModule._differs(device, payload))
        payload["airflow"] = "front-to-rear"
        payload["rack_name"] = "rack-a"
        self.assertTrue(DeviceModule._differs(device, payload))


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

    def _payload(self, **values):
        """Return the minimum complete Device payload used by apply preconditions."""
        payload = {
            "name": "target-edge-device",
            "device_type_id": self.device_type.pk,
            "role_id": self.role.pk,
            "manufacturer_slug": self.manufacturer.slug,
            "device_type_slug": self.device_type.slug,
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
        return DeviceModule().plan(batch, self.profile, None, reader)[0]

    def test_dependency_creates_reject_rows_that_appeared_after_planning(self):
        """Manufacturer, DeviceType, and role dependencies never become silent no-ops."""
        manufacturer_change = self._change(
            "create_manufacturer",
            {"name": self.manufacturer.name, "slug": self.manufacturer.slug},
        )
        with self.assertRaises(PreconditionFailed):
            DeviceModule().apply(manufacturer_change, self.context)

        missing_manufacturer = self._change(
            "create_device_type",
            {
                "manufacturer_slug": "missing-target-edge-make",
                "slug": "missing-target-edge-model",
                "model": "Missing Model",
                "u_height": 1,
            },
        )
        with self.assertRaises(PreconditionFailed):
            DeviceModule().apply(missing_manufacturer, self.context)

        existing_type = self._change(
            "create_device_type",
            {
                "manufacturer_slug": self.manufacturer.slug,
                "slug": self.device_type.slug,
                "model": self.device_type.model,
                "u_height": 1,
            },
        )
        with self.assertRaises(PreconditionFailed):
            DeviceModule().apply(existing_type, self.context)

        existing_role = self._change(
            "create_role",
            {"name": self.role.name, "slug": self.role.slug, "color": self.role.color},
        )
        with self.assertRaises(PreconditionFailed):
            DeviceModule().apply(existing_role, self.context)

    def test_device_create_rejects_dependencies_that_are_still_absent(self):
        """A Device change cannot run before its planned DeviceType or role dependency."""
        missing_type = self._change(
            "create",
            self._payload(
                device_type_id=None,
                manufacturer_slug="missing-target-edge-make",
                device_type_slug="missing-target-edge-model",
            ),
        )
        with self.assertRaises(PreconditionFailed):
            DeviceModule().apply(missing_type, self.context)

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
        held = SimpleNamespace(pk=17)
        target = SimpleNamespace(already_held=True, held=held)
        device = SimpleNamespace(primary_ip4_id=None, primary_ip4=None, saved=[])
        device.save = lambda **kwargs: device.saved.append(kwargs["update_fields"])

        with patch("netbox_data_import.target_modules.ip_assignment.resolve", return_value=target):
            unassigned = _assign_ips(device, {"primary_ip4": "198.18.0.20/32"}, self.actor)

        self.assertEqual(unassigned, {})
        self.assertEqual(device.primary_ip4, held)
        self.assertEqual(device.saved, [["primary_ip4"]])

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
        change = RackModule().plan(batch, self.profile, None, self.reader)[0].changes[0]
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

        unit = RackModule().plan(batch, self.profile, None, scoped)[0]

        self.assertEqual(unit.disposition, Disposition.INVALID)
        self.assertEqual(unit.diagnostics[0].code, "rack.change_permission")

    def test_device_dependency_permissions_and_policy_are_explicit_diagnostics(self):
        """Missing dependency policy and add permissions block the unit that needs them."""
        from dcim.models import Device, Rack

        viewer = user_with_object_permission(
            "dependency-edge-viewer",
            [(Rack, ["view"], None), (Device, ["view"], None)],
        )

        unit = self._plan_device(viewer, self._device_row(make="Unseen Make", model="Unseen Model"))
        self.assertEqual(unit.diagnostics[0].code, "device.manufacturer_permission")

        from dcim.models import Manufacturer

        Manufacturer.objects.create(name="Known Empty Make", slug="known-empty-make")
        unit = self._plan_device(viewer, self._device_row(make="Known Empty Make", model="Unseen Model"))
        self.assertEqual(unit.diagnostics[0].code, "device.device_type_permission")

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
        self.assertEqual(update.diagnostics[0].code, "device.change_permission")

    def test_dependency_slug_collisions_are_not_treated_as_existing_targets(self):
        """A derived slug owned by a different make or model blocks implicit reuse."""
        from dcim.models import DeviceType, Manufacturer

        Manufacturer.objects.create(name="Different Make", slug="colliding-make")
        manufacturer_collision = self._plan_device(
            self.actor,
            self._device_row(make="Colliding Make", model="New Model"),
        )
        self.assertEqual(manufacturer_collision.diagnostics[0].code, "device.manufacturer_slug_collision")

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
