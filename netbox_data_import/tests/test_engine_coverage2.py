# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Additional engine coverage tests targeting specific uncovered lines in engine.py."""

from django.contrib.auth import get_user_model
from django.test import TestCase

from netbox_data_import.engine import (
    ImportContext,
    ImportResult,
    _assign_ip_to_device,
    _compute_field_diff,
    _ensure_device_role,
    _ensure_device_type,
    _ensure_manufacturer,
    _preview_device_row,
    _store_source_id,
    reapply_saved_resolutions,
    run_import,
)
from netbox_data_import.models import ClassRoleMapping, ImportProfile, SourceResolution

User = get_user_model()


def _make_profile(name="ECov2Test") -> ImportProfile:
    return ImportProfile.objects.create(
        name=name,
        sheet_name="Data",
        source_id_column="Id",
        update_existing=True,
        create_missing_device_types=True,
    )


class ReapplySavedResolutionsTest(TestCase):
    """Tests for reapply_saved_resolutions — lines 300, 305-313."""

    def setUp(self):
        self.profile = _make_profile("RRSCov2")

    def test_applies_resolved_fields_to_matching_rows(self):
        """reapply_saved_resolutions updates rows for matching source_ids."""
        SourceResolution.objects.create(
            profile=self.profile,
            source_id="SRC-001",
            source_column="Name",
            original_value="old-name",
            resolved_fields={"device_name": "new-name", "device_class": "Server"},
        )
        rows = [
            {"_row_number": 1, "source_id": "SRC-001", "device_name": "old-name", "device_class": "Unknown"},
            {"_row_number": 2, "source_id": "SRC-002", "device_name": "unchanged"},
        ]
        result = reapply_saved_resolutions(rows, self.profile)
        self.assertEqual(result[0]["device_name"], "new-name")
        self.assertEqual(result[0]["device_class"], "Server")
        self.assertEqual(result[1]["device_name"], "unchanged")

    def test_does_not_mutate_original_rows(self):
        """reapply_saved_resolutions makes shallow copies of matching rows."""
        SourceResolution.objects.create(
            profile=self.profile,
            source_id="SRC-MUT",
            source_column="Name",
            original_value="orig",
            resolved_fields={"device_name": "resolved"},
        )
        rows = [{"_row_number": 1, "source_id": "SRC-MUT", "device_name": "orig"}]
        result = reapply_saved_resolutions(rows, self.profile)
        self.assertEqual(result[0]["device_name"], "resolved")
        self.assertEqual(rows[0]["device_name"], "orig")

    def test_returns_all_rows_including_unmatched(self):
        """Result has same length as input even when some rows don't match."""
        SourceResolution.objects.create(
            profile=self.profile,
            source_id="MATCH",
            source_column="Name",
            original_value="x",
            resolved_fields={"device_name": "y"},
        )
        rows = [
            {"_row_number": 1, "source_id": "MATCH", "device_name": "x"},
            {"_row_number": 2, "source_id": "NOMATCH", "device_name": "z"},
        ]
        result = reapply_saved_resolutions(rows, self.profile)
        self.assertEqual(len(result), 2)


class EnsureManufacturerPermDeniedTest(TestCase):
    """Tests for _ensure_manufacturer permission-denied paths — lines 428, 433."""

    def test_dry_run_perm_denied_appends_error_row(self):
        """Dry-run with user lacking add_manufacturer appends perm-denied error row — line 433."""
        from dcim.models import Manufacturer

        profile = _make_profile("EnsureMfgPermDry")
        limited_user = User.objects.create_user("mfg_perm_dry_user2", "mfgd2@t.com", "pw")
        result = ImportResult()
        ctx = ImportContext(
            profile=profile,
            site=None,
            location=None,
            tenant=None,
            dry_run=True,
            result=result,
            user=limited_user,
        )
        row = {"_row_number": 1, "source_id": "1"}
        _ensure_manufacturer("perm-denied-dry-mfg2", "PermDenied Make", set(), ctx, row, Manufacturer)
        self.assertEqual(len(result.rows), 1)
        self.assertEqual(result.rows[0].action, "error")
        self.assertIn("dcim.add_manufacturer", result.rows[0].detail)

    def test_non_dry_run_perm_denied_appends_error_row(self):
        """Execute mode with user lacking add_manufacturer appends perm-denied error row — line 428."""
        from dcim.models import Manufacturer

        profile = _make_profile("EnsureMfgPermExec2")
        limited_user = User.objects.create_user("mfg_perm_exec_user3", "mfgex3@t.com", "pw")
        result = ImportResult()
        ctx = ImportContext(
            profile=profile,
            site=None,
            location=None,
            tenant=None,
            dry_run=False,
            result=result,
            user=limited_user,
        )
        row = {"_row_number": 1, "source_id": "1"}
        _ensure_manufacturer("nondry-mfg-slug2", "NonDry Make", set(), ctx, row, Manufacturer)
        self.assertEqual(len(result.rows), 1)
        self.assertEqual(result.rows[0].action, "error")
        self.assertIn("dcim.add_manufacturer", result.rows[0].detail)


