# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The target-field catalog, the Source Adapter registry, and the Import Profile cutover."""

import json

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from tenancy.models import ContactRole

from netbox_data_import.adapters import ADAPTERS, FlatWorkbookAdapter, TraceWorkbookAdapter, get_adapter
from netbox_data_import.adapter_forms import FlatWorkbookConfigForm
from netbox_data_import.catalog import CATALOG, POLICY_SECTIONS, OutputKind, TargetModuleKey
from netbox_data_import.forms import ColumnMappingForm, ColumnTransformRuleForm, ImportProfileForm
from netbox_data_import.models import ColumnMapping, ColumnTransformRule, ImportProfile

# The keys the plugin accepted before the cutover. The catalog must accept exactly these.
LEGACY_TARGET_FIELDS = [
    "rack_name",
    "device_name",
    "device_class",
    "face",
    "airflow",
    "u_position",
    "status",
    "make",
    "model",
    "u_height",
    "serial",
    "asset_tag",
    "primary_ip4",
    "primary_ip6",
    "oob_ip",
    "primary_contact",
    "source_id",
    "candidate:contact",
]


class TargetFieldCatalogTest(TestCase):
    """The catalog is the one source of Target Field keys."""

    def test_accepts_exactly_the_keys_accepted_before_the_cutover(self):
        """Every legacy key resolves, and an unknown key does not."""
        for key in LEGACY_TARGET_FIELDS:
            self.assertTrue(CATALOG.is_valid(key), key)
        self.assertFalse(CATALOG.is_valid("not_a_field"))
        self.assertFalse(CATALOG.is_valid(""))

    def test_the_extra_json_family_requires_a_non_empty_name(self):
        """The family validator is the single prefix rule."""
        self.assertTrue(CATALOG.is_valid("extra_json:jira_id"))
        self.assertFalse(CATALOG.is_valid("extra_json:"))
        self.assertFalse(CATALOG.is_valid("extra_json:   "))

    def test_a_family_key_displays_through_the_catalog(self):
        """A family key renders with its name, a static key with its label."""
        self.assertEqual(CATALOG.display("extra_json:jira_id"), "Custom field: jira_id")
        self.assertEqual(CATALOG.display("device_name"), "Device name")

    def test_the_candidate_targets_are_excluded_where_a_capture_group_cannot_supply_them(self):
        """A regex capture group yields text, so it cannot supply a candidate bundle."""
        self.assertFalse(CATALOG.is_valid("candidate:contact", allow_candidates=False))
        keys = [key for key, _label in CATALOG.choices(allow_candidates=False)]
        self.assertNotIn("candidate:contact", keys)
        self.assertIn("device_name", keys)

    def test_fields_are_scoped_by_the_adapter_output_kinds_that_supply_them(self):
        """A trace-only profile supplies none of the flat-row fields."""
        trace = frozenset({OutputKind.SOURCE_TRACE})
        self.assertFalse(CATALOG.is_valid("device_name", output_kinds=trace))
        self.assertTrue(CATALOG.is_valid("device_name", output_kinds=FlatWorkbookAdapter.output_kinds))
        self.assertEqual(CATALOG.choices(output_kinds=trace), [])

    def test_module_ownership_is_derived_from_the_output_kinds(self):
        """A rack source row and a device source row reach their own Target Modules."""
        self.assertEqual(CATALOG.entry("u_position").target_modules, frozenset({TargetModuleKey.DEVICE}))
        self.assertEqual(
            CATALOG.entry("rack_name").target_modules,
            frozenset({TargetModuleKey.DEVICE, TargetModuleKey.RACK}),
        )


