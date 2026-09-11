# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Exercise digest synchronization through real model validation and database writes."""

import hashlib
from io import BytesIO

import yaml
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.test import TestCase
from core.models import ObjectType
from dcim.models import Cable, Device, Interface

from netbox_data_import.field_keys import SELECT_TERMINATION_TASK, TERMINATION_ROLE, termination_field_key
from netbox_data_import.models import CableImportSource, ImportProfile, TerminationResolution
from netbox_data_import.tests.helpers import make_dcim_objects


class DigestIndexedModelTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.profile = ImportProfile.objects.create(name="Digest Profile", source_adapter="trace_workbook")
        site, _manufacturer, device_type, role = make_dcim_objects("Digest")
        cls.device = Device.objects.create(name="Digest Device", site=site, device_type=device_type, role=role)
        cls.first = Interface.objects.create(device=cls.device, name="eth0", type="1000base-t")
        second = Interface.objects.create(device=cls.device, name="eth1", type="1000base-t")
        cls.cable = Cable(a_terminations=[cls.first], b_terminations=[second])
        cls.cable.save()

    def _rows(self):
        key = termination_field_key(
            device="Digest Device", cards="", port="eth0", kind="interface", role=TERMINATION_ROLE
        )
        moved_key = termination_field_key(
            device="Digest Device", cards="", port="eth2", kind="interface", role=TERMINATION_ROLE
        )
        return (
            (
                TerminationResolution(
                    profile=self.profile,
                    task_type=SELECT_TERMINATION_TASK,
                    field_key=key,
                    selected_object_type=ObjectType.objects.get_for_model(self.first),
                    selected_object_id=self.first.pk,
                    selected_display_name="Original selection",
                ),
                "field_key",
                "field_key_digest",
                moved_key,
                "selected_display_name",
            ),
            (
                CableImportSource(
                    cable=self.cable,
                    profile=self.profile,
                    trace_identity='[["dígest device","","eth0"],["digest peer","","eth1"]]',
                    segment_index=0,
                ),
                "trace_identity",
                "trace_key",
                '[["dígest device","","eth2"],["digest peer","","eth1"]]',
                "sheet",
            ),
        )

    def test_save_replaces_a_supplied_digest_without_clean(self):
        for row, source_field, digest_field, _moved, _other in self._rows():
            with self.subTest(model=type(row).__name__):
                source = getattr(row, source_field)
                setattr(row, digest_field, "outdated")
                row.save()
                row.refresh_from_db()
                self.assertEqual(
                    (getattr(row, source_field), getattr(row, digest_field)),
                    (source, hashlib.sha256(source.encode()).hexdigest()),
                )

    def test_clean_derives_the_digest_before_constraint_validation(self):
        for row, source_field, digest_field, _moved, _other in self._rows():
            with self.subTest(model=type(row).__name__):
                setattr(row, digest_field, "outdated")
                row.clean()
                self.assertEqual(
                    getattr(row, digest_field), hashlib.sha256(getattr(row, source_field).encode()).hexdigest()
                )

    def test_partial_source_save_updates_the_stored_digest(self):
        for row, source_field, digest_field, moved, _other in self._rows():
            with self.subTest(model=type(row).__name__):
                row.save()
                setattr(row, source_field, moved)
                update_fields = {source_field}
                row.save(update_fields=update_fields)
                row.refresh_from_db()
                self.assertEqual(
                    (getattr(row, source_field), getattr(row, digest_field)),
                    (moved, hashlib.sha256(moved.encode()).hexdigest()),
                )
                self.assertEqual(update_fields, {source_field})

    def test_unrelated_partial_save_keeps_the_stored_source_and_digest(self):
        for row, source_field, digest_field, moved, other in self._rows():
            with self.subTest(model=type(row).__name__):
                row.save()
                source = getattr(row, source_field)
                setattr(row, source_field, moved)
                setattr(row, other, "Updated")
                row.save(update_fields={other})
                row.refresh_from_db()
                self.assertEqual(
                    (getattr(row, source_field), getattr(row, digest_field), getattr(row, other)),
                    (source, hashlib.sha256(source.encode()).hexdigest(), "Updated"),
                )

    def test_empty_update_fields_does_not_write(self):
        for row, source_field, digest_field, moved, _other in self._rows():
            with self.subTest(model=type(row).__name__):
                row.save()
                source = getattr(row, source_field)
                setattr(row, source_field, moved)
                row.save(update_fields=set())
                row.refresh_from_db()
                self.assertEqual(
                    (getattr(row, source_field), getattr(row, digest_field)),
                    (source, hashlib.sha256(source.encode()).hexdigest()),
                )

    def test_invalid_termination_key_is_rejected_before_derivation(self):
        row, _source_field, _digest_field, _moved, _other = self._rows()[0]
        row.field_key = "not canonical"
        row.field_key_digest = "unchanged"
        with self.assertRaises(ValidationError) as caught:
            row.clean()
        self.assertEqual(set(caught.exception.message_dict), {"field_key"})
        self.assertEqual(row.field_key_digest, "unchanged")

    def test_profile_yaml_reimport_preserves_termination_resolutions(self):
        user = get_user_model().objects.create_superuser("digest_yaml", "digest@example.invalid", "testpass")
        self.client.force_login(user)
        row, _source_field, _digest_field, _moved, _other = self._rows()[0]
        row.save()
        expected = {
            "task_type": row.task_type,
            "field_key": row.field_key,
            "field_key_digest": hashlib.sha256(row.field_key.encode()).hexdigest(),
            "selected_object_type_id": row.selected_object_type_id,
            "selected_object_id": row.selected_object_id,
            "selected_display_name": row.selected_display_name,
        }
        self.profile.description = "Exported description"
        self.profile.save()
        url = reverse("plugins:netbox_data_import:exportprofile_yaml", args=[self.profile.pk])
        exported = self.client.get(url)
        self.assertEqual(exported.status_code, 200)
        payload = yaml.safe_load(exported.content)
        ImportProfile.objects.filter(pk=self.profile.pk).update(description="Changed after export")
        upload = BytesIO(exported.content)
        upload.name = "profile.yaml"
        imported = self.client.post(reverse("plugins:netbox_data_import:import_profile_yaml"), {"yaml_file": upload})
        self.profile.refresh_from_db()
        self.assertEqual((imported.status_code, self.profile.description), (302, "Exported description"))
        self.assertEqual(list(self.profile.termination_resolutions.values(*expected)), [expected])
        self.assertEqual(yaml.safe_load(self.client.get(url).content), payload)