class EnsureDeviceTypePermDeniedTest(TestCase):
    """Tests for _ensure_device_type permission-denied paths — lines 465-469, 479-480."""

    def test_dry_run_perm_denied_appends_error_row(self):
        """Dry-run: user lacking add_devicetype gets perm-denied RowResult — line 480."""
        from dcim.models import DeviceType, Manufacturer

        profile = _make_profile("EnsureDTPermDry2")
        limited_user = User.objects.create_user("dt_perm_dry_user2", "dtp2@t.com", "pw")
        result = ImportResult()
        ctx = ImportContext(
            profile=profile,
            site=None,
            location=None,
            tenant=None,
            dry_run=True,
            result=result,
            user=limited_user,
        )
        row = {"_row_number": 1, "source_id": "1"}
        _ensure_device_type(
            "perm-mfg-dry2",
            "perm-dt-dry2",
            "PermMake2",
            "PermModel2",
            1,
            set(),
            ctx,
            row,
            Manufacturer,
            DeviceType,
        )
        error_rows = [r for r in result.rows if r.action == "error"]
        self.assertGreater(len(error_rows), 0)
        self.assertTrue(any("dcim.add_devicetype" in r.detail for r in error_rows))

    def test_non_dry_run_perm_denied_appends_error_row(self):
        """Execute mode: user lacking add_devicetype gets perm-denied RowResult — lines 465-469."""
        from dcim.models import DeviceType, Manufacturer

        Manufacturer.objects.create(name="ExistMfgForDTPerm2", slug="exist-mfg-dt-perm-cov2")
        profile = _make_profile("EnsureDTPermExec2")
        limited_user = User.objects.create_user("dt_perm_exec_user3", "dtpe3@t.com", "pw")
        result = ImportResult()
        ctx = ImportContext(
            profile=profile,
            site=None,
            location=None,
            tenant=None,
            dry_run=False,
            result=result,
            user=limited_user,
        )
        row = {"_row_number": 1, "source_id": "1"}
        _ensure_device_type(
            "exist-mfg-dt-perm-cov2",
            "perm-dt-exec-nonexist2",
            "ExistMfgForDTPerm2",
            "PermModel3",
            1,
            set(),
            ctx,
            row,
            Manufacturer,
            DeviceType,
        )
        error_rows = [r for r in result.rows if r.action == "error"]
        self.assertGreater(len(error_rows), 0)
        self.assertTrue(any("dcim.add_devicetype" in r.detail for r in error_rows))


class EnsureDeviceRolePermDeniedNonDryRunTest(TestCase):
    """Tests for _ensure_device_role — line 529: user lacks perm in non-dry-run."""

    def test_non_dry_run_user_lacks_perm_role_not_created(self):
        """Non-dry-run: user lacking add_devicerole causes role creation to be skipped."""
        from dcim.models import DeviceRole

        profile = _make_profile("EnsureRolePermNDR2")
        crm = ClassRoleMapping.objects.create(
            profile=profile,
            source_class="TestRole",
            creates_rack=False,
            role_slug="test-role-ndr-perm2",
        )
        limited_user = User.objects.create_user("role_ndr_perm_user2", "rndrp2@t.com", "pw")
        result = ImportResult()
        ctx = ImportContext(
            profile=profile,
            site=None,
            location=None,
            tenant=None,
            dry_run=False,
            result=result,
            user=limited_user,
        )
        _ensure_device_role(crm, set(), ctx, DeviceRole)
        self.assertFalse(DeviceRole.objects.filter(slug="test-role-ndr-perm2").exists())


