# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The target-field catalog, the Source Adapter registry, and the Import Profile cutover."""

import dataclasses
import json
import os
from unittest.mock import patch

from dcim.models import Site
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from tenancy.models import ContactRole

from netbox_data_import.adapters import (
    ADAPTERS,
    FlatWorkbookAdapter,
    TraceWorkbookAdapter,
    UnknownSourceAdapter,
    get_adapter,
    selectable_adapter_choices,
)
from netbox_data_import.adapter_forms import FlatWorkbookConfigForm
from netbox_data_import import catalog as catalog_module
from netbox_data_import.catalog import CATALOG, POLICY_SECTIONS, OutputKind, TargetModuleKey
from netbox_data_import.forms import ColumnMappingForm, ColumnTransformRuleForm, ImportProfileForm
from netbox_data_import.models import ColumnMapping, ColumnTransformRule, ImportProfile
from netbox_data_import.plan import Disposition

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "sample_cans.xlsx")

API = "plugins-api:netbox_data_import-api"


def _api_url(route, *args):
    """Return a plugin REST URL from its router route name, so a path change cannot silently pass."""
    return reverse(f"{API}:{route}", args=args)


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

    def test_the_form_offers_exactly_the_selectable_registered_adapters(self):
        """The profile form derives its choices from the registry, minus the unrunnable adapters."""
        form = ImportProfileForm()
        offered = [key for key, _label in form.fields["source_adapter"].choices if key]
        self.assertEqual(offered, [key for key, _label in selectable_adapter_choices()])

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

    EMPTY_REQUIRED_SETTING_ERROR = (
        "The required adapter setting 'primary_contact_lookup_field' is empty. Edit and save this import profile."
    )

    def test_the_adapter_cannot_change_after_creation(self):
        """A different source format needs a new Import Profile."""
        profile = ImportProfile.objects.create(name="Immutable", adapter_config={})
        profile.source_adapter = "trace_workbook"
        with self.assertRaises(ValidationError) as ctx:
            profile.full_clean()
        self.assertIn("source_adapter", ctx.exception.message_dict)

    def test_save_cannot_bypass_adapter_immutability(self):
        for index, update_fields in enumerate((None, ["source_adapter"], ["adapter_config"])):
            with self.subTest(update_fields=update_fields):
                profile = ImportProfile.objects.create(name=f"Immutable Save {index}", adapter_config={})
                stored_config = profile.adapter_config.copy()
                profile.source_adapter = "trace_workbook"
                profile.adapter_config = {}

                with self.assertRaisesMessage(ValidationError, "cannot change after the profile is created"):
                    if update_fields is None:
                        profile.save()
                    else:
                        profile.save(update_fields=update_fields)

                profile.refresh_from_db()
                self.assertEqual(profile.source_adapter, "flat_workbook")
                self.assertEqual(profile.adapter_config, stored_config)

    def test_full_clean_normalizes_the_stored_configuration(self):
        """Saving through validation fills every declared key."""
        profile = ImportProfile(name="Normalized", adapter_config={"sheet_name": "Inventory"})
        profile.full_clean()
        self.assertEqual(profile.adapter_config["sheet_name"], "Inventory")
        self.assertTrue(profile.adapter_config["update_existing"])

    def test_settings_fall_back_to_the_declared_defaults(self):
        """A partial stored configuration still reads every declared setting."""
        profile = ImportProfile.objects.create(name="Partial")
        ImportProfile.objects.filter(pk=profile.pk).update(adapter_config={"sheet_name": "Only"})
        profile.refresh_from_db()
        self.assertEqual(profile.adapter_settings.sheet_name, "Only")
        self.assertEqual(profile.adapter_settings.preview_view_mode, "rows")

    def test_create_refuses_an_explicitly_blank_required_setting(self):
        with self.assertRaises(ValidationError):
            ImportProfile.objects.create(
                name="Blank Lookup",
                adapter_config={"primary_contact_lookup_field": ""},
            )

        self.assertFalse(ImportProfile.objects.filter(name="Blank Lookup").exists())

    def test_create_refuses_an_explicitly_null_required_setting(self):
        with self.assertRaises(ValidationError):
            ImportProfile.objects.create(
                name="Null Lookup",
                adapter_config={"primary_contact_lookup_field": None},
            )

        self.assertFalse(ImportProfile.objects.filter(name="Null Lookup").exists())

    def test_create_stores_a_partial_configuration_normalized(self):
        profile = ImportProfile.objects.create(
            name="Normalized Create",
            adapter_config={"sheet_name": "Inventory"},
        )
        profile.refresh_from_db()

        self.assertEqual(
            profile.adapter_config,
            {
                "sheet_name": "Inventory",
                "source_id_column": "",
                "custom_field_name": "",
                "update_existing": True,
                "create_missing_device_types": True,
                "capture_extra_data": False,
                "primary_contact_role": None,
                "primary_contact_lookup_field": "email",
                "preview_view_mode": "rows",
            },
        )

    def test_create_preserves_valid_values_and_optional_empty_settings(self):
        profile = ImportProfile.objects.create(
            name="Valid Optional Empties",
            adapter_config={
                "source_id_column": "",
                "custom_field_name": "",
                "update_existing": False,
                "create_missing_device_types": False,
                "capture_extra_data": False,
                "primary_contact_role": None,
                "primary_contact_lookup_field": "name",
            },
        )
        profile.refresh_from_db()

        self.assertEqual(profile.adapter_config["primary_contact_lookup_field"], "name")
        self.assertEqual(profile.adapter_config["source_id_column"], "")
        self.assertEqual(profile.adapter_config["custom_field_name"], "")
        self.assertFalse(profile.adapter_config["update_existing"])
        self.assertFalse(profile.adapter_config["create_missing_device_types"])
        self.assertFalse(profile.adapter_config["capture_extra_data"])
        self.assertIsNone(profile.adapter_config["primary_contact_role"])

    def test_saving_adapter_config_through_update_fields_validates_it(self):
        profile = ImportProfile.objects.create(name="Validate Partial Save")
        stored_config = profile.adapter_config.copy()
        profile.adapter_config = {"primary_contact_lookup_field": ""}

        with self.assertRaises(ValidationError):
            profile.save(update_fields=["adapter_config"])

        profile.refresh_from_db()
        self.assertEqual(profile.adapter_config, stored_config)

    def test_saving_an_unrelated_field_does_not_read_or_rewrite_adapter_config(self):
        profile = ImportProfile.objects.create(name="Unrelated Partial Save")
        ImportProfile.objects.filter(pk=profile.pk).update(adapter_config={"primary_contact_lookup_field": ""})
        profile.refresh_from_db()
        profile.name = "Renamed With Corrupt Config"

        profile.save(update_fields=["name"])

        profile.refresh_from_db()
        self.assertEqual(profile.name, "Renamed With Corrupt Config")
        self.assertEqual(profile.adapter_config, {"primary_contact_lookup_field": ""})

    def test_reading_a_corrupted_required_setting_fails_with_repair_guidance(self):
        for index, invalid_value in enumerate(("", None)):
            with self.subTest(invalid_value=invalid_value):
                profile = ImportProfile.objects.create(name=f"Corrupt Read {index}")
                ImportProfile.objects.filter(pk=profile.pk).update(
                    adapter_config={"primary_contact_lookup_field": invalid_value}
                )
                profile.refresh_from_db()

                with self.assertRaisesMessage(ValidationError, self.EMPTY_REQUIRED_SETTING_ERROR):
                    profile.adapter_settings.primary_contact_lookup_field

    def test_reading_a_non_mapping_configuration_fails_with_repair_guidance(self):
        """A malformed stored JSON value cannot turn into the adapter defaults."""
        for index, invalid_value in enumerate(([], "", 0, False)):
            with self.subTest(invalid_value=invalid_value):
                profile = ImportProfile.objects.create(name=f"Non-mapping Config {index}")
                ImportProfile.objects.filter(pk=profile.pk).update(adapter_config=invalid_value)
                profile.refresh_from_db()

                with self.assertRaisesMessage(ValidationError, "must be a mapping"):
                    profile.adapter_settings.sheet_name

    def test_explicit_optional_empty_values_keep_their_meanings_on_read(self):
        profile = ImportProfile.objects.create(name="Optional Empty Read")
        ImportProfile.objects.filter(pk=profile.pk).update(
            adapter_config={
                "source_id_column": "",
                "update_existing": False,
                "primary_contact_role": None,
                "primary_contact_lookup_field": "name",
            }
        )
        profile.refresh_from_db()

        self.assertEqual(profile.adapter_settings.source_id_column, "")
        self.assertFalse(profile.adapter_settings.update_existing)
        self.assertIsNone(profile.adapter_settings.primary_contact_role)

    def test_adapter_settings_reuses_its_wrapper_and_keeps_in_place_edits_live(self):
        profile = ImportProfile.objects.create(name="Live Adapter Settings")
        settings = profile.adapter_settings

        profile.adapter_config["primary_contact_lookup_field"] = "name"

        self.assertIs(profile.adapter_settings, settings)
        self.assertEqual(settings.primary_contact_lookup_field, "name")

    def test_replacing_the_config_or_adapter_invalidates_adapter_settings(self):
        profile = ImportProfile.objects.create(name="Invalidated Adapter Settings")
        original = profile.adapter_settings
        profile.adapter_config = {"primary_contact_lookup_field": "name"}

        replaced = profile.adapter_settings
        self.assertIsNot(replaced, original)
        self.assertEqual(replaced.primary_contact_lookup_field, "name")

        profile.source_adapter = "trace_workbook"
        trace_settings = profile.adapter_settings
        self.assertIsNot(trace_settings, replaced)
        with self.assertRaises(AttributeError):
            trace_settings.primary_contact_lookup_field

    def test_reading_a_setting_the_adapter_does_not_declare_fails(self):
        """An unknown setting is an error, not None."""
        profile = ImportProfile.objects.create(name="Unknown Setting", adapter_config={})
        with self.assertRaises(AttributeError):
            profile.adapter_settings.not_a_setting

    def test_a_dangling_role_resolves_to_none(self):
        """A renamed or deleted Contact Role no longer resolves.

        The resolution is cached for the profile instance, so a later read reloads the profile.
        """
        role = ContactRole.objects.create(name="Temporary", slug="temporary")
        profile = ImportProfile.objects.create(name="Dangling", adapter_config={"primary_contact_role": "Temporary"})
        self.assertEqual(profile.resolved_primary_contact_role, role)
        role.delete()
        self.assertIsNone(ImportProfile.objects.get(pk=profile.pk).resolved_primary_contact_role)


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
        self.assertIn("RE2", transform.fields["pattern"].help_text)
        self.assertNotIn(
            "candidate:contact",
            [key for key, _label in transform.fields["group_1_target"].choices],
        )