class SourceAdapterRegistryTest(TestCase):
    """The registry is the only source of adapter choices."""

    def test_the_registry_declares_both_delivered_adapters(self):
        """The delivery registers the flat and trace workbook adapters."""
        self.assertEqual([a.key for a in ADAPTERS], ["flat_workbook", "trace_workbook"])
        self.assertIs(get_adapter("flat_workbook"), FlatWorkbookAdapter)
        self.assertIsNone(get_adapter("nope"))

    def test_the_form_offers_exactly_the_registered_adapters(self):
        """The profile form derives its choices from the registry."""
        form = ImportProfileForm()
        offered = [key for key, _label in form.fields["source_adapter"].choices if key]
        self.assertEqual(offered, [a.key for a in ADAPTERS])

    def test_the_trace_adapter_declares_no_configuration(self):
        """The trace workbook's sheet names are fixed by the Source Trace model."""
        self.assertEqual(TraceWorkbookAdapter.config_form_class().base_fields, {})


class AdapterConfigValidationTest(TestCase):
    """The selected adapter declares the form that validates ``adapter_config``."""

    def test_it_rejects_an_unknown_key(self):
        """An unknown adapter configuration key is an error, never ignored."""
        with self.assertRaises(ValidationError) as ctx:
            FlatWorkbookConfigForm.validate_config({"sheet_name": "Data", "nope": 1})
        self.assertIn("nope", str(ctx.exception))

    def test_it_rejects_an_invalid_value(self):
        """A value outside the declared choices is an error."""
        with self.assertRaises(ValidationError):
            FlatWorkbookConfigForm.validate_config({"primary_contact_lookup_field": "fax"})

    def test_an_absent_key_takes_the_declared_default(self):
        """A stored configuration never falls back silently to a widget's empty value."""
        config = FlatWorkbookConfigForm.validate_config({})
        self.assertEqual(config["sheet_name"], "Data")
        self.assertTrue(config["update_existing"])
        self.assertIsNone(config["primary_contact_role"])

    def test_a_dangling_contact_role_reference_fails_at_the_form_boundary(self):
        """The natural key is validated where it is entered."""
        with self.assertRaises(ValidationError) as ctx:
            FlatWorkbookConfigForm.validate_config({"primary_contact_role": "No Such Role"})
        self.assertIn("primary_contact_role", str(ctx.exception))

    def test_the_role_is_stored_as_its_natural_key(self):
        """A saved reference carries no instance-local id."""
        ContactRole.objects.create(name="Site Owner", slug="site-owner")
        config = FlatWorkbookConfigForm.validate_config({"primary_contact_role": "Site Owner"})
        self.assertEqual(config["primary_contact_role"], "Site Owner")


class ImportProfileAdapterTest(TestCase):
    """The Source Adapter is required and immutable after creation."""

    def test_the_adapter_cannot_change_after_creation(self):
        """A different source format needs a new Import Profile."""
        profile = ImportProfile.objects.create(name="Immutable", adapter_config={})
        profile.source_adapter = "trace_workbook"
        with self.assertRaises(ValidationError) as ctx:
            profile.full_clean()
        self.assertIn("source_adapter", ctx.exception.message_dict)

    def test_full_clean_normalizes_the_stored_configuration(self):
        """Saving through validation fills every declared key."""
        profile = ImportProfile(name="Normalized", adapter_config={"sheet_name": "Inventory"})
        profile.full_clean()
        self.assertEqual(profile.adapter_config["sheet_name"], "Inventory")
        self.assertTrue(profile.adapter_config["update_existing"])

    def test_settings_fall_back_to_the_declared_defaults(self):
        """A partial stored configuration still reads every declared setting."""
        profile = ImportProfile.objects.create(name="Partial", adapter_config={"sheet_name": "Only"})
        self.assertEqual(profile.adapter_settings.sheet_name, "Only")
        self.assertEqual(profile.adapter_settings.preview_view_mode, "rows")

    def test_reading_a_setting_the_adapter_does_not_declare_fails(self):
        """An unknown setting is an error, not None."""
        profile = ImportProfile.objects.create(name="Unknown Setting", adapter_config={})
        with self.assertRaises(AttributeError):
            profile.adapter_settings.not_a_setting

    def test_a_dangling_role_resolves_to_none(self):
        """A renamed or deleted Contact Role no longer resolves."""
        role = ContactRole.objects.create(name="Temporary", slug="temporary")
        profile = ImportProfile.objects.create(name="Dangling", adapter_config={"primary_contact_role": "Temporary"})
        self.assertEqual(profile.resolve_primary_contact_role(), role)
        role.delete()
        self.assertIsNone(profile.resolve_primary_contact_role())


