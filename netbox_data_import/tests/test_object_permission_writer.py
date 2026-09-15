# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The one seam every permission-scoped import write goes through.

NetBox grants a bare `has_perm("app.add_thing")` when the user may act on any object of the type.
An ObjectPermission's constraints only apply to a saved instance, so a scoped write has to save
first and then ask again. These tests use real users and real ObjectPermission rows: a mocked
permission check would only restate the assumption under test.
"""

from copy import copy

from django.core.exceptions import ValidationError
from django.db import connection, models
from django.db.models.signals import post_save, pre_save
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext, isolate_apps

from netbox_data_import.field_keys import SELECT_TERMINATION_TASK, termination_field_key
from netbox_data_import.models import (
    DeviceTypeMapping,
    ImportProfile,
    InferenceBackend,
    TerminationResolution,
    index_digest,
)
from netbox_data_import.object_permissions import (
    ObjectPermissionDenied,
    ProspectiveRelation,
    _prepare_prospective_world,
    _prospective_row_matches,
    assess_permission_scoped_save,
    assess_permission_scoped_save_option,
    delete_permission_scoped_objects,
    enforce_saved_object_permission,
    save_permission_scoped_object,
)
from netbox_data_import.tests.helpers import make_dcim_objects, run_on_separate_connection, user_with_object_permission


class EnforceSavedObjectPermissionTest(TestCase):
    """The check has to work on a NetBox model and on a plain plugin model alike."""

    def setUp(self):
        self.profile = ImportProfile.objects.create(name="Scope Profile")
        self.other = ImportProfile.objects.create(name="Other Scope Profile")

    def test_a_constrained_netbox_model_is_scoped(self):
        """The NetBox models were already covered; this pins that behaviour before the change."""
        from dcim.models import Manufacturer

        allowed = Manufacturer.objects.create(name="Allowed", slug="allowed")
        refused = Manufacturer.objects.create(name="Refused", slug="refused")
        user = user_with_object_permission("scope-mfg", [(Manufacturer, ["view"], {"slug": "allowed"})])

        enforce_saved_object_permission(allowed, user, "view")
        with self.assertRaises(ObjectPermissionDenied):
            enforce_saved_object_permission(refused, user, "view")

    def test_a_constrained_plain_plugin_model_is_scoped(self):
        """A policy model is not a NetBoxModel, so its scope check has to hold on its own."""
        mine = DeviceTypeMapping.objects.create(profile=self.profile, source_make="A", source_model="B")
        theirs = DeviceTypeMapping.objects.create(profile=self.other, source_make="A", source_model="B")
        user = user_with_object_permission("scope-map", [(DeviceTypeMapping, ["view"], {"profile": self.profile.pk})])

        enforce_saved_object_permission(mine, user, "view")
        with self.assertRaises(ObjectPermissionDenied):
            enforce_saved_object_permission(theirs, user, "view")

    def test_no_user_is_not_a_scope_check(self):
        """Background imports run without a request user and keep their own authorization path."""
        mapping = DeviceTypeMapping.objects.create(profile=self.profile, source_make="A", source_model="B")
        enforce_saved_object_permission(mapping, None, "view")


class ProspectiveForeignKeyTargetTest(TransactionTestCase):
    """Prospective relations use the concrete value named by each foreign key."""

    @isolate_apps("netbox_data_import")
    def test_a_non_primary_foreign_key_uses_its_known_target_value(self):
        """A generated related primary key does not hide a known natural relation key."""

        class CopyableTestModel(models.Model):
            def __copy__(self):
                duplicate = type(self)()
                duplicate.__dict__.update(self.__dict__)
                return duplicate

            class Meta:
                abstract = True
                app_label = "netbox_data_import"

        class ProspectiveTargetTestModel(CopyableTestModel):
            code = models.CharField(max_length=32, unique=True)

            def __str__(self):
                return self.code

            class Meta:
                app_label = "netbox_data_import"
                db_table = "netbox_data_import_test_prospective_target"

        class ProspectiveRootTestModel(CopyableTestModel):
            target = models.ForeignKey(
                ProspectiveTargetTestModel,
                db_constraint=False,
                on_delete=models.CASCADE,
                to_field="code",
            )

            def __str__(self):
                return str(self.target_id)

            class Meta:
                app_label = "netbox_data_import"
                db_table = "netbox_data_import_test_prospective_root"

        with connection.schema_editor() as schema_editor:
            schema_editor.create_model(ProspectiveTargetTestModel)
            schema_editor.create_model(ProspectiveRootTestModel)
        try:
            saved_target = ProspectiveTargetTestModel.objects.create(code="saved-target")
            saved_world = _prepare_prospective_world(ProspectiveRootTestModel(), {"target": saved_target})
            planned_target = ProspectiveTargetTestModel(code="planned-target")
            planned_world = _prepare_prospective_world(ProspectiveRootTestModel(), {"target": planned_target})

            self.assertEqual(saved_world.root.target_id, saved_target.code)
            self.assertTrue(
                _prospective_row_matches(
                    None,
                    ProspectiveRootTestModel,
                    {"target__code": saved_target.code},
                    saved_world,
                )
            )
            self.assertEqual(planned_world.root.target_id, planned_target.code)
            self.assertTrue(
                _prospective_row_matches(
                    None,
                    ProspectiveRootTestModel,
                    {"target_id": planned_target.code},
                    planned_world,
                )
            )
            self.assertFalse(
                _prospective_row_matches(
                    None,
                    ProspectiveRootTestModel,
                    {"target__pk": -1},
                    planned_world,
                )
            )
        finally:
            with connection.schema_editor() as schema_editor:
                schema_editor.delete_model(ProspectiveRootTestModel)
                schema_editor.delete_model(ProspectiveTargetTestModel)


class AssessPermissionScopedSaveTest(TestCase):
    """The advisory check applies the writer's real object scope without writing."""

    def setUp(self):
        self.profile = ImportProfile.objects.create(name="Assessment Profile")
        self.other = ImportProfile.objects.create(name="Assessment Other Profile")

    def _lookup(self, source_make="Acme", *, profile=None):
        return {
            "profile": profile or self.profile,
            "source_make": source_make,
            "source_model": "Widget",
        }

    @staticmethod
    def _values(manufacturer="acme"):
        return {
            "netbox_manufacturer_slug": manufacturer,
            "netbox_device_type_slug": "acme-widget",
        }

    def test_a_constrained_create_is_assessed_against_its_prospective_state(self):
        user = user_with_object_permission(
            "assess-add",
            [(DeviceTypeMapping, ["add"], {"profile_id": self.profile.pk})],
        )

        inside = assess_permission_scoped_save(user, DeviceTypeMapping, self._lookup(), self._values())
        outside = assess_permission_scoped_save(
            user,
            DeviceTypeMapping,
            self._lookup(profile=self.other),
            self._values(),
        )

        self.assertTrue(inside.allowed)
        self.assertEqual(inside.permission, "netbox_data_import.add_devicetypemapping")
        self.assertFalse(outside.allowed)
        self.assertFalse(DeviceTypeMapping.objects.exists())

    def test_a_save_option_applies_known_constraints_and_defers_the_chosen_value(self):
        user = user_with_object_permission(
            "assess-option",
            [
                (
                    DeviceTypeMapping,
                    ["add"],
                    {
                        "profile_id": self.profile.pk,
                        "netbox_manufacturer_slug": "chosen-later",
                    },
                )
            ],
        )

        inside = assess_permission_scoped_save_option(
            user,
            DeviceTypeMapping,
            self._lookup(),
            self._values("not-chosen-yet"),
            unknown_fields={"netbox_manufacturer_slug"},
        )
        outside = assess_permission_scoped_save_option(
            user,
            DeviceTypeMapping,
            self._lookup(profile=self.other),
            self._values("not-chosen-yet"),
            unknown_fields={"netbox_manufacturer_slug"},
        )

        self.assertTrue(inside.allowed)
        self.assertFalse(outside.allowed)
        self.assertFalse(DeviceTypeMapping.objects.exists())

    def test_a_json_value_is_prepared_for_the_prospective_database_row(self):
        user = user_with_object_permission(
            "assess-json",
            [(InferenceBackend, ["add"], {"backend_key": "prospective"})],
        )

        assessment = assess_permission_scoped_save(
            user,
            InferenceBackend,
            {"backend_key": "prospective"},
            {
                "display_name": "Prospective backend",
                "api_root": "https://backend.example.invalid:443",
                "model": "inference-model",
                "credential_reference": {
                    "backend": "vault_kv_v2",
                    "mount": "secret",
                    "path": "inference/backend",
                    "field": "api_key",
                },
            },
        )

        self.assertTrue(assessment.allowed)
        self.assertFalse(InferenceBackend.objects.exists())

    def test_a_missing_keep_is_assessed_against_its_prospective_add_scope(self):
        user = user_with_object_permission(
            "assess-missing-keep",
            [(DeviceTypeMapping, ["add"], {"profile_id": self.other.pk})],
        )

        assessment = assess_permission_scoped_save(
            user,
            DeviceTypeMapping,
            self._lookup(),
            self._values(),
            on_existing="keep",
        )
        with self.assertRaises(ObjectPermissionDenied):
            save_permission_scoped_object(
                user,
                DeviceTypeMapping,
                self._lookup(),
                self._values(),
                on_existing="keep",
            )

        self.assertFalse(assessment.allowed)
        self.assertFalse(DeviceTypeMapping.objects.exists())

    def test_an_update_requires_both_current_and_prospective_change_scope(self):
        mapping = DeviceTypeMapping.objects.create(**self._lookup(), **self._values("inside"))
        in_scope = user_with_object_permission(
            "assess-change",
            [
                (
                    DeviceTypeMapping,
                    ["change"],
                    {"profile_id": self.profile.pk, "netbox_manufacturer_slug": "inside"},
                )
            ],
        )
        current_outside = user_with_object_permission(
            "assess-current-out",
            [(DeviceTypeMapping, ["change"], {"netbox_manufacturer_slug": "after"})],
        )

        allowed = assess_permission_scoped_save(
            in_scope,
            DeviceTypeMapping,
            self._lookup(),
            {"netbox_device_type_slug": "revised-widget"},
        )
        resulting_outside = assess_permission_scoped_save(
            in_scope,
            DeviceTypeMapping,
            self._lookup(),
            {"netbox_manufacturer_slug": "outside"},
        )
        current_denied = assess_permission_scoped_save(
            current_outside,
            DeviceTypeMapping,
            self._lookup(),
            {"netbox_manufacturer_slug": "after"},
        )

        self.assertTrue(allowed.allowed)
        self.assertFalse(resulting_outside.allowed)
        self.assertFalse(current_denied.allowed)
        mapping.refresh_from_db()
        self.assertEqual(mapping.netbox_manufacturer_slug, "inside")
        self.assertEqual(mapping.netbox_device_type_slug, "acme-widget")

    def test_a_cyclic_constraint_sees_the_prospective_row(self):
        user = user_with_object_permission(
            "assess-cycle",
            [
                (
                    DeviceTypeMapping,
                    ["add"],
                    {
                        "profile__device_type_mappings__source_make": "Cyclic",
                        "profile__device_type_mappings__netbox_manufacturer_slug": "inside",
                    },
                )
            ],
        )

        assessment = assess_permission_scoped_save(
            user,
            DeviceTypeMapping,
            self._lookup("Cyclic"),
            self._values("inside"),
        )

        self.assertTrue(assessment.allowed)
        self.assertFalse(DeviceTypeMapping.objects.exists())

    def test_a_multi_valued_constraint_keeps_related_predicates_correlated(self):
        matched_make = DeviceTypeMapping.objects.create(**self._lookup("Matched"), **self._values("wrong"))
        DeviceTypeMapping.objects.create(**self._lookup("Wrong"), **self._values("inside"))
        user = user_with_object_permission(
            "assess-correlation",
            [
                (
                    DeviceTypeMapping,
                    ["add"],
                    {
                        "profile__device_type_mappings__source_make": "Matched",
                        "profile__device_type_mappings__netbox_manufacturer_slug": "inside",
                    },
                )
            ],
        )

        split_rows = assess_permission_scoped_save(
            user,
            DeviceTypeMapping,
            self._lookup("Candidate"),
            self._values("candidate"),
        )
        matched_make.netbox_manufacturer_slug = "inside"
        matched_make.save(update_fields=["netbox_manufacturer_slug"])
        matching_row = assess_permission_scoped_save(
            user,
            DeviceTypeMapping,
            self._lookup("Candidate"),
            self._values("candidate"),
        )

        self.assertFalse(split_rows.allowed)
        self.assertTrue(matching_row.allowed)

    def test_known_primary_key_nullness_is_supported_without_consuming_the_sequence(self):
        first = DeviceTypeMapping.objects.create(**self._lookup("First"), **self._values())
        user = user_with_object_permission(
            "assess-primary-key",
            [(DeviceTypeMapping, ["add"], {"pk__isnull": False})],
        )
        signals = []

        def record_signal(sender, **kwargs):
            signals.append(sender)

        pre_save.connect(record_signal, sender=DeviceTypeMapping, weak=False)
        post_save.connect(record_signal, sender=DeviceTypeMapping, weak=False)
        self.addCleanup(pre_save.disconnect, record_signal, sender=DeviceTypeMapping)
        self.addCleanup(post_save.disconnect, record_signal, sender=DeviceTypeMapping)

        assessment = assess_permission_scoped_save(
            user,
            DeviceTypeMapping,
            self._lookup("Candidate"),
            self._values(),
        )

        self.assertTrue(assessment.allowed)
        self.assertEqual(signals, [])
        self.assertFalse(DeviceTypeMapping.objects.filter(source_make="Candidate").exists())
        pre_save.disconnect(record_signal, sender=DeviceTypeMapping)
        post_save.disconnect(record_signal, sender=DeviceTypeMapping)
        following = DeviceTypeMapping.objects.create(**self._lookup("Following"), **self._values())
        self.assertEqual(following.pk, first.pk + 1)

    def test_an_unknown_generated_primary_key_cannot_grant_create_scope(self):
        user = user_with_object_permission(
            "assess-generated-primary-key",
            [(DeviceTypeMapping, ["add"], {"pk": 1})],
        )

        assessment = assess_permission_scoped_save(
            user,
            DeviceTypeMapping,
            self._lookup(),
            self._values(),
        )

        self.assertFalse(assessment.allowed)

    def test_an_invalid_constraint_fails_closed(self):
        user = user_with_object_permission(
            "assess-invalid",
            [(DeviceTypeMapping, ["add"], {"missing_field": "value"})],
        )

        assessment = assess_permission_scoped_save(
            user,
            DeviceTypeMapping,
            self._lookup(),
            self._values(),
        )

        self.assertFalse(assessment.allowed)

    def test_an_invalid_typed_constraint_fails_closed(self):
        user = user_with_object_permission(
            "assess-invalid-value",
            [(DeviceTypeMapping, ["add"], {"profile__created": "not-a-date"})],
        )

        assessment = assess_permission_scoped_save(
            user,
            DeviceTypeMapping,
            self._lookup(),
            self._values(),
        )

        self.assertFalse(assessment.allowed)

    def test_unsaved_related_rows_are_visible_in_the_prospective_world(self):
        """Relation constraints see exact planned rows without saving or consuming keys."""
        from dcim.models import Device, DeviceRole, Rack

        site, _manufacturer, device_type, existing_role = make_dcim_objects("ProspectiveRelation")
        existing_rack = Rack.objects.create(name="existing-rack", site=site)
        existing_device = Device.objects.create(
            name="existing-device",
            site=site,
            device_type=device_type,
            role=existing_role,
        )
        planned_rack = Rack(name="planned-rack", site=site)
        planned_role = DeviceRole(name="Planned Role", slug="planned-role", color="9e9e9e")
        user = user_with_object_permission(
            "assess-related",
            [
                (
                    Device,
                    ["add"],
                    {
                        "rack__isnull": False,
                        "rack__name": planned_rack.name,
                        "role__isnull": False,
                        "role__slug": planned_role.slug,
                    },
                )
            ],
        )
        signals = []

        def record_signal(sender, **kwargs):
            signals.append(sender)

        for model in (Device, Rack, DeviceRole):
            pre_save.connect(record_signal, sender=model, weak=False)
            post_save.connect(record_signal, sender=model, weak=False)
            self.addCleanup(pre_save.disconnect, record_signal, sender=model)
            self.addCleanup(post_save.disconnect, record_signal, sender=model)

        assessment = assess_permission_scoped_save(
            user,
            Device,
            {"name": "planned-device"},
            {"site": site, "device_type": device_type},
            prospective_relations={"rack": planned_rack, "role": planned_role},
        )

        self.assertTrue(assessment.allowed)
        self.assertEqual(signals, [])
        self.assertIsNone(planned_rack.pk)
        self.assertIsNone(planned_role.pk)
        self.assertFalse(Device.objects.filter(name="planned-device").exists())
        self.assertFalse(Rack.objects.filter(name=planned_rack.name).exists())
        self.assertFalse(DeviceRole.objects.filter(slug=planned_role.slug).exists())

        for model in (Device, Rack, DeviceRole):
            pre_save.disconnect(record_signal, sender=model)
            post_save.disconnect(record_signal, sender=model)
        following_rack = Rack.objects.create(name="following-rack", site=site)
        following_role = DeviceRole.objects.create(name="Following Role", slug="following-role")
        following_device = Device.objects.create(
            name="following-device",
            site=site,
            device_type=device_type,
            role=existing_role,
        )
        self.assertEqual(following_rack.pk, existing_rack.pk + 1)
        self.assertEqual(following_role.pk, existing_role.pk + 1)
        self.assertEqual(following_device.pk, existing_device.pk + 1)

    def test_synthetic_relation_keys_are_presence_only(self):
        """Synthetic keys support nullness but cannot satisfy value constraints."""
        from dcim.models import Device, DeviceRole, Rack

        site, _manufacturer, device_type, _existing_role = make_dcim_objects("SyntheticRelation")
        planned_rack = Rack(name="planned-rack", site=site)
        planned_role = DeviceRole(name="Planned Role", slug="planned-role", color="9e9e9e")
        values = {"site": site, "device_type": device_type}
        related = {"rack": planned_rack, "role": planned_role}
        cases = (
            ("assess-related-null", {"rack__isnull": True}),
            ("assess-related-id-null", {"rack_id": None}),
            ("assess-related-role-null", {"role__isnull": True}),
            ("assess-related-synthetic-id", {"rack_id": -1}),
            ("assess-related-traversed-id", {"role__pk": -1}),
            ("assess-related-wrong-name", {"rack__name": "other-rack"}),
        )

        for username, constraint in cases:
            with self.subTest(constraint=constraint):
                user = user_with_object_permission(username, [(Device, ["add"], constraint)])
                assessment = assess_permission_scoped_save(
                    user,
                    Device,
                    {"name": "planned-device"},
                    values,
                    prospective_relations=related,
                )
                self.assertFalse(assessment.allowed)

    def test_an_alternate_traversal_cannot_use_a_synthetic_relation_key(self):
        """A generated Rack key stays hidden when the constraint reaches it through Site."""
        from dcim.models import Device, Rack

        site, _manufacturer, device_type, role = make_dcim_objects("AlternateSynthetic")
        planned_rack = Rack(name="planned-rack", site=site)
        values = {"site": site, "device_type": device_type, "role": role}
        cases = (
            ("assess-alternate-related-pk", {"site__racks__pk": -1}),
            ("assess-alternate-root-fk", {"site__devices__rack_id": -1}),
        )

        for username, constraint in cases:
            with self.subTest(constraint=constraint):
                user = user_with_object_permission(username, [(Device, ["add"], constraint)])
                assessment = assess_permission_scoped_save(
                    user,
                    Device,
                    {"name": "planned-device"},
                    values,
                    prospective_relations={"rack": planned_rack},
                )

                self.assertFalse(assessment.allowed)

    def test_multiple_unsaved_relations_of_one_model_share_one_world(self):
        """Two forward relations can resolve to distinct planned rows of one model."""
        from dcim.models import Device
        from ipam.models import IPAddress

        site, _manufacturer, device_type, role = make_dcim_objects("MultipleRelation")
        primary = IPAddress(address="198.18.0.10/32")
        out_of_band = IPAddress(address="198.18.0.11/32")
        user = user_with_object_permission(
            "assess-multiple-related",
            [
                (
                    Device,
                    ["add"],
                    {
                        "primary_ip4__address": str(primary.address),
                        "oob_ip__address": str(out_of_band.address),
                    },
                )
            ],
        )

        assessment = assess_permission_scoped_save(
            user,
            Device,
            {"name": "multiple-relation-device"},
            {"site": site, "device_type": device_type, "role": role},
            prospective_relations={"primary_ip4": primary, "oob_ip": out_of_band},
        )

        self.assertTrue(assessment.allowed)
        self.assertIsNone(primary.pk)
        self.assertIsNone(out_of_band.pk)
        self.assertFalse(IPAddress.objects.filter(address__in=(primary.address, out_of_band.address)).exists())

    def test_a_saved_related_row_replaces_its_physical_state(self):
        """The related world contains the final saved row once, not its stored version too."""
        from dcim.models import Device, Interface
        from ipam.models import IPAddress

        site, _manufacturer, device_type, role = make_dcim_objects("SavedRelation")
        interface_device = Device.objects.create(
            name="interface-device",
            site=site,
            device_type=device_type,
            role=role,
        )
        interface = Interface.objects.create(device=interface_device, name="mgmt0", type="1000base-t")
        stored = IPAddress.objects.create(address="198.18.0.12/32")
        final = copy(stored)
        final.assigned_object = interface
        planned_oob = IPAddress(address="198.18.0.15/32")
        assigned = user_with_object_permission(
            "assess-saved-related-final",
            [
                (
                    Device,
                    ["add"],
                    {"primary_ip4__pk": stored.pk, "primary_ip4__assigned_object_id__isnull": False},
                )
            ],
        )
        stored_only = user_with_object_permission(
            "assess-saved-related-physical",
            [(Device, ["add"], {"primary_ip4__assigned_object_id__isnull": True})],
        )
        values = {"site": site, "device_type": device_type, "role": role}
        related = {"primary_ip4": final, "oob_ip": planned_oob}

        final_assessment = assess_permission_scoped_save(
            assigned,
            Device,
            {"name": "saved-relation-device"},
            values,
            prospective_relations=related,
        )
        physical_assessment = assess_permission_scoped_save(
            stored_only,
            Device,
            {"name": "saved-relation-device"},
            values,
            prospective_relations=related,
        )

        self.assertTrue(final_assessment.allowed)
        self.assertFalse(physical_assessment.allowed)
        stored.refresh_from_db()
        self.assertIsNone(stored.assigned_object)

    def test_duplicate_saved_candidates_must_describe_one_final_state(self):
        """Identical saved candidates coalesce, while conflicting copies fail closed."""
        from dcim.models import Device, Interface
        from ipam.models import IPAddress

        site, _manufacturer, device_type, role = make_dcim_objects("DuplicateRelation")
        interface_device = Device.objects.create(
            name="interface-device",
            site=site,
            device_type=device_type,
            role=role,
        )
        interface = Interface.objects.create(device=interface_device, name="mgmt0", type="1000base-t")
        stored = IPAddress.objects.create(address="198.18.0.13/32")
        assigned = copy(stored)
        assigned.assigned_object = interface
        same = copy(assigned)
        conflicting = copy(stored)
        user = user_with_object_permission("assess-duplicate-related", [(Device, ["add"], None)])
        values = {"site": site, "device_type": device_type, "role": role}

        coalesced = assess_permission_scoped_save(
            user,
            Device,
            {"name": "coalesced-device"},
            values,
            prospective_relations={"primary_ip4": assigned, "oob_ip": same},
        )
        refused = assess_permission_scoped_save(
            user,
            Device,
            {"name": "conflicting-device"},
            values,
            prospective_relations={"primary_ip4": assigned, "oob_ip": conflicting},
        )

        self.assertTrue(coalesced.allowed)
        self.assertFalse(refused.allowed)

    def test_a_generated_related_field_is_presence_only(self):
        """A synthetic interface key can prove assignment, but it has no usable value."""
        from dcim.models import Device, Interface
        from ipam.models import IPAddress

        site, _manufacturer, device_type, role = make_dcim_objects("GeneratedField")
        physical_device = Device.objects.create(
            name="physical-interface-device",
            site=site,
            device_type=device_type,
            role=role,
        )
        physical_interface = Interface.objects.create(
            device=physical_device,
            name="physical-interface",
            type="1000base-t",
        )
        interface = Interface(device=Device(), name="mgmt0", type="1000base-t")
        interface.pk = physical_interface.pk
        address = IPAddress(address="198.18.0.14/32")
        address.assigned_object = interface
        relation = ProspectiveRelation(address, generated_fields=frozenset({"assigned_object_id"}))
        present = user_with_object_permission(
            "assess-generated-related-present",
            [(Device, ["add"], {"primary_ip4__assigned_object_id__isnull": False})],
        )
        exact = user_with_object_permission(
            "assess-generated-related-exact",
            [(Device, ["add"], {"primary_ip4__assigned_object_id": physical_interface.pk})],
        )
        alternate = user_with_object_permission(
            "assess-generated-related-alternate",
            [
                (
                    Device,
                    ["add"],
                    {"site__devices__primary_ip4__assigned_object_id": physical_interface.pk},
                )
            ],
        )
        downstream = user_with_object_permission(
            "assess-generated-related-downstream",
            [(Device, ["add"], {"primary_ip4__interface__name": "physical-interface"})],
        )
        values = {"site": site, "device_type": device_type, "role": role}

        present_assessment = assess_permission_scoped_save(
            present,
            Device,
            {"name": "generated-field-device"},
            values,
            prospective_relations={"primary_ip4": relation},
        )
        exact_assessment = assess_permission_scoped_save(
            exact,
            Device,
            {"name": "generated-field-device"},
            values,
            prospective_relations={"primary_ip4": relation},
        )
        alternate_assessment = assess_permission_scoped_save(
            alternate,
            Device,
            {"name": "generated-field-device"},
            values,
            prospective_relations={"primary_ip4": relation},
        )
        downstream_assessment = assess_permission_scoped_save(
            downstream,
            Device,
            {"name": "generated-field-device"},
            values,
            prospective_relations={"primary_ip4": relation},
        )

        self.assertTrue(present_assessment.allowed)
        self.assertFalse(exact_assessment.allowed)
        self.assertFalse(alternate_assessment.allowed)
        self.assertFalse(downstream_assessment.allowed)

    def test_root_reverse_relations_cannot_authorize_a_prospective_write(self):
        """Missing planned components, contacts, or provenance do not grant scope."""
        from dcim.models import Device

        site, _manufacturer, device_type, role = make_dcim_objects("ReverseRelation")
        values = {"site": site, "device_type": device_type, "role": role}
        paths = (
            "interfaces",
            "contacts",
            "data_import_source",
            "site__devices__interfaces",
            "site__devices__contacts",
            "site__devices__data_import_source",
        )
        for index, path in enumerate(paths):
            with self.subTest(path=path):
                user = user_with_object_permission(
                    f"assess-reverse-related-{index}",
                    [(Device, ["add"], {f"{path}__isnull": True})],
                )

                assessment = assess_permission_scoped_save(
                    user,
                    Device,
                    {"name": "reverse-relation-device"},
                    values,
                )

                self.assertFalse(assessment.allowed)

    def test_a_reverse_relation_does_not_refuse_an_independent_permission_arm(self):
        """One conservative arm stays false without overriding another matching grant."""
        from dcim.models import Device

        site, _manufacturer, device_type, role = make_dcim_objects("ReverseArm")
        user = user_with_object_permission(
            "assess-reverse-independent-arm",
            [
                (Device, ["add"], {"contacts__isnull": True}),
                (Device, ["add"], {"name": "reverse-arm-device"}),
            ],
        )

        assessment = assess_permission_scoped_save(
            user,
            Device,
            {"name": "reverse-arm-device"},
            {"site": site, "device_type": device_type, "role": role},
        )

        self.assertTrue(assessment.allowed)

    def test_invalid_prospective_relation_input_fails_closed(self):
        """The interface validates relation metadata even for an unconstrained grant."""
        from dcim.models import Device, Rack

        site, _manufacturer, device_type, role = make_dcim_objects("InvalidRelation")
        rack = Rack(name="planned-rack", site=site)
        user = user_with_object_permission("assess-invalid-related", [(Device, ["add"], None)])
        cases = (
            ("missing", rack, {}),
            ("tags", rack, {}),
            ("rack", role, {}),
            ("rack", "not-a-model", {}),
            ("rack", rack, {"rack": Rack.objects.create(name="stored-rack", site=site)}),
        )

        for relation_name, relation, extra_values in cases:
            with self.subTest(relation_name=relation_name, relation=relation):
                assessment = assess_permission_scoped_save(
                    user,
                    Device,
                    {"name": "invalid-relation-device"},
                    {"site": site, "device_type": device_type, "role": role, **extra_values},
                    prospective_relations={relation_name: relation},
                )
                self.assertFalse(assessment.allowed)


