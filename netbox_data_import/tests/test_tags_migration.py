# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The upgrade moves profile and backend tags from their own through tables into extras_taggeditem."""

import importlib

from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from extras.models import Tag, TaggedItem

from netbox_data_import.models import ImportProfile, InferenceBackend
from netbox_data_import.tests.helpers import migrate_plugin_to_leaf

APP = "netbox_data_import"
BEFORE = (APP, "0039_remove_job_plan_copies")
DATA_MOVE = "0040_move_tags_to_tagged_items"
AFTER = (APP, "0041_remove_importprofile_tags_and_more")
THROUGH_TABLES = ("netbox_data_import_importprofile_tags", "netbox_data_import_inferencebackend_tags")
BACKEND_FIELDS = {
    "display_name": "Migrated backend",
    "adapter_type": "openai_compatible",
    "api_root": "https://backend.example.invalid:443",
    "model": "migration-model",
    "authentication": "bearer",
    "response_mode": "prompt_json",
    "credential_reference": {},
}


def _tagged_items(model):
    content_type = ContentType.objects.get_for_model(model)
    return set(TaggedItem.objects.filter(content_type=content_type).values_list("object_id", "tag__slug"))


class TagsMigrationTest(TransactionTestCase):
    """Keep every tag assignment across the upgrade and its rollback."""

    def setUp(self):
        self.addCleanup(migrate_plugin_to_leaf)

    def test_upgrade_moves_every_tag_link_to_a_tagged_item(self):
        executor = MigrationExecutor(connection)
        executor.migrate([BEFORE])
        old_apps = executor.loader.project_state([BEFORE]).apps
        old_tag = old_apps.get_model("extras", "Tag")
        first = old_tag.objects.create(name="First", slug="first")
        second = old_tag.objects.create(name="Second", slug="second")
        profile = old_apps.get_model(APP, "ImportProfile").objects.create(name="Migrated profile")
        profile.tags.add(first, second)
        backend = old_apps.get_model(APP, "InferenceBackend").objects.create(backend_key="migrated", **BACKEND_FIELDS)
        backend.tags.add(second)

        MigrationExecutor(connection).migrate([AFTER])

        self.assertEqual(_tagged_items(ImportProfile), {(profile.pk, "first"), (profile.pk, "second")})
        self.assertEqual(_tagged_items(InferenceBackend), {(backend.pk, "second")})
        # Migration 0038's trigger projects only Cable tags into its column.
        self.assertEqual(self._cable_projection(), [])
        self.assertFalse(set(THROUGH_TABLES) & set(connection.introspection.table_names()))

    def test_rollback_moves_the_tag_assignments_back(self):
        tag = Tag.objects.create(name="Rolled back", slug="rolled-back")
        profile = ImportProfile.objects.create(name="Rolled back profile")
        profile.tags.add(tag)
        backend = InferenceBackend.objects.create(backend_key="rolled-back", **BACKEND_FIELDS)
        backend.tags.add(tag)

        executor = MigrationExecutor(connection)
        executor.migrate([BEFORE])
        old_apps = executor.loader.project_state([BEFORE]).apps

        for model_name, pk in (("ImportProfile", profile.pk), ("InferenceBackend", backend.pk)):
            with self.subTest(model=model_name):
                old_model = old_apps.get_model(APP, model_name)
                self.assertEqual(
                    list(old_model.objects.get(pk=pk).tags.values_list("slug", flat=True)), ["rolled-back"]
                )
        self.assertFalse(TaggedItem.objects.exists())

    def test_the_data_move_alone_rolls_back_to_one_copy_of_each_assignment(self):
        executor = MigrationExecutor(connection)
        executor.migrate([BEFORE])
        old_apps = executor.loader.project_state([BEFORE]).apps
        tag = old_apps.get_model("extras", "Tag").objects.create(name="Round trip", slug="round-trip")
        profile = old_apps.get_model(APP, "ImportProfile").objects.create(name="Round trip profile")
        profile.tags.add(tag)
        backend = old_apps.get_model(APP, "InferenceBackend").objects.create(backend_key="round-trip", **BACKEND_FIELDS)
        backend.tags.add(tag)

        MigrationExecutor(connection).migrate([(APP, DATA_MOVE)])
        MigrationExecutor(connection).migrate([BEFORE])

        for model_name, pk in (("importprofile", profile.pk), ("inferencebackend", backend.pk)):
            with self.subTest(model=model_name):
                links = old_apps.get_model(APP, model_name).tags.through
                self.assertEqual(list(links.objects.values_list(f"{model_name}_id", "tag_id")), [(pk, tag.pk)])
        self.assertFalse(TaggedItem.objects.filter(content_type__app_label=APP).exists())

    def test_rollback_drops_an_assignment_whose_object_is_gone(self):
        tag = Tag.objects.create(name="Orphan", slug="orphan")
        profile = ImportProfile.objects.create(name="Kept profile")
        profile.tags.add(tag)
        # A generic key has no foreign key, so an assignment can outlive its object.
        TaggedItem.objects.create(
            tag=tag, content_type=ContentType.objects.get_for_model(ImportProfile), object_id=2_147_483_647
        )

        executor = MigrationExecutor(connection)
        executor.migrate([BEFORE])
        links = executor.loader.project_state([BEFORE]).apps.get_model(APP, "ImportProfile").tags.through

        self.assertEqual(list(links.objects.values_list("importprofile_id", "tag_id")), [(profile.pk, tag.pk)])
        self.assertFalse(TaggedItem.objects.exists())

    def test_a_branch_migrate_fakes_the_move(self):
        module = importlib.import_module(f"{APP}.migrations.{DATA_MOVE}")

        self.assertIs(module.fake_on_branch, True)

    @staticmethod
    def _cable_projection():
        with connection.cursor() as cursor:
            cursor.execute("SELECT id FROM extras_taggeditem WHERE ndi_cable_id IS NOT NULL")
            return [row[0] for row in cursor.fetchall()]