class PolicyApplicabilityTest(TestCase):
    """Every policy section declares the adapter output it applies to."""

    def test_a_flat_only_section_is_rejected_on_a_trace_profile(self):
        """Validation rejects an inapplicable row."""
        profile = ImportProfile.objects.create(name="Trace", source_adapter="trace_workbook", adapter_config={})
        mapping = ColumnMapping(profile=profile, source_column="Name", target_field="device_name")
        with self.assertRaises(ValidationError):
            mapping.full_clean()

    def test_the_same_section_is_accepted_on_a_flat_profile(self):
        """The section applies wherever its adapter output kind is emitted."""
        profile = ImportProfile.objects.create(name="Flat", adapter_config={})
        mapping = ColumnMapping(profile=profile, source_column="Name", target_field="device_name")
        mapping.full_clean()

    def test_every_declared_section_applies_to_at_least_one_adapter(self):
        """A section that no adapter can supply would be unreachable policy."""
        emitted = frozenset().union(*(a.output_kinds for a in ADAPTERS))
        for section in POLICY_SECTIONS:
            self.assertTrue(section.applies_to(emitted), section.key)


class MappingValidationTest(TestCase):
    """``ColumnMapping`` and ``ColumnTransformRule`` resolve keys through the catalog."""

    @classmethod
    def setUpTestData(cls):
        cls.profile = ImportProfile.objects.create(name="Mapping Profile", adapter_config={})

    def test_column_mapping_accepts_every_legacy_key(self):
        """The cutover accepts exactly the values it accepted before."""
        for key in LEGACY_TARGET_FIELDS:
            ColumnMapping(profile=self.profile, source_column=f"C {key}", target_field=key).full_clean()

    def test_column_mapping_accepts_a_named_extra_json_key(self):
        """A family key with a name is valid."""
        ColumnMapping(profile=self.profile, source_column="Jira", target_field="extra_json:jira_id").full_clean()

    def test_column_mapping_rejects_an_unnamed_extra_json_key(self):
        """A family key without a name is not."""
        mapping = ColumnMapping(profile=self.profile, source_column="Jira", target_field="extra_json:")
        with self.assertRaises(ValidationError) as ctx:
            mapping.full_clean()
        self.assertIn("target_field", ctx.exception.message_dict)

    def test_a_transform_rule_rejects_a_candidate_target(self):
        """The candidate-target exclusion survives the move to the catalog."""
        rule = ColumnTransformRule(
            profile=self.profile,
            source_column="Name",
            pattern=r"^(\w+)$",
            group_1_target="candidate:contact",
        )
        with self.assertRaises(ValidationError) as ctx:
            rule.full_clean()
        self.assertIn("group_1_target", ctx.exception.message_dict)

    def test_a_transform_rule_accepts_a_named_extra_json_key(self):
        """A group target may be a family key."""
        ColumnTransformRule(
            profile=self.profile,
            source_column="Name",
            pattern=r"^(\w+)$",
            group_1_target="extra_json:jira_id",
        ).full_clean()

    def test_the_mapping_forms_offer_only_the_profile_target_fields(self):
        """No form keeps a local target-field list."""
        form = ColumnMappingForm(initial={"profile": self.profile})
        self.assertEqual(
            [key for key, _label in form.fields["target_field"].choices],
            [key for key, _label in CATALOG.choices(output_kinds=self.profile.output_kinds)],
        )
        transform = ColumnTransformRuleForm(initial={"profile": self.profile})
        self.assertNotIn(
            "candidate:contact",
            [key for key, _label in transform.fields["group_1_target"].choices],
        )