class SavePermissionScopedObjectTest(TestCase):
    """Create, update, keep and reject, each inside the caller's object scope."""

    def setUp(self):
        self.profile = ImportProfile.objects.create(name="Writer Profile")
        self.other = ImportProfile.objects.create(name="Writer Other Profile")

    def _lookup(self, profile=None):
        return {"profile": profile or self.profile, "source_make": "Acme", "source_model": "Widget"}

    def test_a_create_inside_the_scope_is_saved(self):
        user = user_with_object_permission("writer-add", [(DeviceTypeMapping, ["add"], {"profile": self.profile.pk})])

        result = save_permission_scoped_object(
            user, DeviceTypeMapping, self._lookup(), {"netbox_manufacturer_slug": "acme"}
        )

        self.assertTrue(result.created)
        self.assertEqual(result.instance.netbox_manufacturer_slug, "acme")

    def test_a_create_outside_the_scope_writes_nothing(self):
        """The bare add permission passes; only the saved instance reveals the constraint."""
        user = user_with_object_permission(
            "writer-add-out", [(DeviceTypeMapping, ["add"], {"profile": self.profile.pk})]
        )

        with self.assertRaises(ObjectPermissionDenied):
            save_permission_scoped_object(user, DeviceTypeMapping, self._lookup(self.other), {})

        self.assertFalse(DeviceTypeMapping.objects.filter(profile=self.other).exists())

    def test_a_create_can_use_a_value_derived_during_save(self):
        """The saved-row authority sees a digest that does not exist on a raw candidate."""
        from core.models import ObjectType

        field_key = termination_field_key(device="device-a", cards="", port="port-a", kind="interface")
        user = user_with_object_permission(
            "writer-derived-value",
            [(TerminationResolution, ["add"], {"field_key_digest": index_digest(field_key)})],
        )

        result = save_permission_scoped_object(
            user,
            TerminationResolution,
            {"profile": self.profile, "task_type": SELECT_TERMINATION_TASK, "field_key": field_key},
            {
                "selected_object_type": ObjectType.objects.get_for_model(ImportProfile),
                "selected_object_id": self.profile.pk,
                "selected_display_name": "Selected object",
            },
        )

        self.assertTrue(result.created)
        self.assertEqual(result.instance.field_key_digest, index_digest(field_key))

    def test_an_update_needs_the_change_permission_not_add(self):
        user = user_with_object_permission("writer-add-only", [(DeviceTypeMapping, ["add"], None)])
        DeviceTypeMapping.objects.create(**self._lookup(), netbox_manufacturer_slug="before")

        with self.assertRaises(ObjectPermissionDenied):
            save_permission_scoped_object(
                user, DeviceTypeMapping, self._lookup(), {"netbox_manufacturer_slug": "after"}
            )

        self.assertEqual(DeviceTypeMapping.objects.get(**self._lookup()).netbox_manufacturer_slug, "before")

    def test_change_alone_updates_an_existing_row(self):
        """A user who may change but not add still has to be able to edit what exists."""
        user = user_with_object_permission("writer-change", [(DeviceTypeMapping, ["change"], None)])
        DeviceTypeMapping.objects.create(**self._lookup(), netbox_manufacturer_slug="before")

        result = save_permission_scoped_object(
            user, DeviceTypeMapping, self._lookup(), {"netbox_manufacturer_slug": "after"}
        )

        self.assertFalse(result.created)
        self.assertEqual(DeviceTypeMapping.objects.get(**self._lookup()).netbox_manufacturer_slug, "after")

    def test_an_update_cannot_move_a_row_out_of_the_scope(self):
        """The check after the save is what catches this; the one before it cannot."""
        user = user_with_object_permission(
            "writer-move", [(DeviceTypeMapping, ["change"], {"netbox_manufacturer_slug": "inside"})]
        )
        DeviceTypeMapping.objects.create(**self._lookup(), netbox_manufacturer_slug="inside")

        with self.assertRaises(ObjectPermissionDenied):
            save_permission_scoped_object(
                user, DeviceTypeMapping, self._lookup(), {"netbox_manufacturer_slug": "outside"}
            )

        self.assertEqual(DeviceTypeMapping.objects.get(**self._lookup()).netbox_manufacturer_slug, "inside")

    def test_keep_returns_the_existing_row_untouched(self):
        user = user_with_object_permission("writer-keep", [(DeviceTypeMapping, ["view"], None)])
        DeviceTypeMapping.objects.create(**self._lookup(), netbox_manufacturer_slug="before")

        result = save_permission_scoped_object(
            user, DeviceTypeMapping, self._lookup(), {"netbox_manufacturer_slug": "after"}, on_existing="keep"
        )

        self.assertFalse(result.created)
        self.assertEqual(DeviceTypeMapping.objects.get(**self._lookup()).netbox_manufacturer_slug, "before")

    def test_keep_still_needs_the_view_permission(self):
        """Handing back someone else's row exposes it, so reuse is scoped too."""
        user = user_with_object_permission(
            "writer-keep-out", [(DeviceTypeMapping, ["view"], {"profile": self.other.pk})]
        )
        DeviceTypeMapping.objects.create(**self._lookup(), netbox_manufacturer_slug="before")

        with self.assertRaises(ObjectPermissionDenied):
            save_permission_scoped_object(user, DeviceTypeMapping, self._lookup(), {}, on_existing="keep")

    def test_reject_refuses_an_existing_row(self):
        user = user_with_object_permission("writer-reject", [(DeviceTypeMapping, ["add", "change"], None)])
        DeviceTypeMapping.objects.create(**self._lookup(), netbox_manufacturer_slug="before")

        with self.assertRaises(ObjectPermissionDenied):
            save_permission_scoped_object(
                user, DeviceTypeMapping, self._lookup(), {"netbox_manufacturer_slug": "after"}, on_existing="reject"
            )

        self.assertEqual(DeviceTypeMapping.objects.get(**self._lookup()).netbox_manufacturer_slug, "before")

    def test_an_overlength_value_is_refused_before_the_database_sees_it(self):
        user = user_with_object_permission("writer-long", [(DeviceTypeMapping, ["add"], None)])

        with self.assertRaisesMessage(
            ValidationError,
            "Device Type Mapping netbox_manufacturer_slug cannot exceed 100 characters.",
        ):
            save_permission_scoped_object(
                user, DeviceTypeMapping, self._lookup(), {"netbox_manufacturer_slug": "s" * 300}
            )

        self.assertFalse(DeviceTypeMapping.objects.filter(**self._lookup()).exists())