class ComputeFieldDiffFaceAirflowTest(TestCase):
    """Tests for _compute_field_diff — lines 766, 768, 778-780."""

    def setUp(self):
        from dcim.models import DeviceRole, DeviceType, Manufacturer, Site

        self.site = Site.objects.create(name="DiffFaceSite2", slug="diff-face-site2")
        mfg = Manufacturer.objects.create(name="DiffFaceMfg2", slug="diff-face-mfg2")
        self.dt = DeviceType.objects.create(
            manufacturer=mfg, model="DiffFaceModel2", slug="diff-face-model2", u_height=1
        )
        self.role = DeviceRole.objects.create(name="DiffFaceRole2", slug="diff-face-role2", color="000000")

    def test_face_included_in_diff_when_different(self):
        """_compute_field_diff includes face in diff when it differs — line 766."""
        from dcim.choices import DeviceFaceChoices
        from dcim.models import Device

        device = Device.objects.create(
            name="face-diff-device2",
            site=self.site,
            device_type=self.dt,
            role=self.role,
            face=DeviceFaceChoices.FACE_FRONT,
        )
        diff = _compute_field_diff(
            matched_device=device,
            device_name="face-diff-device2",
            serial="",
            asset_tag="",
            device_face=DeviceFaceChoices.FACE_REAR,
            device_airflow=None,
            device_status="active",
            u_height=1,
            u_position=None,
        )
        self.assertIn("face", diff)
        self.assertEqual(diff["face"]["file"], str(DeviceFaceChoices.FACE_REAR))

    def test_airflow_included_in_diff_when_different(self):
        """_compute_field_diff includes airflow in diff when it differs — line 768."""
        from dcim.choices import DeviceAirflowChoices
        from dcim.models import Device

        device = Device.objects.create(
            name="airflow-diff-device2",
            site=self.site,
            device_type=self.dt,
            role=self.role,
            airflow=DeviceAirflowChoices.AIRFLOW_FRONT_TO_REAR,
        )
        diff = _compute_field_diff(
            matched_device=device,
            device_name="airflow-diff-device2",
            serial="",
            asset_tag="",
            device_face=None,
            device_airflow=DeviceAirflowChoices.AIRFLOW_REAR_TO_FRONT,
            device_status="active",
            u_height=1,
            u_position=None,
        )
        self.assertIn("airflow", diff)
        self.assertNotEqual(diff["airflow"]["file"], diff["airflow"]["netbox"])

    def test_u_height_float_comparison_detects_diff(self):
        """_compute_field_diff detects u_height difference via float comparison — line 778."""
        from dcim.models import Device

        device = Device.objects.create(
            name="uheight-diff-device2",
            site=self.site,
            device_type=self.dt,
            role=self.role,
        )
        diff = _compute_field_diff(
            matched_device=device,
            device_name="uheight-diff-device2",
            serial="",
            asset_tag="",
            device_face=None,
            device_airflow=None,
            device_status="active",
            u_height=2,
            u_position=None,
        )
        self.assertIn("u_height", diff)
        self.assertEqual(diff["u_height"]["netbox"], "1")
        self.assertEqual(diff["u_height"]["file"], "2")

    def test_u_height_none_silences_typeerror(self):
        """_compute_field_diff silences TypeError when u_height is None — line 780."""
        from dcim.models import Device

        device = Device.objects.create(
            name="uheight-none-device2",
            site=self.site,
            device_type=self.dt,
            role=self.role,
        )
        diff = _compute_field_diff(
            matched_device=device,
            device_name="uheight-none-device2",
            serial="",
            asset_tag="",
            device_face=None,
            device_airflow=None,
            device_status="active",
            u_height=None,
            u_position=None,
        )
        self.assertNotIn("u_height", diff)


