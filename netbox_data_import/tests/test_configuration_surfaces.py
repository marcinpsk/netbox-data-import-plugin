# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Configuration surfaces introduced for trace and inference workflows."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from io import BytesIO
from threading import Barrier, local
from unittest.mock import patch

import yaml
from django.contrib.auth import get_user_model
from django.db import DatabaseError, transaction
from django.db.models.signals import post_delete, post_save
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from dcim.models import Cable, Device, Interface

from netbox_data_import.forms import InferenceBackendForm
from netbox_data_import.api.serializers import PolicySectionSerializer
from netbox_data_import.models import (
    CableClassMapping,
    CableImportSource,
    ColumnMapping,
    ColumnTransformRule,
    ImportProfile,
    InferenceBackend,
    locked_profile_policy,
    SourceResolution,
)
from netbox_data_import.tests.helpers import (
    make_dcim_objects,
    run_on_separate_connection,
    user_with_object_permission,
)


User = get_user_model()

INFERENCE_ALLOWLIST = ["https://backend.example.invalid:443"]
INFERENCE_REFERENCE = {
    "backend": "vault_kv_v2",
    "mount": "secret",
    "path": "inference/backend",
    "field": "api_key",
}


class CableClassMappingAPITest(TestCase):
    """Manage CableClass policy through the registered REST resource."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser("cable-class-api", "cable-class@example.invalid", "testpass")
        cls.trace_profile = ImportProfile.objects.create(
            name="Trace API profile",
            source_adapter="trace_workbook",
            adapter_config={},
        )
        cls.other_trace_profile = ImportProfile.objects.create(
            name="Other trace API profile",
            source_adapter="trace_workbook",
            adapter_config={},
        )
        cls.flat_profile = ImportProfile.objects.create(name="Flat API profile", adapter_config={})

    def setUp(self):
        self.client.force_login(self.user)
        self.list_url = reverse("plugins-api:netbox_data_import-api:cableclassmapping-list")

    def test_create_read_update_and_delete_mapping(self):
        response = self.client.post(
            self.list_url,
            data={
                "profile": self.trace_profile.pk,
                "cable_class": "Copper patch",
                "cable_type_resolved": True,
                "cable_type": "cat6",
                "cable_profile_resolved": True,
                "cable_profile": "single-1c1p",
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 201, response.content)
        mapping = CableClassMapping.objects.get(profile=self.trace_profile, cable_class="Copper patch")
        detail_url = reverse(
            "plugins-api:netbox_data_import-api:cableclassmapping-detail",
            args=[mapping.pk],
        )
        self.assertEqual(self.client.get(detail_url).json()["cable_type"], "cat6")

        response = self.client.patch(
            detail_url,
            data={"cable_type_resolved": True, "cable_type": None},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content)
        mapping.refresh_from_db()
        self.assertTrue(mapping.cable_type_resolved)
        self.assertIsNone(mapping.cable_type)
        self.assertEqual(self.client.delete(detail_url).status_code, 204)
        self.assertFalse(CableClassMapping.objects.filter(pk=mapping.pk).exists())

    def test_create_persists_the_model_normalization_of_a_blank_choice(self):
        response = self.client.post(
            self.list_url,
            data={
                "profile": self.trace_profile.pk,
                "cable_class": "No selected type",
                "cable_type_resolved": True,
                "cable_type": "",
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 201, response.content)
        mapping = CableClassMapping.objects.get(profile=self.trace_profile, cable_class="No selected type")
        self.assertIsNone(mapping.cable_type)

    def test_update_persists_the_model_normalization_of_a_blank_choice(self):
        mapping = CableClassMapping.objects.create(profile=self.trace_profile, cable_class="No selected profile")
        detail_url = reverse("plugins-api:netbox_data_import-api:cableclassmapping-detail", args=[mapping.pk])

        response = self.client.patch(
            detail_url,
            data={"cable_profile_resolved": True, "cable_profile": ""},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content)
        mapping.refresh_from_db()
        self.assertIsNone(mapping.cable_profile)

    def test_profile_filter_returns_only_matching_mappings(self):
        expected = CableClassMapping.objects.create(profile=self.trace_profile, cable_class="Expected")
        CableClassMapping.objects.create(profile=self.other_trace_profile, cable_class="Hidden")

        response = self.client.get(self.list_url, {"profile_id": self.trace_profile.pk})

        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["id"] for row in response.json()["results"]], [expected.pk])

    def test_flat_profile_rejects_cable_class_policy(self):
        response = self.client.post(
            self.list_url,
            data={"profile": self.flat_profile.pk, "cable_class": "Not applicable"},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("profile", response.json())
        self.assertFalse(CableClassMapping.objects.filter(profile=self.flat_profile).exists())

    def scoped_operator(self, actions):
        """Log in an operator whose writes are limited to one profile."""
        operator = user_with_object_permission(
            f"cable-class-{'-'.join(actions)}",
            [
                (CableClassMapping, ["view"], {}),
                (CableClassMapping, actions, {"profile_id": self.trace_profile.pk}),
                (ImportProfile, ["view"], {}),
            ],
        )
        self.client.force_login(operator)

    def test_constrained_add_cannot_create_under_another_profile(self):
        self.scoped_operator(["add"])

        response = self.client.post(
            self.list_url,
            data={"profile": self.other_trace_profile.pk, "cable_class": "Outside add scope"},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403, response.content)
        self.assertFalse(
            CableClassMapping.objects.filter(
                profile=self.other_trace_profile,
                cable_class="Outside add scope",
            ).exists()
        )

    def test_constrained_change_cannot_update_another_profile(self):
        mapping = CableClassMapping.objects.create(profile=self.other_trace_profile, cable_class="Outside change scope")
        self.scoped_operator(["change"])
        detail_url = reverse("plugins-api:netbox_data_import-api:cableclassmapping-detail", args=[mapping.pk])

        response = self.client.patch(
            detail_url,
            data={"cable_class": "Changed outside scope"},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403, response.content)
        mapping.refresh_from_db()
        self.assertEqual(mapping.cable_class, "Outside change scope")

    def test_constrained_delete_cannot_remove_another_profile(self):
        mapping = CableClassMapping.objects.create(profile=self.other_trace_profile, cable_class="Outside delete scope")
        self.scoped_operator(["delete"])
        detail_url = reverse("plugins-api:netbox_data_import-api:cableclassmapping-detail", args=[mapping.pk])

        response = self.client.delete(detail_url)

        self.assertEqual(response.status_code, 403, response.content)
        self.assertTrue(CableClassMapping.objects.filter(pk=mapping.pk).exists())


class SourceResolutionAPIPermissionTest(TestCase):
    """Apply object-permission scope to every Source Resolution write."""

    @classmethod
    def setUpTestData(cls):
        cls.allowed_profile = ImportProfile.objects.create(
            name="Allowed resolution API profile",
            source_adapter="flat_workbook",
            adapter_config={},
        )
        cls.other_profile = ImportProfile.objects.create(
            name="Other resolution API profile",
            source_adapter="flat_workbook",
            adapter_config={},
        )

    def setUp(self):
        self.list_url = reverse("plugins-api:netbox_data_import-api:sourceresolution-list")

    def scoped_operator(self, actions):
        """Log in an operator whose writes are limited to one profile."""
        operator = user_with_object_permission(
            f"source-resolution-{'-'.join(actions)}",
            [
                (SourceResolution, ["view"], {}),
                (SourceResolution, actions, {"profile_id": self.allowed_profile.pk}),
                (ImportProfile, ["view"], {}),
            ],
        )
        self.client.force_login(operator)

    def resolution_data(self, profile):
        """Return one valid Source Resolution request for the profile."""
        return {
            "profile": profile.pk,
            "source_id": "SR-PERMISSION",
            "source_column": "Name",
            "original_value": "Before",
            "resolved_fields": {"device_name": "After"},
        }

    def test_constrained_add_cannot_create_under_another_profile(self):
        self.scoped_operator(["add"])

        response = self.client.post(
            self.list_url,
            data=self.resolution_data(self.other_profile),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403, response.content)
        self.assertFalse(SourceResolution.objects.filter(profile=self.other_profile).exists())

    def test_constrained_change_cannot_update_another_profile(self):
        resolution = SourceResolution.objects.create(
            profile=self.other_profile,
            source_id="SR-PERMISSION",
            source_column="Name",
            original_value="Before",
            resolved_fields={"device_name": "After"},
        )
        self.scoped_operator(["change"])
        detail_url = reverse("plugins-api:netbox_data_import-api:sourceresolution-detail", args=[resolution.pk])

        response = self.client.patch(
            detail_url,
            data={"original_value": "Changed outside scope"},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403, response.content)
        resolution.refresh_from_db()
        self.assertEqual(resolution.original_value, "Before")

    def test_constrained_delete_cannot_remove_another_profile(self):
        resolution = SourceResolution.objects.create(
            profile=self.other_profile,
            source_id="SR-PERMISSION",
            source_column="Name",
            original_value="Before",
            resolved_fields={"device_name": "After"},
        )
        self.scoped_operator(["delete"])
        detail_url = reverse("plugins-api:netbox_data_import-api:sourceresolution-detail", args=[resolution.pk])

        response = self.client.delete(detail_url)

        self.assertEqual(response.status_code, 403, response.content)
        self.assertTrue(SourceResolution.objects.filter(pk=resolution.pk).exists())


class CableClassMappingAPIPolicyLockTest(TransactionTestCase):
    """Every REST policy mutation must serialize against import execution."""

    def setUp(self):
        self.profile = ImportProfile.objects.create(
            name="Cable class API lock profile",
            source_adapter="trace_workbook",
            adapter_config={},
        )
        self.mapping = CableClassMapping.objects.create(profile=self.profile, cable_class="Before")
        user = User.objects.create_superuser("cable-class-lock", "cable-class-lock@example.invalid", "testpass")
        self.client.force_login(user)
        self.list_url = reverse("plugins-api:netbox_data_import-api:cableclassmapping-list")
        self.detail_url = reverse(
            "plugins-api:netbox_data_import-api:cableclassmapping-detail",
            args=[self.mapping.pk],
        )

    def lock_state_during_write(self, signal, request):
        """Return whether another connection can lock the profile during one API write."""
        seen = []

        def probe_from_another_connection(sender, instance, **kwargs):
            if seen or instance.profile_id != self.profile.pk:
                return

            def probe():
                try:
                    with transaction.atomic():
                        ImportProfile.objects.select_for_update(nowait=True).get(pk=self.profile.pk)
                    seen.append("unlocked")
                except DatabaseError:
                    seen.append("locked")

            with run_on_separate_connection(probe):
                pass

        signal.connect(probe_from_another_connection, sender=CableClassMapping)
        try:
            response = request()
        finally:
            signal.disconnect(probe_from_another_connection, sender=CableClassMapping)
        return seen, response

    def test_create_holds_the_profile_policy_lock(self):
        seen, response = self.lock_state_during_write(
            post_save,
            lambda: self.client.post(
                self.list_url,
                data={"profile": self.profile.pk, "cable_class": "Created"},
                content_type="application/json",
            ),
        )

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(seen, ["locked"])

    def test_update_holds_the_profile_policy_lock(self):
        seen, response = self.lock_state_during_write(
            post_save,
            lambda: self.client.patch(
                self.detail_url,
                data={"cable_class": "Updated"},
                content_type="application/json",
            ),
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(seen, ["locked"])

    def test_delete_holds_the_profile_policy_lock(self):
        seen, response = self.lock_state_during_write(post_delete, lambda: self.client.delete(self.detail_url))

        self.assertEqual(response.status_code, 204, response.content)
        self.assertEqual(seen, ["locked"])


class PolicyAPICreateSerializationTest(TransactionTestCase):
    """Concurrent REST creates must recheck cross-row policy invariants."""

    def setUp(self):
        self.profile = ImportProfile.objects.create(name="Concurrent policy API profile", adapter_config={})
        self.user = User.objects.create_superuser(
            "concurrent-policy-api",
            "concurrent-policy-api@example.invalid",
            "testpass",
        )

    def test_conflicting_creates_revalidate_after_the_profile_lock(self):
        """Only one request can assign a target when both first validate an empty policy."""
        validation_barrier = Barrier(2)
        cleaned_values_barrier = Barrier(2)
        lock_state = local()
        original_validate = PolicySectionSerializer.validate
        original_model_cleaned_values = PolicySectionSerializer.model_cleaned_values

        def synchronize_initial_validation(serializer, attrs):
            result = original_validate(serializer, attrs)
            if not getattr(serializer, "_initial_validation_synchronized", False):
                serializer._initial_validation_synchronized = True
                validation_barrier.wait(timeout=10)
            return result

        def synchronize_unlocked_cleaned_values(serializer):
            result = original_model_cleaned_values(serializer)
            if not getattr(lock_state, "held", False):
                cleaned_values_barrier.wait(timeout=10)
            return result

        @contextmanager
        def record_profile_lock(*profile_ids):
            with locked_profile_policy(*profile_ids):
                lock_state.held = True
                try:
                    yield
                finally:
                    lock_state.held = False

        requests = (
            (
                reverse("plugins-api:netbox_data_import-api:columnmapping-list"),
                {
                    "profile": self.profile.pk,
                    "source_column": "Direct name",
                    "target_field": "device_name",
                },
            ),
            (
                reverse("plugins-api:netbox_data_import-api:columntransformrule-list"),
                {
                    "profile": self.profile.pk,
                    "source_column": "Parsed name",
                    "pattern": "(.+)",
                    "group_1_target": "device_name",
                },
            ),
        )

        def create_policy(request):
            url, data = request
            client = self.client_class()
            client.force_login(User.objects.get(pk=self.user.pk))
            response = client.post(url, data=data, content_type="application/json")
            return response.status_code, response.json()

        with (
            patch.object(PolicySectionSerializer, "validate", synchronize_initial_validation),
            patch.object(PolicySectionSerializer, "model_cleaned_values", synchronize_unlocked_cleaned_values),
            patch("netbox_data_import.api.views.locked_profile_policy", record_profile_lock),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            responses = list(executor.map(create_policy, requests))

        self.assertEqual(sorted(status for status, _body in responses), [201, 400], responses)
        created = ColumnMapping.objects.filter(profile=self.profile).count()
        created += ColumnTransformRule.objects.filter(profile=self.profile).count()
        self.assertEqual(created, 1)


@override_settings(PLUGINS_CONFIG={"netbox_data_import": {"inference_backend_origin_allowlist": INFERENCE_ALLOWLIST}})
class InferenceBackendAPITest(TestCase):
    """Manage inference configuration without disclosing its credential reference."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser("inference-api", "inference@example.invalid", "testpass")

    def setUp(self):
        self.client.force_login(self.user)
        self.list_url = reverse("plugins-api:netbox_data_import-api:inferencebackend-list")

    def backend_data(self, **overrides):
        """Return one valid API payload using the same fields as the model form."""
        return {
            "backend_key": "primary",
            "display_name": "Primary inference backend",
            "adapter_type": "openai_compatible",
            "api_root": INFERENCE_ALLOWLIST[0],
            "model": "inference-model",
            "authentication": "bearer",
            "response_mode": "prompt_json",
            "credential_reference": INFERENCE_REFERENCE,
            "connect_timeout": 5,
            "read_timeout": 60,
            "enabled": False,
            "tags": [],
        } | overrides

    def test_create_and_update_use_a_write_only_credential_reference(self):
        response = self.client.post(self.list_url, data=self.backend_data(), content_type="application/json")

        self.assertEqual(response.status_code, 201, response.content)
        self.assertNotIn("credential_reference", response.json())
        backend = InferenceBackend.objects.get(backend_key="primary")
        self.assertEqual(backend.credential_reference, INFERENCE_REFERENCE)

        detail_url = reverse("plugins-api:netbox_data_import-api:inferencebackend-detail", args=[backend.pk])
        replacement = {**INFERENCE_REFERENCE, "path": "inference/replacement"}
        response = self.client.patch(
            detail_url,
            data={"display_name": "Updated backend", "credential_reference": replacement},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertNotIn("credential_reference", response.json())
        backend.refresh_from_db()
        self.assertEqual((backend.display_name, backend.credential_reference), ("Updated backend", replacement))
        self.assertNotIn("credential_reference", self.client.get(detail_url).json())

    def test_endpoint_rejects_configuration_the_form_rejects(self):
        response = self.client.post(
            self.list_url,
            data=self.backend_data(api_root="https://unapproved.example.invalid:443"),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("api_root", response.json())
        self.assertFalse(InferenceBackend.objects.exists())

    def test_options_exposes_the_form_configuration_schema(self):
        response = self.client.options(self.list_url)

        self.assertEqual(response.status_code, 200)
        writable = {name for name, metadata in response.json()["actions"]["POST"].items() if not metadata["read_only"]}
        self.assertEqual(writable, {*InferenceBackendForm.Meta.fields, "custom_fields"})
        self.assertNotIn("target_field", writable)


class CableProvenanceAPITest(TestCase):
    """Read per-Cable import provenance through its audit-only REST resource."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser("provenance-api", "provenance@example.invalid", "testpass")
        cls.profile = ImportProfile.objects.create(
            name="Provenance profile",
            source_adapter="trace_workbook",
            adapter_config={},
        )
        cls.other_profile = ImportProfile.objects.create(
            name="Other provenance profile",
            source_adapter="trace_workbook",
            adapter_config={},
        )
        site, _manufacturer, device_type, role = make_dcim_objects("Provenance API")
        device = Device.objects.create(name="Provenance device", site=site, device_type=device_type, role=role)
        first = Interface.objects.create(device=device, name="eth0", type="1000base-t")
        second = Interface.objects.create(device=device, name="eth1", type="1000base-t")
        third = Interface.objects.create(device=device, name="eth2", type="1000base-t")
        fourth = Interface.objects.create(device=device, name="eth3", type="1000base-t")
        cls.cable = Cable(a_terminations=[first], b_terminations=[second])
        cls.cable.save()
        cls.other_cable = Cable(a_terminations=[third], b_terminations=[fourth])
        cls.other_cable.save()
        cls.provenance = CableImportSource.objects.create(
            cable=cls.cable,
            profile=cls.profile,
            trace_identity='[["device-a","","eth0"],["device-b","","eth1"]]',
            segment_index=1,
            from_text="Device A > eth0",
            to_text="Device B > eth1",
            direction="canonical",
            workbook_fingerprint="a" * 64,
            sheet="Trace Path",
            block_ordinal=2,
            row_start=10,
            row_end=12,
            export_timestamp="2026-09-01T12:00:00+00:00",
        )
        cls.other_provenance = CableImportSource.objects.create(
            cable=cls.other_cable,
            profile=cls.other_profile,
            trace_identity='[["device-b","","eth1"],["device-c","","eth2"]]',
            segment_index=0,
        )

    def setUp(self):
        self.client.force_login(self.user)
        self.list_url = reverse("plugins-api:netbox_data_import-api:cableimportsource-list")
        self.detail_url = reverse(
            "plugins-api:netbox_data_import-api:cableimportsource-detail",
            args=[self.provenance.pk],
        )

    def test_list_and_detail_return_the_complete_provenance_record(self):
        response = self.client.get(self.list_url, {"cable_id": self.cable.pk})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 1)
        row = response.json()["results"][0]
        self.assertEqual(row, self.client.get(self.detail_url).json())
        self.assertEqual(
            set(row),
            {
                "id",
                "cable",
                "profile",
                "trace_identity",
                "segment_index",
                "from_text",
                "to_text",
                "direction",
                "workbook_fingerprint",
                "sheet",
                "block_ordinal",
                "row_start",
                "row_end",
                "export_timestamp",
            },
        )
        self.assertEqual((row["cable"], row["profile"]), (self.cable.pk, self.profile.pk))

    def test_profile_filter_returns_only_matching_provenance(self):
        response = self.client.get(self.list_url, {"profile_id": self.profile.pk})

        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["id"] for row in response.json()["results"]], [self.provenance.pk])

    def test_object_constraint_hides_other_provenance(self):
        viewer = user_with_object_permission(
            "provenance-viewer",
            [(CableImportSource, ["view"], {"profile_id": self.profile.pk})],
        )
        self.client.force_login(viewer)

        response = self.client.get(self.list_url)

        self.assertEqual([row["id"] for row in response.json()["results"]], [self.provenance.pk])
        hidden_url = reverse(
            "plugins-api:netbox_data_import-api:cableimportsource-detail",
            args=[self.other_provenance.pk],
        )
        self.assertEqual(self.client.get(hidden_url).status_code, 404)

    def test_every_write_method_is_refused(self):
        before = list(CableImportSource.objects.values())
        requests = (
            self.client.post(self.list_url, data={}, content_type="application/json"),
            self.client.put(self.detail_url, data={}, content_type="application/json"),
            self.client.patch(self.detail_url, data={}, content_type="application/json"),
            self.client.delete(self.detail_url),
        )

        self.assertEqual([response.status_code for response in requests], [405, 405, 405, 405])
        self.assertEqual(list(CableImportSource.objects.values()), before)


class CableClassMappingGraphQLTest(TestCase):
    """Expose CableClass configuration without exposing workflow state."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser("graphql-api", "graphql@example.invalid", "testpass")
        cls.profile = ImportProfile.objects.create(
            name="GraphQL trace profile",
            source_adapter="trace_workbook",
            adapter_config={},
        )
        cls.mapping = CableClassMapping.objects.create(
            profile=cls.profile,
            cable_class="Copper patch",
            cable_type_resolved=True,
            cable_type="cat6",
            cable_profile_resolved=True,
            cable_profile="single-1c1p",
        )

    def setUp(self):
        self.client.force_login(self.user)

    def test_query_returns_cable_class_configuration(self):
        response = self.client.post(
            "/graphql/",
            data={
                "query": """
                    {
                      cable_class_mapping_list {
                        id
                        cable_class
                        cable_type_resolved
                        cable_type
                        cable_profile_resolved
                        cable_profile
                        profile { id name source_adapter adapter_config }
                      }
                    }
                """,
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("errors", response.json(), response.json())
        self.assertEqual(
            response.json()["data"]["cable_class_mapping_list"],
            [
                {
                    "id": str(self.mapping.pk),
                    "cable_class": "Copper patch",
                    "cable_type_resolved": True,
                    "cable_type": "cat6",
                    "cable_profile_resolved": True,
                    "cable_profile": "single-1c1p",
                    "profile": {
                        "id": str(self.profile.pk),
                        "name": "GraphQL trace profile",
                        "source_adapter": "trace_workbook",
                        "adapter_config": {},
                    },
                }
            ],
        )

    def test_schema_exposes_configuration_but_not_workflow_state(self):
        response = self.client.post(
            "/graphql/",
            data={"query": "{ __schema { types { name } queryType { fields { name } } } }"},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("errors", response.json(), response.json())
        schema = response.json()["data"]["__schema"]
        type_names = {item["name"] for item in schema["types"]}
        query_names = {item["name"] for item in schema["queryType"]["fields"]}
        self.assertIn("CableClassMappingType", type_names)
        for model_name in ("ImportPlan", "ImportExecution", "ResolutionProposal", "InferenceBackend"):
            with self.subTest(model_name=model_name):
                self.assertNotIn(model_name, type_names)
                self.assertNotIn(f"{model_name}Type", type_names)

        for field_name in (
            "import_plan",
            "import_plan_list",
            "import_execution",
            "import_execution_list",
            "resolution_proposal",
            "resolution_proposal_list",
            "inference_backend",
            "inference_backend_list",
        ):
            with self.subTest(field_name=field_name):
                self.assertNotIn(field_name, query_names)

    def test_object_constraint_limits_the_mapping_query(self):
        other_profile = ImportProfile.objects.create(
            name="Other GraphQL trace profile",
            source_adapter="trace_workbook",
            adapter_config={},
        )
        CableClassMapping.objects.create(profile=other_profile, cable_class="Hidden mapping")
        viewer = user_with_object_permission(
            "graphql-viewer",
            [(CableClassMapping, ["view"], {"pk": self.mapping.pk})],
        )
        self.client.force_login(viewer)

        response = self.client.post(
            "/graphql/",
            data={"query": "{ cable_class_mapping_list { id cable_class } }"},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("errors", response.json(), response.json())
        self.assertEqual(
            response.json()["data"]["cable_class_mapping_list"],
            [{"id": str(self.mapping.pk), "cable_class": "Copper patch"}],
        )


class ProfileYamlSurfaceTest(TestCase):
    """Round-trip adapter-specific profile policy through the YAML document interface."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser("profile-yaml", "profile-yaml@example.invalid", "testpass")

    def setUp(self):
        self.client.force_login(self.user)

    def test_trace_profile_round_trips_without_instance_local_ids(self):
        source = ImportProfile.objects.create(
            name="Portable trace profile",
            description="Portable trace policy",
            source_adapter="trace_workbook",
            adapter_config={},
        )
        CableClassMapping.objects.create(
            profile=source,
            cable_class="Copper patch",
            cable_type_resolved=True,
            cable_type="cat6",
            cable_profile_resolved=True,
            cable_profile="single-1c1p",
        )

        response = self.client.get(reverse("plugins:netbox_data_import:exportprofile_yaml", kwargs={"pk": source.pk}))

        self.assertEqual(response.status_code, 200)
        document = yaml.safe_load(response.content)
        self.assertEqual(set(document), {"profile", "cable_class_mappings"})
        self.assertFalse(self._keys_ending_in_id(document))
        original_pk = source.pk
        source.delete()
        upload = BytesIO(response.content)
        upload.name = "portable-trace-profile.yaml"

        imported_response = self.client.post(
            reverse("plugins:netbox_data_import:import_profile_yaml"),
            {"yaml_file": upload},
        )

        self.assertEqual(imported_response.status_code, 302, imported_response.content)
        imported = ImportProfile.objects.get(name="Portable trace profile")
        self.assertNotEqual(imported.pk, original_pk)
        self.assertEqual(
            list(
                imported.cable_class_mappings.values(
                    "cable_class",
                    "cable_type_resolved",
                    "cable_type",
                    "cable_profile_resolved",
                    "cable_profile",
                )
            ),
            [
                {
                    "cable_class": "Copper patch",
                    "cable_type_resolved": True,
                    "cable_type": "cat6",
                    "cable_profile_resolved": True,
                    "cable_profile": "single-1c1p",
                }
            ],
        )

    def test_import_rejects_policy_sections_the_adapter_does_not_support(self):
        cases = (
            (
                "Flat profile with trace policy",
                "flat_workbook",
                "cable_class_mappings",
            ),
            (
                "Trace profile with flat policy",
                "trace_workbook",
                "column_mappings",
            ),
        )
        for name, adapter, section in cases:
            with self.subTest(adapter=adapter, section=section):
                document = {
                    "profile": {"name": name, "source_adapter": adapter, "adapter_config": {}},
                    section: [],
                }
                upload = BytesIO(yaml.safe_dump(document).encode())
                upload.name = "unsupported-policy.yaml"

                response = self.client.post(
                    reverse("plugins:netbox_data_import:import_profile_yaml"),
                    {"yaml_file": upload},
                )

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, section)
                self.assertContains(response, "does not apply to source adapter")
                self.assertFalse(ImportProfile.objects.filter(name=name).exists())

    @classmethod
    def _keys_ending_in_id(cls, value):
        """Return instance-local key paths found in one parsed YAML value."""
        if isinstance(value, dict):
            return [
                key for key, item in value.items() if key == "id" or key.endswith("_id") or cls._keys_ending_in_id(item)
            ]
        if isinstance(value, list):
            return [item for item in value if cls._keys_ending_in_id(item)]
        return []