class PolicyWriteHoldsTheProfileTest(TestCase):
    """A policy write serializes against an executing import, and says so in its own SQL."""

    def setUp(self):
        """Create the profile whose policy row the write belongs to."""
        self.profile = ImportProfile.objects.create(name="Policy Lock Profile")

    def _profile_locks(self, captured) -> list[str]:
        """Return the statements that lock the profile row on its own, without a policy-row join."""
        table = ImportProfile._meta.db_table
        return [
            query["sql"]
            for query in captured.captured_queries
            if "FOR UPDATE" in query["sql"]
            and f'FROM "{table}"' in query["sql"]
            and DeviceTypeMapping._meta.db_table not in query["sql"]
        ]

    def test_a_policy_create_takes_the_profile_lock_itself(self):
        """`Meta.ordering` joins the profile today, so an ordering change would drop the lock."""
        with CaptureQueriesContext(connection) as captured:
            save_permission_scoped_object(
                None,
                DeviceTypeMapping,
                {"profile": self.profile, "source_make": "Dell", "source_model": "R660"},
                {"netbox_manufacturer_slug": "dell", "netbox_device_type_slug": "dell-r660"},
            )

        self.assertEqual(len(self._profile_locks(captured)), 1, captured.captured_queries)

    def test_a_policy_update_takes_the_profile_lock_itself(self):
        """An update of an existing row touches no parent row, so nothing else would serialize it."""
        save_permission_scoped_object(
            None,
            DeviceTypeMapping,
            {"profile": self.profile, "source_make": "Dell", "source_model": "R660"},
            {"netbox_manufacturer_slug": "dell", "netbox_device_type_slug": "dell-r660"},
        )

        with CaptureQueriesContext(connection) as captured:
            save_permission_scoped_object(
                None,
                DeviceTypeMapping,
                {"profile": self.profile, "source_make": "Dell", "source_model": "R660"},
                {"netbox_manufacturer_slug": "dell", "netbox_device_type_slug": "dell-r760"},
            )

        self.assertEqual(len(self._profile_locks(captured)), 1, captured.captured_queries)

    def test_a_policy_write_for_a_deleted_profile_is_refused(self):
        """The lock reads the profile again, so a row whose profile is gone cannot be written."""
        gone = ImportProfile.objects.create(name="Deleted Policy Profile")
        primary_key = gone.pk
        gone.delete()
        gone.pk = primary_key

        with self.assertRaises(ImportProfile.DoesNotExist):
            save_permission_scoped_object(
                None,
                DeviceTypeMapping,
                {"profile": gone, "source_make": "Dell", "source_model": "R660"},
                {"netbox_manufacturer_slug": "dell", "netbox_device_type_slug": "dell-r660"},
            )

    def test_a_write_outside_the_policy_tables_takes_no_profile_lock(self):
        """A NetBox model has no import profile, so the seam has nothing to serialize it against."""
        from dcim.models import Manufacturer

        with CaptureQueriesContext(connection) as captured:
            save_permission_scoped_object(None, Manufacturer, {"slug": "seam-mfg"}, {"name": "Seam Mfg"})

        self.assertEqual(self._profile_locks(captured), [])