class PreviewDeviceRowUHeightInvalidTest(TestCase):
    """Tests for _preview_device_row — lines 813-814: invalid u_height falls back to 1."""

    def setUp(self):
        from dcim.models import Site

        self.site = Site.objects.create(name="UHInvalidSite2", slug="uh-invalid-site2")
        self.profile = _make_profile("UHInvalid2")

    def test_invalid_u_height_string_falls_back_to_1(self):
        """u_height='not-a-number' in row results in u_height=1 in result extra_data."""
        from dcim.models import Device, DeviceType, Rack

        row = {
            "_row_number": 1,
            "rack_name": "",
            "u_position": None,
            "u_height": "not-a-number",
        }
        ctx = ImportContext(
            profile=self.profile,
            site=self.site,
            location=None,
            tenant=None,
            dry_run=True,
            result=ImportResult(),
        )
        result_row = _preview_device_row(
            row=row,
            ctx=ctx,
            make="TestMake",
            model="TestModel",
            mfg_slug="test-mfg-uh2",
            dt_slug="test-dt-uh2",
            source_id="uh-1",
            device_name="uh-device-02",
            serial="",
            asset_tag="",
            DeviceType=DeviceType,
            Device=Device,
            Rack=Rack,
        )
        self.assertEqual(result_row.extra_data.get("u_height"), 1)


class PreviewDeviceRowRackLookupTest(TestCase):
    """Tests for _preview_device_row rack lookup paths — lines 838-842."""

    def setUp(self):
        from dcim.models import Site

        self.site = Site.objects.create(name="RackLookupSite2", slug="rack-lookup-site2")
        self.profile = _make_profile("RackLookup2")

    def test_rack_in_cache_uses_cached_value(self):
        """rack_name already in ctx.rack_map skips DB query and sets rack_label — lines 838-839."""
        from dcim.models import Device, DeviceType, Rack

        ctx = ImportContext(
            profile=self.profile,
            site=self.site,
            location=None,
            tenant=None,
            dry_run=True,
            result=ImportResult(),
        )
        ctx.rack_map["Rack-Cache-02"] = "cached-value"
        row = {
            "_row_number": 1,
            "rack_name": "Rack-Cache-02",
            "u_position": None,
        }
        result_row = _preview_device_row(
            row=row,
            ctx=ctx,
            make="CacheMake2",
            model="CacheModel2",
            mfg_slug="cache-mfg2",
            dt_slug="cache-dt2",
            source_id="cache-2",
            device_name="cache-device-02",
            serial="",
            asset_tag="",
            DeviceType=DeviceType,
            Device=Device,
            Rack=Rack,
        )
        self.assertIn("Rack-Cache-02", result_row.detail)

    def test_rack_in_db_not_in_cache_populates_cache(self):
        """rack_name in DB but not in rack_map is found and cache is populated — lines 841-842."""
        from dcim.models import Device, DeviceType, Rack

        Rack.objects.create(name="RackDB-02", site=self.site, u_height=42)
        ctx = ImportContext(
            profile=self.profile,
            site=self.site,
            location=None,
            tenant=None,
            dry_run=True,
            result=ImportResult(),
        )
        row = {
            "_row_number": 1,
            "rack_name": "RackDB-02",
            "u_position": None,
        }
        result_row = _preview_device_row(
            row=row,
            ctx=ctx,
            make="DBMake2",
            model="DBModel2",
            mfg_slug="db-mfg2",
            dt_slug="db-dt2",
            source_id="db-2",
            device_name="db-device-02",
            serial="",
            asset_tag="",
            DeviceType=DeviceType,
            Device=Device,
            Rack=Rack,
        )
        self.assertIn("RackDB-02", ctx.rack_map)
        self.assertIn("RackDB-02", result_row.detail)
        self.assertNotIn("not found", result_row.detail)