def _scalars(node):
    """Yield every non-boolean scalar in a nested YAML document."""
    if isinstance(node, dict):
        for value in node.values():
            yield from _scalars(value)
    elif isinstance(node, (list, tuple)):
        for value in node:
            yield from _scalars(value)
    elif not isinstance(node, bool):
        yield node


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
        # A nested id breaks a cross-instance import too, so walk the whole block.
        scalars = set(_scalars(data["profile"]))
        self.assertIn("Portable Owner", scalars, "the scan must reach inside adapter_config")
        self.assertNotIn(profile.pk, scalars)
        self.assertNotIn(str(profile.pk), scalars)


class ImportProfileApiTest(TestCase):
    """REST derives its adapter choices and configuration rules from the registry."""

    def setUp(self):
        self.client.force_login(_superuser())

    def _post(self, payload):
        return self.client.post(
            _api_url("importprofile-list"),
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
            _api_url("importprofile-detail", profile.pk),
            data=json.dumps({"source_adapter": "trace_workbook"}),
            content_type="application/json",
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(response.status_code, 400, response.content)

    def test_it_rejects_a_target_field_the_adapter_cannot_supply(self):
        """REST resolves the target key through the catalog."""
        profile = ImportProfile.objects.create(name="API Trace", source_adapter="trace_workbook", adapter_config={})
        response = self.client.post(
            _api_url("columnmapping-list"),
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


class ProfileAndPolicyBoundaryTest(TestCase):
    """Boundary rules for adapter configuration, policy rows, and an unregistered adapter."""

    @classmethod
    def setUpTestData(cls):
        cls.flat = ImportProfile.objects.create(name="Finding Flat", adapter_config={})
        cls.trace = ImportProfile.objects.create(
            name="Finding Trace", source_adapter="trace_workbook", adapter_config={}
        )

    def test_rest_normalizes_an_absent_adapter_config(self):
        """A REST-created profile stores the same document a form-created profile stores."""
        self.client.force_login(_superuser())
        response = self.client.post(
            _api_url("importprofile-list"),
            data=json.dumps({"name": "REST No Config"}),
            content_type="application/json",
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        profile = ImportProfile.objects.get(name="REST No Config")
        self.assertEqual(profile.adapter_config, FlatWorkbookConfigForm.validate_config({}))

    def test_the_resolved_contact_role_is_memoized_per_profile(self):
        """Planning reads the role once per profile instance, not once per row."""
        ContactRole.objects.create(name="Cached Owner", slug="cached-owner")
        profile = ImportProfile.objects.create(
            name="Cached Role", adapter_config={"primary_contact_role": "Cached Owner"}
        )
        with self.assertNumQueries(1):
            first = profile.resolved_primary_contact_role
            second = profile.resolved_primary_contact_role
        self.assertIsNotNone(first)
        self.assertIs(first, second)

    def test_changing_the_configured_role_invalidates_the_memo(self):
        """A mutated profile instance must not keep returning the previous role."""
        ContactRole.objects.create(name="First Owner", slug="first-owner")
        replacement = ContactRole.objects.create(name="Second Owner", slug="second-owner")
        profile = ImportProfile.objects.create(
            name="Remapped Role", adapter_config={"primary_contact_role": "First Owner"}
        )
        self.assertEqual(profile.resolved_primary_contact_role.name, "First Owner")
        profile.adapter_config["primary_contact_role"] = "Second Owner"
        self.assertEqual(profile.resolved_primary_contact_role, replacement)

    def test_a_submitted_profile_is_ignored(self):
        """The profile is the view's to set, so submitted text reaches neither the lookup nor the row."""
        form = ColumnMappingForm(data={"profile": "abc", "source_column": "Name", "target_field": "device_name"})

        self.assertNotIn("profile", form.fields)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIsNone(form.instance.profile_id)

    def test_a_stored_family_target_stays_editable(self):
        """A row saved with an extra_json target must re-save through its form."""
        mapping = ColumnMapping.objects.create(
            profile=self.flat, source_column="Jira", target_field="extra_json:jira_id"
        )
        form = ColumnMappingForm(instance=mapping, initial={"profile": self.flat})
        self.assertIn("extra_json:jira_id", [key for key, _label in form.fields["target_field"].choices])
        bound = ColumnMappingForm(
            instance=mapping,
            data={"profile": self.flat.pk, "source_column": "Jira", "target_field": "extra_json:jira_id"},
        )
        self.assertTrue(bound.is_valid(), bound.errors)

    def test_a_stored_family_group_target_stays_editable(self):
        """The same holds for a transform rule group target."""
        rule = ColumnTransformRule.objects.create(
            profile=self.flat,
            source_column="Name",
            pattern=r"^(\w+)$",
            group_1_target="extra_json:jira_id",
        )
        form = ColumnTransformRuleForm(instance=rule, initial={"profile": self.flat})
        self.assertIn("extra_json:jira_id", [key for key, _label in form.fields["group_1_target"].choices])

    def test_a_stored_target_the_catalog_dropped_is_not_offered(self):
        """An upgrade can retire a target, and clean() rejects it, so the form must not offer it."""
        mapping = ColumnMapping.objects.create(profile=self.flat, source_column="Old", target_field="retired_field")

        form = ColumnMappingForm(instance=mapping, initial={"profile": self.flat})

        self.assertNotIn("retired_field", [key for key, _label in form.fields["target_field"].choices])
        bound = ColumnMappingForm(
            instance=mapping,
            data={"profile": self.flat.pk, "source_column": "Old", "target_field": "retired_field"},
        )
        self.assertFalse(bound.is_valid())

    def test_a_stored_candidate_group_target_is_not_offered(self):
        """A capture group yields text, and clean() refuses a candidate target, so neither may the form."""
        rule = ColumnTransformRule.objects.create(
            profile=self.flat,
            source_column="Name",
            pattern=r"^(\w+)$",
            group_1_target="candidate:contact",
        )

        form = ColumnTransformRuleForm(instance=rule, initial={"profile": self.flat})

        self.assertNotIn("candidate:contact", [key for key, _label in form.fields["group_1_target"].choices])

    def test_a_transform_rule_is_rejected_on_an_inapplicable_profile(self):
        """ColumnTransformRule must run the shared applicability check too."""
        rule = ColumnTransformRule(profile=self.trace, source_column="Name", pattern=r"^(\w+)$")
        with self.assertRaises(ValidationError):
            rule.full_clean()

    def test_the_profile_list_renders_the_adapter_label(self):
        """The list and the detail page must agree on how the adapter reads."""
        from netbox_data_import.tables import ImportProfileTable

        table = ImportProfileTable(ImportProfile.objects.filter(pk=self.flat.pk))
        rendered = table.rows[0].get_cell("source_adapter")
        self.assertIn("Flat workbook", str(rendered))

    def test_an_unregistered_adapter_reports_its_key(self):
        """A profile stamped with an adapter this release no longer registers fails legibly."""
        ImportProfile.objects.filter(pk=self.flat.pk).update(source_adapter="retired_adapter")
        profile = ImportProfile.objects.get(pk=self.flat.pk)
        with self.assertRaisesMessage(UnknownSourceAdapter, "retired_adapter"):
            profile.adapter_settings.preview_view_mode

    def test_the_import_wizard_rejects_a_profile_with_an_unregistered_adapter(self):
        """The stale profile fails at the form boundary instead of reaching the preview."""
        from netbox_data_import.forms import ImportSetupForm

        ImportProfile.objects.filter(pk=self.flat.pk).update(source_adapter="retired_adapter")
        form = ImportSetupForm(data={"profile": self.flat.pk})
        self.assertFalse(form.is_valid())
        self.assertIn("retired_adapter", " ".join(form.errors["profile"]))

    def test_rest_rejects_a_non_mapping_adapter_config(self):
        """A falsy non-mapping is invalid input, never an empty configuration."""
        self.client.force_login(_superuser())
        for invalid in ([], "", 0, False):
            with self.subTest(invalid=invalid):
                response = self.client.post(
                    _api_url("importprofile-list"),
                    data=json.dumps({"name": f"Bad Config {invalid!r}", "adapter_config": invalid}),
                    content_type="application/json",
                    HTTP_ACCEPT="application/json",
                )
                self.assertEqual(response.status_code, 400, response.content)

    def test_rest_rejects_a_policy_row_on_an_inapplicable_profile(self):
        """The REST policy endpoints enforce the same applicability rule the models enforce."""
        self.client.force_login(_superuser())
        response = self.client.post(
            _api_url("classrolemapping-list"),
            data=json.dumps({"profile": self.trace.pk, "source_class": "Server", "role_slug": "server"}),
            content_type="application/json",
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(response.status_code, 400, response.content)

    def test_yaml_import_rejects_an_adapter_change(self):
        """The bulk YAML path must keep the adapter immutable."""
        from netbox_data_import.views import _apply_profile_yaml_data

        with self.assertRaisesMessage(ValueError, "source adapter"):
            _apply_profile_yaml_data({"profile": {"name": self.flat.name, "source_adapter": "trace_workbook"}})

    def test_bulk_csv_import_rejects_an_adapter_change_end_to_end(self):
        """Drive the real bulk-import view: an id row cannot repoint an existing profile."""
        self.client.force_login(_superuser())
        csv_data = f"id,name,source_adapter\n{self.flat.pk},{self.flat.name},trace_workbook\n"
        response = self.client.post(
            reverse("plugins:netbox_data_import:importprofile_bulk_import"),
            {"data": csv_data, "format": "csv", "csv_delimiter": "auto"},
        )
        self.assertEqual(response.status_code, 200, "a rejected import re-renders the form")
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.source_adapter, "flat_workbook")

    def test_csv_import_rejects_an_adapter_change(self):
        """The flat CSV import path must keep the adapter immutable."""
        from netbox_data_import.forms import ImportProfileImportForm

        form = ImportProfileImportForm(
            data={"name": self.flat.name, "source_adapter": "trace_workbook"}, instance=self.flat
        )
        self.assertFalse(form.is_valid())
        self.assertIn("source_adapter", form.errors)


WITHOUT_CABLE_MODULE = tuple(
    dataclasses.replace(module, implemented=False) if module.key == TargetModuleKey.CABLE else module
    for module in catalog_module.TARGET_MODULES
)


@patch.object(catalog_module, "TARGET_MODULES", WITHOUT_CABLE_MODULE)
class AdapterRuntimeSupportTest(TestCase):
    """An adapter is selectable only when this release implements a Target Module that consumes it.

    The release implements every declared module, so the gate is driven by an override that puts
    the Cable module back where T5 found it.
    """

    @classmethod
    def setUpTestData(cls):
        cls.site = Site.objects.create(name="Runtime Site", slug="runtime-site")
        cls.trace = ImportProfile.objects.create(
            name="Runtime Trace", source_adapter="trace_workbook", adapter_config={}
        )

    def test_the_profile_form_offers_only_adapters_this_release_can_run(self):
        """The operator never sees a source format the plugin cannot import."""
        offered = {key for key, _label in ImportProfileForm().fields["source_adapter"].choices if key}
        self.assertEqual(offered, {"flat_workbook"})

    def test_rest_offers_only_adapters_this_release_can_run(self):
        """The REST schema states the choices the create path accepts, not the whole registry."""
        from netbox_data_import.api.serializers import ImportProfileSerializer

        creating = ImportProfileSerializer()
        updating = ImportProfileSerializer(instance=self.trace)

        self.assertEqual(set(creating.fields["source_adapter"].choices), {"flat_workbook"})
        self.assertEqual(
            creating.fields["source_adapter"].choices,
            dict(selectable_adapter_choices()),
            "the create schema must state each adapter's label, not repeat its key",
        )
        self.assertIn(
            "trace_workbook",
            set(updating.fields["source_adapter"].choices),
            "an existing trace profile must still round-trip through REST",
        )

    def test_rest_updates_an_existing_profile_without_a_target_module(self):
        """A client that reads a trace profile can write it back unchanged."""
        self.client.force_login(_superuser())
        response = self.client.patch(
            _api_url("importprofile-detail", self.trace.pk),
            data=json.dumps({"name": "Runtime Trace Renamed", "source_adapter": "trace_workbook"}),
            content_type="application/json",
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.trace.refresh_from_db()
        self.assertEqual(self.trace.name, "Runtime Trace Renamed")
        self.assertEqual(self.trace.source_adapter, "trace_workbook", "the write-back must not drop the adapter")

    def test_the_profile_form_rejects_an_adapter_without_a_target_module(self):
        """Choice filtering alone is presentation, so the form must also reject the value."""
        form = ImportProfileForm(data={"name": "Form Trace", "source_adapter": "trace_workbook"})
        self.assertFalse(form.is_valid())
        self.assertIn("source_adapter", form.errors)

    def test_rest_rejects_creating_a_profile_without_a_target_module(self):
        """The REST create path shares the model rule instead of restating it."""
        self.client.force_login(_superuser())
        response = self.client.post(
            _api_url("importprofile-list"),
            data=json.dumps({"name": "REST Trace", "source_adapter": "trace_workbook"}),
            content_type="application/json",
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(ImportProfile.objects.filter(name="REST Trace").exists())

    def test_bulk_csv_import_rejects_creating_a_profile_without_a_target_module(self):
        """Drive the real bulk-import view: a CSV row cannot create an unrunnable profile."""
        self.client.force_login(_superuser())
        self._bulk_import("CSV Flat", "flat_workbook")
        self.assertTrue(ImportProfile.objects.filter(name="CSV Flat").exists(), "the control row imports")

        response = self._bulk_import("CSV Trace", "trace_workbook")
        self.assertEqual(response.status_code, 200, "a rejected import re-renders the form")
        self.assertFalse(ImportProfile.objects.filter(name="CSV Trace").exists())

    def _bulk_import(self, name, adapter_key):
        """POST one profile row through the real bulk-import view."""
        return self.client.post(
            reverse("plugins:netbox_data_import:importprofile_bulk_import"),
            {"data": f"name,source_adapter\n{name},{adapter_key}\n", "format": "csv", "csv_delimiter": "auto"},
        )

    def test_yaml_import_rejects_creating_a_profile_without_a_target_module(self):
        """The hierarchical YAML path validates through the same model rule."""
        from netbox_data_import.views import _apply_profile_yaml_data

        with self.assertRaises(ValueError):
            _apply_profile_yaml_data({"profile": {"name": "YAML Trace", "source_adapter": "trace_workbook"}})
        self.assertFalse(ImportProfile.objects.filter(name="YAML Trace").exists())

    def test_an_unsaved_profile_with_a_preset_pk_is_still_a_creation(self):
        """A set pk is not proof the profile exists, so the rules must read the persisted row."""
        profile = ImportProfile(pk=999999, name="Preset PK Trace", source_adapter="trace_workbook", adapter_config={})
        with self.assertRaises(ValidationError) as caught:
            profile.full_clean()
        self.assertIn("source_adapter", caught.exception.message_dict)

    def test_an_unsaved_profile_with_a_preset_pk_and_a_runnable_adapter_validates(self):
        """The creation rule must not reject an adapter this release can run."""
        ImportProfile(pk=999998, name="Preset PK Flat", source_adapter="flat_workbook", adapter_config={}).full_clean()

    def test_an_existing_profile_without_a_target_module_still_validates(self):
        """The gate is a creation rule, so it never strands a profile a later release can run."""
        self.trace.full_clean()

    def test_the_wizard_reports_an_unrunnable_adapter_instead_of_crashing(self):
        """A profile the engine cannot consume must fail as a parse error, not an AttributeError."""
        self.client.force_login(_superuser())
        with open(FIXTURE_PATH, "rb") as handle:
            response = self.client.post(
                reverse("plugins:netbox_data_import:import_setup"),
                {"profile": self.trace.pk, "site": self.site.pk, "excel_file": handle},
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("trace_workbook", response.content.decode())

    def test_import_engine_refuses_an_adapter_with_no_target_module(self):
        """The coordinator refuses a source batch no registered Target Module consumes."""
        from netbox_data_import.adapters import UnknownSourceAdapter
        from netbox_data_import.import_engine import ImportEngine
        from netbox_data_import.models import SourceDocument

        actor = _superuser()
        with open(FIXTURE_PATH, "rb") as handle:
            document = SourceDocument.store(profile=self.trace, content=handle.read())
        with self.assertRaisesMessage(UnknownSourceAdapter, "Target Module"):
            ImportEngine.plan(
                self.trace,
                document,
                actor,
                {"site_id": self.site.pk, "location_id": None, "tenant_id": None},
            )


class TraceAdapterIsSelectableTest(TestCase):
    """T5 implements the Cable Target Module, so every surface offers the trace adapter."""

    def test_every_declared_target_module_is_implemented(self):
        """Nothing in this release is declared and unbuilt, so no adapter is held back."""
        self.assertEqual([module.key for module in catalog_module.TARGET_MODULES if not module.implemented], [])

    def test_the_profile_form_offers_the_trace_adapter(self):
        offered = {key for key, _label in ImportProfileForm().fields["source_adapter"].choices if key}

        self.assertEqual(offered, {"flat_workbook", "trace_workbook"})

    def test_the_profile_form_creates_a_trace_profile(self):
        form = ImportProfileForm(data={"name": "Form Trace", "source_adapter": "trace_workbook"})

        self.assertTrue(form.is_valid(), form.errors)

    def test_rest_creates_a_trace_profile(self):
        self.client.force_login(_superuser())

        response = self.client.post(
            _api_url("importprofile-list"),
            data=json.dumps({"name": "REST Trace", "source_adapter": "trace_workbook"}),
            content_type="application/json",
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 201, response.content)
        self.assertTrue(ImportProfile.objects.filter(name="REST Trace").exists())

    def test_the_cable_target_module_has_a_registered_runtime(self):
        """The coordinator resolves the declared module to a runtime that consumes Source Traces."""
        from netbox_data_import import target_modules

        runtime = target_modules.runtime_for(TargetModuleKey.CABLE)

        self.assertIsNotNone(runtime)
        self.assertEqual(runtime.consumes, frozenset({OutputKind.SOURCE_TRACE}))


class StaleAdapterRuntimeGuardTest(TestCase):
    """A profile whose adapter this release no longer registers must never reach the runtime."""

    def setUp(self):
        self.site = Site.objects.create(name="Stale Site", slug="stale-site")
        from netbox_data_import.tests.test_views import _make_profile

        self.profile = _make_profile("Stale Runtime")
        self.user = _superuser()
        self.client.force_login(self.user)

    def _start_a_preview(self):
        """Post the setup form so the session carries a parsed preview."""
        with open(FIXTURE_PATH, "rb") as handle:
            response = self.client.post(
                reverse("plugins:netbox_data_import:import_setup"),
                {"profile": self.profile.pk, "site": self.site.pk, "excel_file": handle},
            )
        self.assertIn(response.status_code, (200, 302), response.content[:400])

    def _retire_the_adapter(self):
        """Drop the adapter out of the registry the way an upgrade would."""
        ImportProfile.objects.filter(pk=self.profile.pk).update(source_adapter="retired_adapter")

    def test_the_preview_reports_the_stale_adapter_instead_of_raising(self):
        """The session outlives a restart, so the preview can load a profile the release dropped."""
        self._start_a_preview()
        self._retire_the_adapter()
        response = self.client.get(reverse("plugins:netbox_data_import:import_preview"), follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("retired_adapter", response.content.decode())

    def test_the_run_view_refuses_a_stale_adapter(self):
        """A queued run would otherwise fail inside the worker with no operator feedback."""
        self._start_a_preview()
        self._retire_the_adapter()
        response = self.client.post(reverse("plugins:netbox_data_import:import_run"), follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("retired_adapter", response.content.decode())

    def _syncable_row_number(self):
        """Return a preview row number `SyncSingleRowView` accepts."""
        from netbox_data_import.review_workspace import ReviewWorkspace

        workspace = ReviewWorkspace.from_dict(self.client.session["import_plan"])
        for unit in workspace.units:
            if unit.row_number is None:
                continue
            if unit.action == "create" and unit.object_type in ("device", "rack"):
                return unit.row_number
        self.fail("the sample workbook must offer one syncable create row")

    def test_the_single_row_sync_refuses_a_stale_adapter(self):
        """It runs the engine straight from the session, so it meets the retired adapter too."""
        self._start_a_preview()
        row_number = self._syncable_row_number()
        self._retire_the_adapter()
        response = self.client.post(
            reverse("plugins:netbox_data_import:sync_single_row"),
            {
                "row_number": str(row_number),
                "preview_revision": self.client.session["import_preview_revision"],
            },
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("retired_adapter", response.json()["error"])

    def test_the_engine_refuses_a_stale_adapter_at_its_own_boundary(self):
        """The coordinator checks the adapter before it interprets the stored source."""
        from netbox_data_import.adapters import UnknownSourceAdapter
        from netbox_data_import.import_engine import ImportEngine
        from netbox_data_import.models import SourceDocument

        self._start_a_preview()
        session = self.client.session
        self._retire_the_adapter()
        profile = ImportProfile.objects.get(pk=self.profile.pk)
        document = SourceDocument.objects.get(pk=session["import_context"]["source_document_id"])
        with self.assertRaisesMessage(UnknownSourceAdapter, "retired_adapter"):
            ImportEngine.plan(
                profile,
                document,
                self.user,
                {"site_id": self.site.pk, "location_id": None, "tenant_id": None},
            )

    def test_the_job_runner_fails_the_job_on_a_stale_adapter(self):
        """A job queued before the upgrade still reaches the worker, so it needs its own guard."""
        import uuid

        from core.exceptions import JobFailed
        from core.models import Job

        from netbox_data_import.jobs import ImportJobRunner

        self._start_a_preview()
        self._retire_the_adapter()
        session = self.client.session
        plan = session["import_plan"]
        selection = [unit["identity"] for unit in plan["units"] if unit["disposition"] == Disposition.ACTIONABLE]
        job = Job.objects.create(
            name="Data Import",
            user=self.user,
            status="pending",
            job_id=uuid.uuid4(),
            queue_name="default",
            data={"job_type": ImportJobRunner.job_type},
        )
        with self.assertRaises(JobFailed):
            ImportJobRunner(job).run(
                self.profile.pk,
                session["import_context"]["source_document_id"],
                plan,
                selection,
                "stale-adapter-test",
            )
        job.refresh_from_db()
        self.assertEqual(job.data["phase"], "failed")
        self.assertIn("retired_adapter", job.data["message"])


class RequiredTargetKeyRestTest(TestCase):
    """The catalog check runs on an empty value only where the target key is required."""

    def setUp(self):
        self.profile = ImportProfile.objects.create(name="Required Target", adapter_config={})
        self.client.force_login(_superuser())

    def _post(self, route, payload):
        return self.client.post(
            _api_url(route),
            data=json.dumps({"profile": self.profile.pk, **payload}),
            content_type="application/json",
            HTTP_ACCEPT="application/json",
        )

    def test_rest_rejects_a_column_mapping_without_a_target_field(self):
        """A column mapping carries no meaning without a target, so an empty value is invalid."""
        response = self._post("columnmapping-list", {"source_column": "Name", "target_field": ""})
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("target_field", response.json())

    def test_rest_accepts_a_transform_rule_with_only_the_first_group_target(self):
        """The second group target is optional, so an empty value must skip the catalog check."""
        response = self._post(
            "columntransformrule-list",
            {
                "source_column": "Name",
                "pattern": r"^(\S+)$",
                "group_1_target": "device_name",
                "group_2_target": "",
            },
        )
        self.assertEqual(response.status_code, 201, response.content)


class StaleAdapterContactResolutionTest(TestCase):
    """Saving a Contact resolution reads an adapter setting of its own, outside the engine."""

    def _workbook(self):
        """Return an in-memory workbook whose rows carry Contact candidate columns."""
        import io

        import openpyxl

        book = openpyxl.Workbook()
        sheet = book.active
        sheet.title = "Data"
        sheet.append(["Id", "Rack", "Name", "Class", "UHeight", "Owner"])
        sheet.append(["s-1", "RackS", "RackS", "Cabinet", "42", None])
        sheet.append(["s-2", "RackS", "stale-server-01", "Server", "1", "ada@example.invalid"])
        buffer = io.BytesIO()
        book.save(buffer)
        buffer.seek(0)
        return buffer

    def setUp(self):
        from netbox_data_import.tests.test_views import _make_profile

        self.site = Site.objects.create(name="Stale Contact Site", slug="stale-contact-site")
        self.profile = _make_profile("Stale Contact")
        ColumnMapping.objects.create(profile=self.profile, source_column="Owner", target_field="candidate:contact")
        self.client.force_login(_superuser())

        workbook = self._workbook()
        workbook.name = "stale-contact.xlsx"
        response = self.client.post(
            reverse("plugins:netbox_data_import:import_setup"),
            {"profile": self.profile.pk, "site": self.site.pk, "excel_file": workbook},
        )
        self.assertEqual(response.status_code, 302)

    def test_it_reports_the_stale_adapter_instead_of_raising(self):
        """`primary_contact_lookup_field` is read straight off the profile, so it needs its own guard."""
        ImportProfile.objects.filter(pk=self.profile.pk).update(source_adapter="retired_adapter")
        response = self.client.post(
            reverse("plugins:netbox_data_import:save_resolution"),
            {
                "profile_id": self.profile.pk,
                "source_id": "s-2",
                "source_column": "candidate:contact",
                "resolved_fields": "{}",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("retired_adapter", response.content.decode())