class SavePermissionScopedObjectConcurrencyTest(TransactionTestCase):
    """Concurrent inserts resolve through the requested existing-row policy."""

    def test_a_concurrent_create_is_resolved_through_keep_policy(self):
        """A policy write holds its profile, so only a write outside the policy tables can race."""
        from threading import Event, current_thread

        from dcim.models import Manufacturer
        from django.db.models.signals import pre_save

        lookup = {"slug": "concurrent-writer"}
        user = user_with_object_permission("writer-concurrent-keep", [(Manufacturer, ["add", "view"], None)])
        insert_started = Event()
        competing_insert_finished = Event()
        request_thread = current_thread()

        def pause_before_insert(sender, instance, **kwargs):
            if current_thread() is request_thread and instance.slug == lookup["slug"]:
                insert_started.set()
                self.assertTrue(competing_insert_finished.wait(timeout=10))

        pre_save.connect(pause_before_insert, sender=Manufacturer, weak=False)
        self.addCleanup(pre_save.disconnect, pause_before_insert, sender=Manufacturer)

        def insert_competing_row():
            self.assertTrue(insert_started.wait(timeout=10))
            try:
                Manufacturer.objects.create(**lookup, name="Winner")
            finally:
                competing_insert_finished.set()

        with run_on_separate_connection(insert_competing_row):
            result = save_permission_scoped_object(user, Manufacturer, lookup, {"name": "Request"}, on_existing="keep")

        self.assertFalse(result.created)
        self.assertEqual(result.instance.name, "Winner")
        self.assertEqual(Manufacturer.objects.filter(**lookup).count(), 1)


