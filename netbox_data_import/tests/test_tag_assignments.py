# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Import Profiles and AI backends hold their tags as NetBox's standard tag assignments."""

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from extras.models import Tag, TaggedItem

from netbox_data_import.models import ImportProfile, InferenceBackend
from netbox_data_import.tables import ImportProfileTable, InferenceBackendTable

INFERENCE_ALLOWLIST = ["https://backend.example.invalid:443"]


def _backend(backend_key="tagged-backend"):
    return InferenceBackend.objects.create(
        backend_key=backend_key,
        display_name="Tagged backend",
        adapter_type="openai_compatible",
        api_root=INFERENCE_ALLOWLIST[0],
        model="tag-model",
        authentication="bearer",
        response_mode="prompt_json",
        credential_reference={"backend": "vault_kv_v2", "mount": "secret", "path": "inference/tag", "field": "k"},
    )


@override_settings(PLUGINS_CONFIG={"netbox_data_import": {"inference_backend_origin_allowlist": INFERENCE_ALLOWLIST}})
class TagAssignmentTest(TestCase):
    """A tag assignment is an ``extras.TaggedItem`` row, which netbox-branching replicates."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_superuser("tag-user", "tag@example.invalid", "testpass")
        cls.alpha = Tag.objects.create(name="Alpha", slug="alpha")
        cls.bravo = Tag.objects.create(name="Bravo", slug="bravo")

    def setUp(self):
        self.client.force_login(self.user)

    def test_a_tag_assignment_is_a_tagged_item(self):
        profile = ImportProfile.objects.create(name="Tagged profile")
        backend = _backend()

        for instance in (profile, backend):
            with self.subTest(model=type(instance).__name__):
                instance.tags.add(self.alpha)

                self.assertEqual(
                    list(
                        TaggedItem.objects.filter(
                            content_type=ContentType.objects.get_for_model(instance), object_id=instance.pk
                        ).values_list("tag__slug", flat=True)
                    ),
                    ["alpha"],
                )

    def test_the_rest_api_assigns_tags_by_name(self):
        profile = ImportProfile.objects.create(name="API tagged profile")
        backend = _backend()
        targets = (
            (profile, "plugins-api:netbox_data_import-api:importprofile-detail"),
            (backend, "plugins-api:netbox_data_import-api:inferencebackend-detail"),
        )

        for instance, route in targets:
            with self.subTest(model=type(instance).__name__):
                response = self.client.patch(
                    reverse(route, args=[instance.pk]),
                    data={"tags": [{"name": "Alpha"}, {"name": "Bravo"}]},
                    content_type="application/json",
                )

                self.assertEqual(response.status_code, 200, response.content)
                self.assertEqual(sorted(tag["slug"] for tag in response.json()["tags"]), ["alpha", "bravo"])
                self.assertEqual(sorted(instance.tags.values_list("slug", flat=True)), ["alpha", "bravo"])

    def test_the_profile_list_filters_by_tag(self):
        tagged = ImportProfile.objects.create(name="Filter tagged profile")
        ImportProfile.objects.create(name="Filter untagged profile")
        tagged.tags.add(self.bravo)

        response = self.client.get(reverse("plugins:netbox_data_import:importprofile_list"), {"tag": "bravo"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context["table"].data), [tagged])

    def test_the_ui_form_assigns_tags(self):
        profile = ImportProfile.objects.create(name="Form tagged profile")

        response = self.client.post(
            reverse("plugins:netbox_data_import:importprofile_edit", args=[profile.pk]),
            data={
                "name": profile.name,
                "source_adapter": profile.source_adapter,
                "sheet_name": "Inventory",
                "source_id_column": "Source ID",
                "preview_view_mode": "rows",
                "primary_contact_lookup_field": "email",
                "tags": [self.alpha.pk, self.bravo.pk],
            },
        )

        self.assertEqual(response.status_code, 302, response.content)
        self.assertEqual(sorted(profile.tags.values_list("slug", flat=True)), ["alpha", "bravo"])

    def test_graphql_returns_profile_tags(self):
        profile = ImportProfile.objects.create(name="GraphQL tagged profile")
        profile.tags.add(self.alpha)

        response = self.client.post(
            "/graphql/",
            data={"query": f"{{ import_profile(id: {profile.pk}) {{ tags {{ name }} }} }}"},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("errors", response.json(), response.json())
        self.assertEqual(response.json()["data"]["import_profile"]["tags"], [{"name": "Alpha"}])

    def test_the_tables_offer_a_tags_column(self):
        profile = ImportProfile.objects.create(name="Table tagged profile")
        backend = _backend()
        profile.tags.add(self.alpha)
        backend.tags.add(self.alpha)

        for table_class, instance in ((ImportProfileTable, profile), (InferenceBackendTable, backend)):
            with self.subTest(table=table_class.__name__):
                table = table_class(type(instance).objects.filter(pk=instance.pk))
                table.columns.show("tags")

                self.assertIn("Alpha", str(table.rows[0].get_cell("tags")))

    def test_the_system_checks_report_no_clash(self):
        call_command("check", "netbox_data_import", fail_level="WARNING")
