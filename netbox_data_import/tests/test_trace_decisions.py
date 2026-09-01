# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Trace decision persistence and CableClass policy tests."""

import ast
import pathlib

from core.models import ObjectType
from dcim.choices import CableProfileChoices, CableTypeChoices
from dcim.models import Cable, Device, Interface
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse

from netbox_data_import.adapters import UnknownSourceAdapter
from netbox_data_import import field_keys
from netbox_data_import.catalog import OutputKind, policy_section
from netbox_data_import.field_keys import (
    MAPPED_PEER_ROLE,
    PORT_CLASS_CLAIMED_KINDS,
    SELECT_TERMINATION_TASK,
    TERMINATION_ROLE,
    claimed_termination_kind,
    termination_field_key,
)
from netbox_data_import.forms import CableClassMappingForm
from netbox_data_import.models import (
    CableClassMapping,
    ImportProfile,
    SourceDocument,
    TerminationResolution,
)
from netbox_data_import.object_permissions import ObjectPermissionDenied
from netbox_data_import.tests.helpers import make_dcim_objects
from netbox_data_import.review_workspace import save_termination_resolution_and_replan

User = get_user_model()


def _flatten_choices(choices):
    """Return value and label pairs from flat or grouped NetBox choices."""
    flattened = []
    for value, label in choices:
        if isinstance(label, (tuple, list)):
            flattened.extend(label)
        else:
            flattened.append((value, label))
    return flattened


def _runtime_cable_type_choices():
    """Return Cable Type choices declared by the running NetBox instance."""
    return _flatten_choices(CableTypeChoices.CHOICES)


def _runtime_cable_profile_choices():
    """Return Cable Profile choices declared by the running NetBox instance."""
    return _flatten_choices(CableProfileChoices.CHOICES)


def _compatible_profile_choices():
    """Return the running Cable Profiles that accept one termination on each side."""
    return [
        (value, label)
        for value, label in _runtime_cable_profile_choices()
        if len(Cable(profile=value).profile_class.a_connectors) == 1
        and len(Cable(profile=value).profile_class.b_connectors) == 1
    ]


def _choice_value(form, field_name, label):
    """Return one submitted choice value by its operator-facing label."""
    return next(value for value, candidate in form.fields[field_name].choices if str(candidate) == label)