class ProfileYamlPortabilityTest(TestCase):
    """A YAML profile export imports into a different NetBox instance."""

    def test_the_export_carries_natural_keys_only(self):
        """No instance-local id appears in the profile block."""
        ContactRole.objects.create(name="Portable Owner", slug="portable-owner")
        profile = ImportProfile.objects.create(
            name="Portable",
            adapter_config={"sheet_name": "Data", "primary_contact_role": "Portable Owner"},
        )
        ColumnMapping.objects.create(profile=profile, source_column="Name", target_field="device_name")

        self.client.force_login(_superuser())
        response = self.client.get(reverse("plugins:netbox_data_import:exportprofile_yaml", args=[profile.pk]))
        self.assertEqual(response.status_code, 200)

        import yaml

        data = yaml.safe_load(response.content)
        self.assertEqual(data["profile"]["source_adapter"], "flat_workbook")
        self.assertEqual(data["profile"]["adapter_config"]["primary_contact_role"], "Portable Owner")
        self.assertNotIn("id", data["profile"])
        self.assertNotIn(profile.pk, list(data["profile"].values()))


class ImportProfileApiTest(TestCase):
    """REST derives its adapter choices and configuration rules from the registry."""

    def setUp(self):
        self.client.force_login(_superuser())

    def _post(self, payload):
        return self.client.post(
            "/api/plugins/data-import/profiles/",
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_ACCEPT="application/json",
        )

    def test_it_rejects_an_unknown_adapter_config_key(self):
        """The adapter form validates the REST payload too."""
        response = self._post({"name": "API Unknown Key", "adapter_config": {"nope": 1}})
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("adapter_config", json.loads(response.content))

    def test_it_rejects_an_unknown_adapter(self):
        """The registry is the only source of adapter choices."""
        response = self._post({"name": "API Unknown Adapter", "source_adapter": "spreadsheet"})
        self.assertEqual(response.status_code, 400, response.content)

    def test_it_rejects_an_adapter_change_after_creation(self):
        """The adapter selection is immutable."""
        profile = ImportProfile.objects.create(name="API Immutable", adapter_config={})
        response = self.client.patch(
            f"/api/plugins/data-import/profiles/{profile.pk}/",
            data=json.dumps({"source_adapter": "trace_workbook"}),
            content_type="application/json",
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(response.status_code, 400, response.content)

    def test_it_rejects_a_target_field_the_adapter_cannot_supply(self):
        """REST resolves the target key through the catalog."""
        profile = ImportProfile.objects.create(name="API Trace", source_adapter="trace_workbook", adapter_config={})
        response = self.client.post(
            "/api/plugins/data-import/column-mappings/",
            data=json.dumps({"profile": profile.pk, "source_column": "Name", "target_field": "device_name"}),
            content_type="application/json",
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(response.status_code, 400, response.content)


class CatalogKeyDriftTest(TestCase):
    """Every surface that names a Target Field must name one the catalog still declares."""

    def test_the_column_suggestion_aliases_point_at_catalog_keys(self):
        """A renamed or removed key must not leave the quick-add suggestions pointing at nothing."""
        from netbox_data_import.views import _ALIAS_TO_CANONICAL

        for alias, key in _ALIAS_TO_CANONICAL.items():
            self.assertTrue(CATALOG.is_valid(key), f"alias '{alias}' suggests unknown target field '{key}'")

    def test_the_syncable_fields_are_catalog_keys(self):
        """The preview sync action writes Target Fields, so its allow-list follows the catalog."""
        from netbox_data_import.views import SyncDeviceFieldView

        for key in SyncDeviceFieldView._ALLOWED_FIELDS:
            self.assertTrue(CATALOG.is_valid(key), key)

    def test_the_writable_review_fields_a_source_can_supply_are_catalog_keys(self):
        """Device field review also compares Device attributes no source column supplies.

        Only the review fields a Target Field can supply are catalog keys. ``tenant``, ``location``,
        ``role``, and ``device_type`` come from the import context, not from a column mapping.
        """
        from netbox_data_import.device_field_review import DeviceFieldReviewer

        context_only = {"tenant", "location", "role", "device_type"}
        for key in DeviceFieldReviewer.reviewable_fields() - context_only:
            self.assertTrue(CATALOG.is_valid(key), key)


def _superuser():
    """Return a saved superuser for view and API tests."""
    from django.contrib.auth import get_user_model

    User = get_user_model()
    return User.objects.create_superuser(username="catalog-admin", password="catalog-admin")