class DeletePermissionScopedObjectsTest(TestCase):
    """A refused row leaves the whole set intact."""

    def setUp(self):
        self.profile = ImportProfile.objects.create(name="Delete Profile")
        self.other = ImportProfile.objects.create(name="Delete Other Profile")

    def test_every_row_in_scope_is_deleted(self):
        user = user_with_object_permission("delete-all", [(DeviceTypeMapping, ["delete"], None)])
        DeviceTypeMapping.objects.create(profile=self.profile, source_make="A", source_model="B")
        DeviceTypeMapping.objects.create(profile=self.profile, source_make="C", source_model="D")

        deleted = delete_permission_scoped_objects(user, DeviceTypeMapping.objects.filter(profile=self.profile))

        self.assertEqual(deleted, 2)
        self.assertFalse(DeviceTypeMapping.objects.filter(profile=self.profile).exists())

    def test_one_refused_row_leaves_the_whole_set(self):
        """Checking every row before deleting any is what makes this all-or-nothing."""
        user = user_with_object_permission("delete-part", [(DeviceTypeMapping, ["delete"], {"source_make": "A"})])
        DeviceTypeMapping.objects.create(profile=self.profile, source_make="A", source_model="B")
        DeviceTypeMapping.objects.create(profile=self.profile, source_make="C", source_model="D")

        with self.assertRaises(ObjectPermissionDenied):
            delete_permission_scoped_objects(user, DeviceTypeMapping.objects.filter(profile=self.profile))

        self.assertEqual(DeviceTypeMapping.objects.filter(profile=self.profile).count(), 2)