class TraceFieldKeyTest(SimpleTestCase):
    """The source adapter and Cable module share one termination-key vocabulary."""

    def test_each_port_class_claims_its_netbox_termination_kind(self):
        """Every fixed PortClass value maps to the kind from specification 6.1."""
        expected = {
            "NIC": "interface",
            "Switch Port": "interface",
            "Port": "interface",
            "Position Front": "front_port",
            "Fiber Pair Front": "front_port",
            "Punch-Down": "rear_port",
            "Fiber Pair Back": "rear_port",
        }

        self.assertEqual(PORT_CLASS_CLAIMED_KINDS, expected)
        self.assertEqual({value: claimed_termination_kind(value) for value in expected}, expected)

    def test_shared_field_keys_import_no_netbox_or_runtime_implementation(self):
        """Both source and target sides can import the shared module without crossing a boundary."""
        tree = ast.parse(pathlib.Path(field_keys.__file__).read_text())
        roots = {
            node.module.partition(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        roots.update(
            name.name.partition(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for name in node.names
        )

        self.assertEqual(roots, {"__future__", "json", "values"})

    def test_unknown_port_class_has_no_claimed_kind(self):
        """A value outside the fixed vocabulary fails instead of claiming a target kind."""
        with self.assertRaisesRegex(ValueError, "Unknown PortClass"):
            claimed_termination_kind("Management Port")

    def test_termination_field_key_is_normalized_sorted_compact_json(self):
        """The stored key follows the canonical trace identity and role contract."""
        key = termination_field_key(
            device="  TOR   1 ",
            cards=" Line   Card A ",
            port=" Ethernet   1/1 ",
            kind="interface",
            role=TERMINATION_ROLE,
        )

        self.assertEqual(
            key,
            '{"cards":"line card a","device":"tor 1","kind":"interface","port":"ethernet 1/1","role":"termination"}',
        )

    def test_mapped_peer_role_produces_a_distinct_canonical_key(self):
        """The mapped-peer selection cannot overwrite the termination selection."""
        values = {"device": "Panel 1", "cards": "", "port": "P01", "kind": "front_port"}

        termination = termination_field_key(**values, role=TERMINATION_ROLE)
        mapped_peer = termination_field_key(**values, role=MAPPED_PEER_ROLE)

        self.assertNotEqual(termination, mapped_peer)
        self.assertIn('"role":"mapped_peer"', mapped_peer)

    def test_trace_workbook_keeps_its_public_port_class_names(self):
        """Moving the vocabulary does not break the Source Adapter's public constants."""
        from netbox_data_import.field_keys import FRONT_PORT_CLASSES, INTERFACE_PORT_CLASSES, REAR_PORT_CLASSES
        from netbox_data_import.trace_workbook import (
            FRONT_PORT_CLASSES as TRACE_FRONT_PORT_CLASSES,
            INTERFACE_PORT_CLASSES as TRACE_INTERFACE_PORT_CLASSES,
            REAR_PORT_CLASSES as TRACE_REAR_PORT_CLASSES,
        )

        self.assertIs(TRACE_INTERFACE_PORT_CLASSES, INTERFACE_PORT_CLASSES)
        self.assertIs(TRACE_FRONT_PORT_CLASSES, FRONT_PORT_CLASSES)
        self.assertIs(TRACE_REAR_PORT_CLASSES, REAR_PORT_CLASSES)


class TracePolicyModelTest(TestCase):
    """Trace policy rows validate their identity, state, and applicability."""

    @classmethod
    def setUpTestData(cls):
        cls.trace_profile = ImportProfile.objects.create(
            name="Trace Decisions",
            source_adapter="trace_workbook",
            adapter_config={},
        )
        cls.flat_profile = ImportProfile.objects.create(name="Flat Decisions", adapter_config={})
        site, device_type, role = cls._device_dependencies()
        cls.device = Device.objects.create(
            name="Trace Device",
            site=site,
            device_type=device_type,
            role=role,
        )
        cls.interface = Interface.objects.create(device=cls.device, name="Ethernet 1/1")
        cls.interface_type = ObjectType.objects.get_for_model(cls.interface)

    @staticmethod
    def _device_dependencies():
        """Create the NetBox objects required by one real Device."""
        site, manufacturer, device_type, role = make_dcim_objects("TraceDecision")
        del manufacturer
        return site, device_type, role

    def _resolution(self, role, *, profile=None):
        """Build one unsaved resolution for the shared test termination."""
        return TerminationResolution(
            profile=profile or self.trace_profile,
            task_type=SELECT_TERMINATION_TASK,
            field_key=termination_field_key(
                device=self.device.name,
                cards="Line Card A",
                port=self.interface.name,
                kind="interface",
                role=role,
            ),
            selected_object_type=self.interface_type,
            selected_object_id=self.interface.pk,
            selected_display_name=str(self.interface),
        )

    def test_termination_resolution_stores_the_complete_selection(self):
        """A saved row carries its canonical binding and all three selected-object values."""
        resolution = self._resolution(TERMINATION_ROLE)
        resolution.full_clean()
        resolution.save()

        stored = TerminationResolution.objects.get(pk=resolution.pk)

        self.assertEqual(stored.task_type, "select_termination")
        self.assertEqual(
            stored.field_key,
            '{"cards":"line card a","device":"trace device","kind":"interface",'
            '"port":"ethernet 1/1","role":"termination"}',
        )
        self.assertEqual(stored.selected_object_type, self.interface_type)
        self.assertEqual(stored.selected_object_id, self.interface.pk)
        self.assertEqual(stored.selected_display_name, str(self.interface))

    def test_termination_and_mapped_peer_rows_coexist(self):
        """The role marker makes two decisions for one termination separate unique keys."""
        for role in (TERMINATION_ROLE, MAPPED_PEER_ROLE):
            resolution = self._resolution(role)
            resolution.full_clean()
            resolution.save()

        rows = TerminationResolution.objects.filter(profile=self.trace_profile)

        self.assertEqual(rows.count(), 2)
        self.assertEqual(
            {row.field_key for row in rows},
            {self._resolution(role).field_key for role in (TERMINATION_ROLE, MAPPED_PEER_ROLE)},
        )

    def test_same_task_and_field_key_are_unique_within_one_profile(self):
        """One bound trace field carries at most one selected object row."""
        first = self._resolution(TERMINATION_ROLE)
        first.save()
        duplicate = self._resolution(TERMINATION_ROLE)

        with self.assertRaises(ValidationError) as caught:
            duplicate.full_clean()

        self.assertIn("__all__", caught.exception.message_dict)

    def test_termination_resolution_rejects_a_noncanonical_key(self):
        """A display form or unnormalized JSON cannot become a stored decision key."""
        resolution = self._resolution(TERMINATION_ROLE)
        resolution.field_key = "Trace Device|Line Card A|Ethernet 1/1|interface|termination"

        with self.assertRaises(ValidationError) as caught:
            resolution.full_clean()

        self.assertEqual(caught.exception.error_dict["field_key"][0].code, "invalid")

    def test_termination_resolution_rejects_a_flat_profile(self):
        """Catalog applicability rejects trace decisions on a flat profile."""
        with self.assertRaisesMessage(ValidationError, "do not apply"):
            self._resolution(TERMINATION_ROLE, profile=self.flat_profile).full_clean()

    def test_cable_class_mapping_accepts_each_tri_state(self):
        """Flags distinguish unresolved, explicit none, and a runtime choice without a sentinel value."""
        cable_type = _runtime_cable_type_choices()[0][0]
        cable_profile = _compatible_profile_choices()[0][0]
        mappings = (
            CableClassMapping(profile=self.trace_profile, cable_class="Unresolved"),
            CableClassMapping(
                profile=self.trace_profile,
                cable_class="Plain",
                cable_type_resolved=True,
                cable_profile_resolved=True,
            ),
            CableClassMapping(
                profile=self.trace_profile,
                cable_class="Selected",
                cable_type_resolved=True,
                cable_type=cable_type,
                cable_profile_resolved=True,
                cable_profile=cable_profile,
            ),
        )

        for mapping in mappings:
            mapping.full_clean()

        self.assertEqual(
            [
                (mapping.cable_type_resolved, mapping.cable_type, mapping.cable_profile_resolved, mapping.cable_profile)
                for mapping in mappings
            ],
            [(False, None, False, None), (True, None, True, None), (True, cable_type, True, cable_profile)],
        )

    def _assert_database_rejects_invalid_value_states(self, resolved_field, value_field, selected_value):
        """Assert that each direct write path enforces one resolution dimension."""
        invalid_states = (
            ("unresolved value", {resolved_field: False, value_field: selected_value}),
            ("resolved empty", {resolved_field: True, value_field: ""}),
        )
        for invalid_state, invalid_values in invalid_states:
            for write_method in ("create", "bulk_create", "update"):
                cable_class = f"Invalid {value_field} {invalid_state} {write_method}"
                if write_method == "update":
                    mapping = CableClassMapping.objects.create(profile=self.trace_profile, cable_class=cable_class)
                with (
                    self.subTest(invalid_state=invalid_state, value_field=value_field, write_method=write_method),
                    self.assertRaises(IntegrityError),
                ):
                    with transaction.atomic():
                        if write_method == "create":
                            CableClassMapping.objects.create(
                                profile=self.trace_profile,
                                cable_class=cable_class,
                                **invalid_values,
                            )
                        elif write_method == "bulk_create":
                            CableClassMapping.objects.bulk_create(
                                [
                                    CableClassMapping(
                                        profile=self.trace_profile,
                                        cable_class=cable_class,
                                        **invalid_values,
                                    )
                                ]
                            )
                        else:
                            CableClassMapping.objects.filter(pk=mapping.pk).update(**invalid_values)

    def test_database_rejects_invalid_cable_type_states(self):
        """Every database write path rejects invalid Cable Type state representations."""
        self._assert_database_rejects_invalid_value_states(
            "cable_type_resolved",
            "cable_type",
            "cat6",
        )

    def test_database_rejects_invalid_cable_profile_states(self):
        """Every database write path rejects invalid Cable Profile state representations."""
        self._assert_database_rejects_invalid_value_states(
            "cable_profile_resolved",
            "cable_profile",
            "lc",
        )

    def test_database_accepts_each_cable_mapping_tri_state(self):
        """The database accepts unresolved, explicit none, and selected values."""
        cable_type = _runtime_cable_type_choices()[0][0]
        cable_profile = _compatible_profile_choices()[0][0]
        mappings = (
            CableClassMapping.objects.create(profile=self.trace_profile, cable_class="Database unresolved"),
            CableClassMapping.objects.create(
                profile=self.trace_profile,
                cable_class="Database explicit none",
                cable_type_resolved=True,
                cable_profile_resolved=True,
            ),
            CableClassMapping.objects.create(
                profile=self.trace_profile,
                cable_class="Database selected",
                cable_type_resolved=True,
                cable_type=cable_type,
                cable_profile_resolved=True,
                cable_profile=cable_profile,
            ),
            CableClassMapping.objects.create(
                profile=self.trace_profile,
                cable_class="Database stale selected",
                cable_type_resolved=True,
                cable_type="not-a-running-cable-type",
                cable_profile_resolved=True,
                cable_profile="not-a-running-cable-profile",
            ),
        )
        for mapping in mappings:
            mapping.refresh_from_db()

        self.assertEqual(
            [
                (mapping.cable_type_resolved, mapping.cable_type, mapping.cable_profile_resolved, mapping.cable_profile)
                for mapping in mappings
            ],
            [
                (False, None, False, None),
                (True, None, True, None),
                (True, cable_type, True, cable_profile),
                (True, "not-a-running-cable-type", True, "not-a-running-cable-profile"),
            ],
        )

    def test_stale_runtime_choices_have_the_stale_mapping_code(self):
        """Removed Cable Type and Cable Profile values share the stale-mapping condition."""
        cases = (
            ("cable_type", {"cable_type_resolved": True, "cable_type": "not-a-running-cable-type"}),
            (
                "cable_profile",
                {"cable_profile_resolved": True, "cable_profile": "not-a-running-cable-profile"},
            ),
        )
        for field_name, values in cases:
            with self.subTest(field_name=field_name):
                mapping = CableClassMapping(
                    profile=self.trace_profile,
                    cable_class=f"Retired {field_name}",
                    **values,
                )

                with self.assertRaises(ValidationError) as caught:
                    mapping.full_clean()

                self.assertEqual(
                    caught.exception.error_dict[field_name][0].code,
                    "cable.cableclass_stale_mapping",
                )

    def test_offered_incompatible_profile_has_its_own_code(self):
        """A running multi-connector profile is distinct from a stale profile value."""
        compatible = {value for value, _label in _compatible_profile_choices()}
        incompatible = next(value for value, _label in _runtime_cable_profile_choices() if value not in compatible)
        mapping = CableClassMapping(
            profile=self.trace_profile,
            cable_class="Trunk",
            cable_profile_resolved=True,
            cable_profile=incompatible,
        )

        with self.assertRaises(ValidationError) as caught:
            mapping.full_clean()

        self.assertEqual(caught.exception.error_dict["cable_profile"][0].code, "cable.profile_incompatible")

    def test_cable_class_mapping_rejects_a_flat_profile(self):
        """Catalog applicability rejects Cable target policy on a flat profile."""
        mapping = CableClassMapping(profile=self.flat_profile, cable_class="CAT6")

        with self.assertRaisesMessage(ValidationError, "do not apply"):
            mapping.full_clean()

    def test_cable_class_is_unique_within_one_profile(self):
        """One profile cannot carry two independent policies for the same CableClass."""
        CableClassMapping.objects.create(profile=self.trace_profile, cable_class="Duplicate")
        duplicate = CableClassMapping(profile=self.trace_profile, cable_class="Duplicate")

        with self.assertRaises(ValidationError) as caught:
            duplicate.full_clean()

        self.assertIn("__all__", caught.exception.message_dict)

    def test_catalog_registers_both_trace_policy_sections(self):
        """Both rows derive applicability from the source-trace output kind."""
        source_trace = frozenset({OutputKind.SOURCE_TRACE})

        self.assertTrue(policy_section("termination_resolutions").applies_to(source_trace))
        self.assertTrue(policy_section("cable_class_mappings").applies_to(source_trace))


class TerminationResolutionPersistenceTest(TestCase):
    """The manual-selection seam saves through policy permissions before replanning."""

    @classmethod
    def setUpTestData(cls):
        cls.actor = User.objects.create_superuser("trace-decider", "trace-decider@example.com", "testpass")
        cls.profile = ImportProfile.objects.create(
            name="Trace Replan",
            source_adapter="trace_workbook",
            adapter_config={},
        )
        cls.document = SourceDocument.store(profile=cls.profile, content=b"T5 will interpret this")
        site, manufacturer, device_type, role = make_dcim_objects("TraceReplan")
        del manufacturer
        device = Device.objects.create(name="Replan Device", site=site, device_type=device_type, role=role)
        cls.interface = Interface.objects.create(device=device, name="Ethernet 1/2")
        cls.interface_type = ObjectType.objects.get_for_model(cls.interface)
        cls.field_key = termination_field_key(
            device=device.name,
            cards="",
            port=cls.interface.name,
            kind="interface",
            role=TERMINATION_ROLE,
        )
        cls.planning_context = {"site_id": site.pk, "location_id": None, "tenant_id": None}

    def _save(self, actor):
        """Call the public manual-selection path with the shared test decision."""
        return save_termination_resolution_and_replan(
            profile=self.profile,
            source_document=self.document,
            actor=actor,
            planning_context=self.planning_context,
            task_type=SELECT_TERMINATION_TASK,
            field_key=self.field_key,
            selected_object_type=self.interface_type,
            selected_object_id=self.interface.pk,
            selected_display_name=str(self.interface),
        )

    def test_selection_is_saved_before_the_engine_reports_the_missing_cable_module(self):
        """T5 turns this delegated typed error into a plan without changing the persistence path."""
        with self.assertRaisesMessage(UnknownSourceAdapter, "Target Module"):
            self._save(self.actor)

        resolution = TerminationResolution.objects.get(
            profile=self.profile,
            task_type=SELECT_TERMINATION_TASK,
            field_key=self.field_key,
        )
        self.assertEqual(resolution.selected_object_type, self.interface_type)
        self.assertEqual(resolution.selected_object_id, self.interface.pk)
        self.assertEqual(resolution.selected_display_name, str(self.interface))

    def test_selection_write_enforces_the_policy_model_object_permission(self):
        """A caller without add permission cannot store a selection or reach replanning."""
        actor = User.objects.create_user("trace-no-policy", password="testpass")

        with self.assertRaises(ObjectPermissionDenied):
            self._save(actor)

        self.assertFalse(TerminationResolution.objects.filter(profile=self.profile, field_key=self.field_key).exists())

    def test_selection_cannot_store_trace_policy_for_a_flat_database_profile(self):
        """The stored profile controls whether a trace-only decision applies."""
        flat_profile = ImportProfile.objects.create(name="Flat Replan", adapter_config={})
        flat_document = SourceDocument.store(profile=flat_profile, content=b"Flat source content")
        fabricated_trace_profile = ImportProfile(pk=flat_profile.pk, source_adapter="trace_workbook")

        with self.assertRaisesMessage(ValidationError, "do not apply"):
            save_termination_resolution_and_replan(
                profile=fabricated_trace_profile,
                source_document=flat_document,
                actor=self.actor,
                planning_context=self.planning_context,
                task_type=SELECT_TERMINATION_TASK,
                field_key=self.field_key,
                selected_object_type=self.interface_type,
                selected_object_id=self.interface.pk,
                selected_display_name=str(self.interface),
            )

        self.assertFalse(TerminationResolution.objects.filter(profile=flat_profile).exists())


class CableClassMappingFormTest(TestCase):
    """The operator form derives both dimensions from the running NetBox instance."""

    @classmethod
    def setUpTestData(cls):
        cls.profile = ImportProfile.objects.create(
            name="Trace Mapping Form",
            source_adapter="trace_workbook",
            adapter_config={},
        )

    def test_form_offers_runtime_types_and_only_single_side_profiles(self):
        """The select fields contain current choices, compatible profiles, and explicit none."""
        form = CableClassMappingForm(initial={"profile": self.profile})
        runtime_types = {value for value, _label in _runtime_cable_type_choices()}
        compatible_profiles = {value for value, _label in _compatible_profile_choices()}
        offered_types = {value for value, _label in form.fields["cable_type"].choices}
        offered_profiles = {value for value, _label in form.fields["cable_profile"].choices}
        type_state_values = {
            value
            for value, label in form.fields["cable_type"].choices
            if str(label) in {"Unresolved", "Explicitly none"}
        }
        profile_state_values = {
            value
            for value, label in form.fields["cable_profile"].choices
            if str(label) in {"Unresolved", "Explicitly none"}
        }

        self.assertEqual(offered_types, runtime_types | type_state_values)
        self.assertEqual(offered_profiles, compatible_profiles | profile_state_values)
        runtime_profiles = {value for value, _label in _runtime_cable_profile_choices()}
        # Comparing the form against its own helper would still pass if the helper offered everything.
        self.assertTrue(compatible_profiles < runtime_profiles, "the restriction must exclude a running profile")
        self.assertTrue(compatible_profiles, "the restriction must leave a usable profile")
        self.assertEqual(
            {str(label) for _value, label in form.fields["cable_type"].choices} & {"Unresolved", "Explicitly none"},
            {"Unresolved", "Explicitly none"},
        )
        self.assertEqual(
            {str(label) for _value, label in form.fields["cable_profile"].choices} & {"Unresolved", "Explicitly none"},
            {"Unresolved", "Explicitly none"},
        )

    def test_form_stores_explicit_none_as_resolved_null(self):
        """The form-only option never becomes a magic value in either model column."""
        unbound = CableClassMappingForm(initial={"profile": self.profile})
        none_type = _choice_value(unbound, "cable_type", "Explicitly none")
        none_profile = _choice_value(unbound, "cable_profile", "Explicitly none")
        form = CableClassMappingForm(
            data={
                "profile": self.profile.pk,
                "cable_class": "Plain Cable",
                "cable_type": none_type,
                "cable_profile": none_profile,
            }
        )

        self.assertTrue(form.is_valid(), form.errors)
        mapping = form.save()
        self.assertTrue(mapping.cable_type_resolved)
        self.assertIsNone(mapping.cable_type)
        self.assertTrue(mapping.cable_profile_resolved)
        self.assertIsNone(mapping.cable_profile)

    def test_form_uses_the_shared_incompatible_profile_code(self):
        """A crafted POST receives the same profile-cardinality code as model validation."""
        compatible = {value for value, _label in _compatible_profile_choices()}
        incompatible = next(value for value, _label in _runtime_cable_profile_choices() if value not in compatible)
        form = CableClassMappingForm(
            data={
                "profile": self.profile.pk,
                "cable_class": "Unsupported Trunk",
                "cable_type": "",
                "cable_profile": incompatible,
            }
        )

        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors.as_data()["cable_profile"][0].code, "cable.profile_incompatible")


class CableClassMappingViewTest(TestCase):
    """The profile UI exposes CableClass policy only where the catalog permits it."""

    @classmethod
    def setUpTestData(cls):
        cls.actor = User.objects.create_superuser("trace-policy-ui", "trace-policy-ui@example.com", "testpass")
        cls.trace_profile = ImportProfile.objects.create(
            name="Trace Policy UI",
            source_adapter="trace_workbook",
            adapter_config={},
        )
        cls.flat_profile = ImportProfile.objects.create(name="Flat Policy UI", adapter_config={})

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.actor)

    def _add_form(self):
        """Return the real add form used by the HTTP surface."""
        response = self.client.get(
            reverse(
                "plugins:netbox_data_import:cableclassmapping_add",
                kwargs={"profile_pk": self.trace_profile.pk},
            )
        )
        self.assertEqual(response.status_code, 200)
        return response.context["form"]

    def test_profile_detail_switches_policy_sections_through_catalog_applicability(self):
        """Trace shows CableClass policy, while flat shows only its own mapping sections."""
        trace = self.client.get(self.trace_profile.get_absolute_url())
        flat = self.client.get(self.flat_profile.get_absolute_url())

        self.assertContains(trace, "CableClass Mappings")
        self.assertNotContains(trace, "Column Mappings")
        self.assertNotContains(trace, "Class → Role Mappings")
        self.assertNotContains(trace, "Device Type Mappings")
        self.assertNotContains(trace, "Column Transform Rules")
        self.assertNotContains(flat, "CableClass Mappings")
        self.assertContains(flat, "Column Mappings")

    def test_add_view_creates_a_runtime_choice_and_explicit_none(self):
        """POST through the add view stores both independent selection states."""
        form = self._add_form()
        cable_type = _runtime_cable_type_choices()[0][0]
        none_profile = _choice_value(form, "cable_profile", "Explicitly none")
        url = reverse(
            "plugins:netbox_data_import:cableclassmapping_add",
            kwargs={"profile_pk": self.trace_profile.pk},
        )

        response = self.client.post(
            url,
            {
                "profile": self.trace_profile.pk,
                "cable_class": "Copper Patch",
                "cable_type": cable_type,
                "cable_profile": none_profile,
            },
        )

        self.assertEqual(response.status_code, 302)
        mapping = CableClassMapping.objects.get(profile=self.trace_profile, cable_class="Copper Patch")
        self.assertEqual((mapping.cable_type_resolved, mapping.cable_type), (True, cable_type))
        self.assertEqual((mapping.cable_profile_resolved, mapping.cable_profile), (True, None))

    def test_edit_view_updates_both_dimensions(self):
        """POST through the edit view can replace both decisions."""
        mapping = CableClassMapping.objects.create(profile=self.trace_profile, cable_class="Fiber Patch")
        url = reverse("plugins:netbox_data_import:cableclassmapping_edit", kwargs={"pk": mapping.pk})
        get_response = self.client.get(url)
        self.assertEqual(get_response.status_code, 200)
        form = get_response.context["form"]
        none_type = _choice_value(form, "cable_type", "Explicitly none")
        cable_profile = _compatible_profile_choices()[0][0]

        response = self.client.post(
            url,
            {
                "profile": self.trace_profile.pk,
                "cable_class": mapping.cable_class,
                "cable_type": none_type,
                "cable_profile": cable_profile,
            },
        )

        self.assertEqual(response.status_code, 302)
        mapping.refresh_from_db()
        self.assertEqual((mapping.cable_type_resolved, mapping.cable_type), (True, None))
        self.assertEqual((mapping.cable_profile_resolved, mapping.cable_profile), (True, cable_profile))

    def test_delete_view_removes_the_mapping(self):
        """POST through the delete view removes one CableClass policy row."""
        mapping = CableClassMapping.objects.create(profile=self.trace_profile, cable_class="Remove Me")
        url = reverse("plugins:netbox_data_import:cableclassmapping_delete", kwargs={"pk": mapping.pk})

        response = self.client.post(url, {"confirm": "yes"})

        self.assertEqual(response.status_code, 302)
        self.assertFalse(CableClassMapping.objects.filter(pk=mapping.pk).exists())

    def test_detail_table_links_to_edit_and_delete_views(self):
        """The inline table exposes both management actions for an existing row."""
        mapping = CableClassMapping.objects.create(profile=self.trace_profile, cable_class="Managed")

        response = self.client.get(self.trace_profile.get_absolute_url())

        self.assertContains(
            response,
            reverse("plugins:netbox_data_import:cableclassmapping_edit", kwargs={"pk": mapping.pk}),
        )
        self.assertContains(
            response,
            reverse("plugins:netbox_data_import:cableclassmapping_delete", kwargs={"pk": mapping.pk}),
        )