class WriteDeviceRowRackFromDBTest(TestCase):
    """Tests for _write_device_row — line 967: rack looked up from DB when not in rack_map."""

    def setUp(self):
        from dcim.models import DeviceRole, DeviceType, Manufacturer, Rack, Site

        self.site = Site.objects.create(name="RackDB2-Site", slug="rack-db2-site")
        # Use slugs matching what slugify(name) produces so engine's get_or_create finds them.
        mfg = Manufacturer.objects.create(name="RackDB2Mfg", slug="rackdb2mfg")
        self.dt = DeviceType.objects.create(
            manufacturer=mfg, model="RackDB2Model", slug="rackdb2mfg-rackdb2model", u_height=1
        )
        self.role = DeviceRole.objects.create(name="RackDB2Role", slug="rack-db2-role", color="000000")
        self.rack = Rack.objects.create(name="RackDB2-01", site=self.site, u_height=42)
        self.profile = _make_profile("RackDB2Profile")
        ClassRoleMapping.objects.create(
            profile=self.profile, source_class="Server", creates_rack=False, role_slug="rack-db2-role"
        )

    def test_device_created_with_rack_from_db(self):
        """Device creation finds rack from DB when rack_map is empty — line 967."""
        from dcim.models import Device

        rows = [
            {
                "_row_number": 1,
                "source_id": "RDB2-001",
                "device_name": "rack-db2-device-01",
                "device_class": "Server",
                "rack_name": "RackDB2-01",
                "make": "RackDB2Mfg",
                "model": "RackDB2Model",
                "u_height": "1",
                "status": "active",
                "u_position": "3",
                "serial": "",
                "asset_tag": "",
            }
        ]
        run_import(rows, self.profile, {"site": self.site}, dry_run=False)
        device = Device.objects.filter(site=self.site, name="rack-db2-device-01").first()
        self.assertIsNotNone(device)
        self.assertEqual(device.rack, self.rack)


class WriteDeviceRowTenantAndIPJsonTest(TestCase):
    """Tests for _write_device_row update path — lines 990, 994-996: tenant + IP JSON."""

    def setUp(self):
        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
        from django.contrib.contenttypes.models import ContentType
        from extras.models import CustomField

        self.site = Site.objects.create(name="TenantIP2-Site", slug="tenant-ip2-site")
        # Use slugs matching what slugify(name) produces so engine's get_or_create finds them.
        mfg = Manufacturer.objects.create(name="TenantIP2Mfg", slug="tenantip2mfg")
        self.dt = DeviceType.objects.create(
            manufacturer=mfg, model="TenantIP2Model", slug="tenantip2mfg-tenantip2model", u_height=1
        )
        self.role = DeviceRole.objects.create(name="TenantIP2Role", slug="tenant-ip2-role", color="000000")
        self.profile = _make_profile("TenantIP2Profile")
        ClassRoleMapping.objects.create(
            profile=self.profile, source_class="Server", creates_rack=False, role_slug="tenant-ip2-role"
        )
        device_ct = ContentType.objects.get_for_model(Device)
        cf, created = CustomField.objects.get_or_create(name="data_import_source", defaults={"type": "json"})
        if created:
            cf.object_types.set([device_ct])

        self.device = Device.objects.create(
            name="tenant-ip2-device-01",
            site=self.site,
            device_type=self.dt,
            role=self.role,
            serial="TIP2-SN",
        )
        self.device.custom_field_data["data_import_source"] = {"source_id": "TIP2-001"}
        self.device.save()

    def test_update_sets_tenant_and_stores_ip_in_json_when_no_interface(self):
        """Update path stores IP in JSON when device has no interface — lines 990, 994-996."""
        from tenancy.models import Tenant

        tenant = Tenant.objects.create(name="TestTenant2", slug="test-tenant-tip2")
        rows = [
            {
                "_row_number": 1,
                "source_id": "TIP2-001",
                "device_name": "tenant-ip2-device-01",
                "device_class": "Server",
                "rack_name": "",
                "make": "TenantIP2Mfg",
                "model": "TenantIP2Model",
                "u_height": "1",
                "status": "active",
                "u_position": "",
                "serial": "TIP2-SN",
                "asset_tag": "",
                "primary_ip4": "10.1.2.4/32",
            }
        ]
        run_import(rows, self.profile, {"site": self.site, "tenant": tenant}, dry_run=False)
        self.device.refresh_from_db()
        self.assertEqual(self.device.tenant, tenant)
        import_data = self.device.cf.get("data_import_source") or {}
        ip_data = import_data.get("_ip") or {}
        self.assertIn("primary_ip4", ip_data)
        self.assertEqual(ip_data["primary_ip4"], "10.1.2.4/32")


