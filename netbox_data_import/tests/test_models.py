# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Unit tests for the ImportProfile, ColumnMapping, ClassRoleMapping, and DeviceTypeMapping models."""

from django.test import TestCase

from netbox_data_import.models import (
    ClassRoleMapping,
    ColumnMapping,
    ColumnTransformRule,
    DeviceTypeMapping,
    ImportJob,
    ImportProfile,
)


class ImportProfileModelTest(TestCase):
    """Tests for the ImportProfile model."""

    def test_create_minimal_profile(self):
        """A profile with only a name can be created."""
        profile = ImportProfile.objects.create(name="Test Profile")
        self.assertEqual(str(profile), "Test Profile")
        self.assertEqual(profile.sheet_name, "Data")
        self.assertTrue(profile.update_existing)
        self.assertTrue(profile.create_missing_device_types)

    def test_profile_name_unique(self):
        """Two profiles with the same name cannot coexist."""
        from django.db import IntegrityError

        ImportProfile.objects.create(name="Unique Profile")
        with self.assertRaises(IntegrityError):
            ImportProfile.objects.create(name="Unique Profile")

    def test_get_absolute_url(self):
        """get_absolute_url returns the expected plugin URL."""
        profile = ImportProfile.objects.create(name="URL Profile")
        url = profile.get_absolute_url()
        self.assertIn("/plugins/data-import/profiles/", url)
        self.assertIn(str(profile.pk), url)

    def test_get_absolute_url_without_profile(self):
        """get_absolute_url returns the list URL when profile_id is None."""
        job = ImportJob.__new__(ImportJob)
        job.profile_id = None
        url = job.get_absolute_url()
        self.assertIn("/plugins/data-import/profiles/", url)


class ColumnTransformRuleModelTest(TestCase):
    """Tests for ColumnTransformRule.clean() validation."""

    def setUp(self):
        self.profile = ImportProfile.objects.create(name="CTR Profile")

    def test_clean_invalid_regex(self):
        """clean() raises ValidationError for an invalid regex pattern."""
        from django.core.exceptions import ValidationError

        rule = ColumnTransformRule(
            profile=self.profile,
            source_column="Name",
            pattern="[invalid",
            group_1_target="device_name",
        )
        with self.assertRaises(ValidationError) as cm:
            rule.clean()
        self.assertIn("pattern", cm.exception.message_dict)

    def test_clean_too_few_groups_for_group2_target(self):
        """clean() raises ValidationError when group_2_target needs 2 groups but pattern has none."""
        from django.core.exceptions import ValidationError

        rule = ColumnTransformRule(
            profile=self.profile,
            source_column="Name",
            pattern="nogroups",
            group_1_target="device_name",
            group_2_target="rack_name",
        )
        with self.assertRaises(ValidationError) as cm:
            rule.clean()
        self.assertIn("pattern", cm.exception.message_dict)
        self.assertIn("2", cm.exception.message_dict["pattern"][0])


class ColumnMappingModelTest(TestCase):
    """Tests for the ColumnMapping model."""

    def setUp(self):
        self.profile = ImportProfile.objects.create(name="CM Profile")

    def test_create_column_mapping(self):
        """A column mapping can be created and stringified."""
        cm = ColumnMapping.objects.create(
            profile=self.profile,
            source_column="Name",
            target_field="device_name",
        )
        self.assertIn("Name", str(cm))
        self.assertIn("Device name", str(cm))  # get_target_field_display() is used in __str__

    def test_unique_target_field_per_profile(self):
        """Two column mappings for the same profile+target_field are rejected."""
        from django.db import IntegrityError

        ColumnMapping.objects.create(
            profile=self.profile,
            source_column="Name",
            target_field="device_name",
        )
        with self.assertRaises(IntegrityError):
            ColumnMapping.objects.create(
                profile=self.profile,
                source_column="DeviceName",
                target_field="device_name",
            )

    def test_cascade_delete_with_profile(self):
        """Deleting a profile removes its column mappings."""
        ColumnMapping.objects.create(
            profile=self.profile,
            source_column="Name",
            target_field="device_name",
        )
        pk = self.profile.pk
        self.profile.delete()
        self.assertEqual(ColumnMapping.objects.filter(profile_id=pk).count(), 0)


class ClassRoleMappingModelTest(TestCase):
    """Tests for the ClassRoleMapping model."""

    def setUp(self):
        self.profile = ImportProfile.objects.create(name="CRM Profile")

    def test_creates_rack_flag(self):
        """creates_rack=True means the row maps to a rack, not a device."""
        crm = ClassRoleMapping.objects.create(
            profile=self.profile,
            source_class="Cabinet",
            creates_rack=True,
        )
        self.assertTrue(crm.creates_rack)

    def test_unique_source_class_per_profile(self):
        """Two class mappings for the same profile+source_class are rejected."""
        from django.db import IntegrityError

        ClassRoleMapping.objects.create(
            profile=self.profile,
            source_class="Server",
            role_slug="server",
        )
        with self.assertRaises(IntegrityError):
            ClassRoleMapping.objects.create(
                profile=self.profile,
                source_class="Server",
                role_slug="server-duplicate",
            )


class DeviceTypeMappingModelTest(TestCase):
    """Tests for the DeviceTypeMapping model."""

    def setUp(self):
        self.profile = ImportProfile.objects.create(name="DTM Profile")

    def test_create_device_type_mapping(self):
        """A device type mapping stores make/model and slug overrides."""
        dtm = DeviceTypeMapping.objects.create(
            profile=self.profile,
            source_make="Dell",
            source_model="R660",
            netbox_manufacturer_slug="dell",
            netbox_device_type_slug="dell-poweredge-r660",
        )
        self.assertEqual(dtm.source_make, "Dell")
        self.assertEqual(dtm.netbox_device_type_slug, "dell-poweredge-r660")

    def test_unique_make_model_per_profile(self):
        """Duplicate (profile, make, model) combinations are rejected."""
        from django.db import IntegrityError

        DeviceTypeMapping.objects.create(
            profile=self.profile,
            source_make="Cisco",
            source_model="C9300",
            netbox_manufacturer_slug="cisco",
            netbox_device_type_slug="cisco-c9300",
        )
        with self.assertRaises(IntegrityError):
            DeviceTypeMapping.objects.create(
                profile=self.profile,
                source_make="Cisco",
                source_model="C9300",
                netbox_manufacturer_slug="cisco",
                netbox_device_type_slug="cisco-c9300-dup",
            )
