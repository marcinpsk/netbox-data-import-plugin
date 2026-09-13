# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Configuration surfaces introduced for trace and inference workflows."""

from io import BytesIO

import yaml
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from dcim.models import Cable, Device, Interface

from netbox_data_import.forms import InferenceBackendForm
from netbox_data_import.models import CableClassMapping, CableImportSource, ImportProfile, InferenceBackend
from netbox_data_import.tests.helpers import make_dcim_objects, user_with_object_permission


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
        self.assertIn("CableClassMappingType", {item["name"] for item in schema["types"]})
        normalized_names = {
            item["name"].replace("_", "").lower() for item in [*schema["types"], *schema["queryType"]["fields"]]
        }
        for forbidden in ("ImportPlan", "ImportExecution", "ResolutionProposal", "InferenceBackend"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden.lower(), normalized_names)

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