class NewDeviceIPStoredInJSONTest(TestCase):
    """Tests for _write_device_row new device path — line 1040: IP stored in JSON."""

    def setUp(self):
        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
        from django.contrib.contenttypes.models import ContentType
        from extras.models import CustomField

        self.site = Site.objects.create(name="NewDevIP2-Site", slug="new-dev-ip2-site")
        # Use slugs matching what slugify(name) produces so engine's get_or_create finds them.
        mfg = Manufacturer.objects.create(name="NewDevIP2Mfg", slug="newdevip2mfg")
        self.dt = DeviceType.objects.create(
            manufacturer=mfg, model="NewDevIP2Model", slug="newdevip2mfg-newdevip2model", u_height=1
        )
        self.role = DeviceRole.objects.create(name="NewDevIP2Role", slug="new-dev-ip2-role", color="000000")
        self.profile = _make_profile("NewDevIP2Profile")
        ClassRoleMapping.objects.create(
            profile=self.profile, source_class="Server", creates_rack=False, role_slug="new-dev-ip2-role"
        )
        device_ct = ContentType.objects.get_for_model(Device)
        cf, created = CustomField.objects.get_or_create(name="data_import_source", defaults={"type": "json"})
        if created:
            cf.object_types.set([device_ct])

    def test_new_device_ip_stored_in_json(self):
        """New device with IP fields stores the IP in data_import_source._ip — line 1040."""
        from dcim.models import Device

        rows = [
            {
                "_row_number": 1,
                "source_id": "NDIP2-001",
                "device_name": "new-dev-ip2-device-01",
                "device_class": "Server",
                "rack_name": "",
                "make": "NewDevIP2Mfg",
                "model": "NewDevIP2Model",
                "u_height": "1",
                "status": "active",
                "u_position": "",
                "serial": "",
                "asset_tag": "",
                "primary_ip4": "192.168.99.2/32",
            }
        ]
        run_import(rows, self.profile, {"site": self.site}, dry_run=False)
        device = Device.objects.filter(site=self.site, name="new-dev-ip2-device-01").first()
        self.assertIsNotNone(device)
        import_data = device.cf.get("data_import_source") or {}
        ip_data = import_data.get("_ip") or {}
        self.assertIn("primary_ip4", ip_data)
        self.assertEqual(ip_data["primary_ip4"], "192.168.99.2/32")


class AssignIPToDeviceTest(TestCase):
    """Tests for _assign_ip_to_device — lines 1064-1093."""

    def setUp(self):
        from dcim.models import DeviceRole, DeviceType, Manufacturer, Site

        self.site = Site.objects.create(name="AssignIP2-Site", slug="assign-ip2-site")
        mfg = Manufacturer.objects.create(name="AssignIP2Mfg", slug="assign-ip2-mfg")
        self.dt = DeviceType.objects.create(
            manufacturer=mfg, model="AssignIP2Model", slug="assign-ip2-model", u_height=1
        )
        self.role = DeviceRole.objects.create(name="AssignIP2Role", slug="assign-ip2-role", color="000000")

    def _make_device(self, name):
        from dcim.models import Device

        return Device.objects.create(name=name, site=self.site, device_type=self.dt, role=self.role)

    def test_invalid_ip_string_returns_false(self):
        """_assign_ip_to_device returns False for an invalid IP string — lines 1067-1070."""
        device = self._make_device("assign-ip2-invalid-01")
        result = _assign_ip_to_device(device, "primary_ip4", "not-an-ip")
        self.assertFalse(result)

    def test_no_interface_returns_false(self):
        """_assign_ip_to_device returns False when device has no interfaces — line 1093."""
        device = self._make_device("assign-ip2-noiface-01")
        result = _assign_ip_to_device(device, "primary_ip4", "192.168.2.1/32")
        self.assertFalse(result)

    def test_interface_with_matching_subnet_creates_and_assigns_ip(self):
        """_assign_ip_to_device creates a new IP and returns True — lines 1073-1090."""
        from dcim.models import Interface
        from ipam.models import IPAddress

        device = self._make_device("assign-ip2-match-01")
        iface = Interface.objects.create(device=device, name="eth0", type="1000base-t")
        IPAddress.objects.create(address="10.50.1.1/24", assigned_object=iface)
        result = _assign_ip_to_device(device, "primary_ip4", "10.50.1.100/32")
        self.assertTrue(result)
        device.refresh_from_db()
        self.assertIsNotNone(device.primary_ip4)

    def test_unassigned_ip_gets_assigned_to_matching_interface(self):
        """IP exists in DB without assigned_object; it is assigned to the interface — lines 1083-1085."""
        from dcim.models import Interface
        from ipam.models import IPAddress

        device = self._make_device("assign-ip2-unassigned-01")
        iface = Interface.objects.create(device=device, name="eth1", type="1000base-t")
        IPAddress.objects.create(address="10.60.1.1/24", assigned_object=iface)
        unassigned_ip = IPAddress.objects.create(address="10.60.1.50/32")
        result = _assign_ip_to_device(device, "primary_ip4", "10.60.1.50/32")
        self.assertTrue(result)
        unassigned_ip.refresh_from_db()
        self.assertEqual(unassigned_ip.assigned_object, iface)


class Pass3UnparseableIPTest(TestCase):
    """Tests for _pass3_process_devices — line 1129: unparseable IP triggers warning."""

    def setUp(self):
        from dcim.models import DeviceRole, DeviceType, Manufacturer, Site

        self.site = Site.objects.create(name="UnparseIP2-Site", slug="unparse-ip2-site")
        mfg = Manufacturer.objects.create(name="UnparseIP2Mfg", slug="unparse-ip2-mfg")
        self.dt = DeviceType.objects.create(
            manufacturer=mfg, model="UnparseIP2Model", slug="unparse-ip2-model", u_height=1
        )
        self.role = DeviceRole.objects.create(name="UnparseIP2Role", slug="unparse-ip2-role", color="000000")
        self.profile = _make_profile("UnparseIP2Profile")
        ClassRoleMapping.objects.create(
            profile=self.profile, source_class="Server", creates_rack=False, role_slug="unparse-ip2-role"
        )

    def test_unparseable_ip_logs_warning_and_row_is_processed(self):
        """Unparseable IP triggers logger.warning and row is still processed — line 1129."""
        rows = [
            {
                "_row_number": 1,
                "source_id": "UIPW2-001",
                "device_name": "unparse-ip2-device-01",
                "device_class": "Server",
                "rack_name": "",
                "make": "UnparseIP2Mfg",
                "model": "UnparseIP2Model",
                "u_height": "1",
                "status": "active",
                "u_position": "",
                "serial": "",
                "asset_tag": "",
                "primary_ip4": "not-a-valid-ip",
            }
        ]
        with self.assertLogs("netbox_data_import.engine", level="WARNING") as cm:
            result = run_import(rows, self.profile, {"site": self.site}, dry_run=True)
        self.assertTrue(any("not-a-valid-ip" in m or "unparseable" in m.lower() for m in cm.output))
        self.assertGreater(len(result.rows), 0)


class StoreSourceIdWithIPDataTest(TestCase):
    """Tests for _store_source_id — line 1339: ip_data causes data['_ip'] to be set."""

    def setUp(self):
        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
        from django.contrib.contenttypes.models import ContentType
        from extras.models import CustomField

        self.site = Site.objects.create(name="StoreIP2Site", slug="store-ip2-site")
        mfg = Manufacturer.objects.create(name="StoreIP2Mfg", slug="store-ip2-mfg")
        self.dt = DeviceType.objects.create(manufacturer=mfg, model="StoreIP2Model", slug="store-ip2-model", u_height=1)
        self.role = DeviceRole.objects.create(name="StoreIP2Role", slug="store-ip2-role", color="000000")
        self.profile = _make_profile("StoreIP2Profile")
        device_ct = ContentType.objects.get_for_model(Device)
        cf, created = CustomField.objects.get_or_create(name="data_import_source", defaults={"type": "json"})
        if created:
            cf.object_types.set([device_ct])

    def test_ip_data_stored_under_underscore_ip_key(self):
        """_store_source_id stores ip_data as _ip in data_import_source — line 1339."""
        from dcim.models import Device

        device = Device.objects.create(name="store-ip2-device-01", site=self.site, device_type=self.dt, role=self.role)
        ip_data = {"primary_ip4": "192.168.5.2/32"}
        _store_source_id(device, self.profile, "SIP2-001", None, ip_data=ip_data)
        device.refresh_from_db()
        import_data = device.cf.get("data_import_source") or {}
        self.assertIn("_ip", import_data)
        self.assertEqual(import_data["_ip"], ip_data)
