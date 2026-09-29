# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The background import records each NetBox update with its real before and after state."""

import uuid

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import TransactionTestCase

from core.exceptions import JobFailed
from core.models import Job, ObjectChange

from netbox_data_import.import_engine import ImportEngine
from netbox_data_import.jobs import ImportJobRunner
from netbox_data_import.models import ClassRoleMapping, ColumnMapping, ImportProfile
from netbox_data_import.plan import Disposition
from netbox_data_import.tests.helpers import (
    make_dcim_objects,
    recorded_updates,
    store_workbook_document,
    update_webhook_rule,
)
from netbox_data_import.tests.mixins import IsolatedRQQueueTestMixin

_HEADERS = ["Source ID", "Class", "Name", "Rack", "Make", "Model", "Height", "Serial", "Primary IPv4", "Contact"]
_TARGETS = [
    "source_id",
    "device_class",
    "device_name",
    "rack_name",
    "make",
    "model",
    "u_height",
    "serial",
    "primary_ip4",
    "primary_contact",
]


class ImportJobTestBase(TransactionTestCase):
    """A stored rack and device that one import job row set updates."""

    def setUp(self):
        from dcim.models import Device, InterfaceTemplate, Rack
        from tenancy.models import ContactRole

        self.site, _manufacturer, self.device_type, self.role = make_dcim_objects("changelog-")
        InterfaceTemplate.objects.create(device_type=self.device_type, name="mgmt0", type="1000base-t", mgmt_only=True)
        self.rack = Rack.objects.create(name="rack-a", site=self.site, u_height=20)
        self.device = Device.objects.create(
            name="server-a",
            site=self.site,
            rack=self.rack,
            device_type=self.device_type,
            role=self.role,
            serial="SN-OLD",
        )
        self.contact_role = ContactRole.objects.create(name="Owner", slug="owner")
        self.actor = get_user_model().objects.create_superuser(
            username="changelog-operator", email="changelog@example.invalid", password="testpass"
        )
        self.profile = ImportProfile.objects.create(
            name="Change Log Profile",
            adapter_config={
                "sheet_name": "Data",
                "update_existing": True,
                "custom_field_name": "source_ref",
                "primary_contact_role": self.contact_role.name,
                "primary_contact_lookup_field": "email",
            },
        )
        for source_column, target_field in zip(_HEADERS, _TARGETS, strict=True):
            ColumnMapping.objects.create(profile=self.profile, source_column=source_column, target_field=target_field)
        ClassRoleMapping.objects.create(profile=self.profile, source_class="Cabinet", creates_rack=True)
        ClassRoleMapping.objects.create(profile=self.profile, source_class="Server", role_slug=self.role.slug)

    def _run_import(self, *rows):
        """Plan the rows, select every actionable unit, and run the import job on them."""
        document = store_workbook_document(self.profile, _HEADERS, list(rows), self.actor, "changelog.xlsx")
        plan = ImportEngine.plan(
            self.profile, document, self.actor, {"site_id": self.site.pk, "location_id": None, "tenant_id": None}
        )
        selection = [unit.identity for unit in plan.units if unit.disposition == Disposition.ACTIONABLE]
        self.assertTrue(selection, [unit.diagnostics for unit in plan.units])
        job = Job.objects.create(
            name="Data Import",
            user=self.actor,
            status="running",
            job_id=uuid.uuid4(),
            queue_name="default",
            data={"job_type": ImportJobRunner.job_type},
        )
        ImportJobRunner(job).run(self.profile.pk, document.pk, plan.to_dict(), selection, uuid.uuid4().hex)
        return job

    @staticmethod
    def _server_row(serial="SN-NEW", address="", contact=""):
        return ["D-1", "Server", "server-a", "rack-a", "changelog-Mfg", "changelog-Model", "", serial, address, contact]

    @staticmethod
    def _cabinet_row(height="42"):
        return ["R-1", "Cabinet", "", "rack-a", "", "", height, "", "", ""]


class ImportJobChangeLogTest(ImportJobTestBase):
    """Each update the import job writes has an ObjectChange whose prechange data is the stored row."""

    def _updates(self, obj, job):
        """Return the update ObjectChanges this job wrote for *obj*, oldest first."""
        changes = recorded_updates(obj)
        for change in changes:
            self.assertEqual(change.user, self.actor)
            self.assertEqual(change.request_id, job.job_id)
        return changes

    def test_the_job_records_its_writes_as_its_user(self):
        """Before this, a background import wrote no ObjectChange at all."""
        job = self._run_import(self._server_row())

        changes = ObjectChange.objects.filter(request_id=job.job_id)
        self.assertTrue(changes.exists())
        self.assertEqual(set(changes.values_list("user_id", flat=True)), {self.actor.pk})

    def test_a_rack_update_records_the_stored_rack(self):
        job = self._run_import(self._cabinet_row())

        (change,) = self._updates(self.rack, job)
        self.assertEqual(change.prechange_data["u_height"], 20)
        self.assertEqual(change.postchange_data["u_height"], 42)

    def test_each_device_save_records_the_state_the_previous_save_left(self):
        """The import saves the device three times, so each snapshot must be retaken."""
        from ipam.models import IPAddress

        address = IPAddress.objects.create(address="198.18.0.20/32")

        job = self._run_import(self._server_row(address="198.18.0.20"))

        fields, ips, custom = self._updates(self.device, job)
        self.assertEqual((fields.prechange_data["serial"], fields.postchange_data["serial"]), ("SN-OLD", "SN-NEW"))
        self.assertEqual(ips.prechange_data["serial"], "SN-NEW")
        self.assertEqual((ips.prechange_data["primary_ip4"], ips.postchange_data["primary_ip4"]), (None, address.pk))
        self.assertEqual(custom.prechange_data["primary_ip4"], address.pk)
        self.assertNotIn("source_ref", custom.prechange_data["custom_fields"])
        self.assertEqual(custom.postchange_data["custom_fields"]["source_ref"], "D-1")
        (moved,) = self._updates(address, job)
        self.assertIsNone(moved.prechange_data["assigned_object_id"])
        self.assertEqual(moved.postchange_data["assigned_object_id"], self.device.interfaces.get(name="mgmt0").pk)

    def _assignment(self, contact, priority="primary"):
        from tenancy.models import ContactAssignment

        return ContactAssignment.objects.create(
            object_type=ContentType.objects.get_for_model(self.device),
            object_id=self.device.pk,
            contact=contact,
            role=self.contact_role,
            priority=priority,
        )

    def test_a_replaced_primary_contact_records_the_previous_contact(self):
        from tenancy.models import Contact

        previous = Contact.objects.create(name="Previous", email="previous@example.invalid")
        selected = Contact.objects.create(name="Selected", email="selected@example.invalid")
        assignment = self._assignment(previous)

        job = self._run_import(self._server_row(contact=selected.email))

        (change,) = self._updates(assignment, job)
        self.assertEqual(
            (change.prechange_data["contact"], change.postchange_data["contact"]), (previous.pk, selected.pk)
        )

    def test_a_demoted_and_a_promoted_contact_record_their_previous_priority(self):
        from tenancy.models import Contact

        previous = self._assignment(Contact.objects.create(name="Previous", email="previous@example.invalid"))
        selected_contact = Contact.objects.create(name="Selected", email="selected@example.invalid")
        selected = self._assignment(selected_contact, priority="secondary")

        job = self._run_import(self._server_row(contact=selected_contact.email))

        (demoted,) = self._updates(previous, job)
        self.assertEqual(
            (demoted.prechange_data["priority"], demoted.postchange_data["priority"]), ("primary", "secondary")
        )
        (promoted,) = self._updates(selected, job)
        self.assertEqual(
            (promoted.prechange_data["priority"], promoted.postchange_data["priority"]), ("secondary", "primary")
        )


class ImportJobEventTest(IsolatedRQQueueTestMixin, ImportJobTestBase):
    """Event rules see only the changes a successful import commits."""

    def test_a_failed_import_sends_no_events_for_its_rolled_back_writes(self):
        from dcim.models import Device, Rack
        from django.core.exceptions import ValidationError
        from django.db.models.signals import pre_save
        from django_rq import get_queue

        update_webhook_rule(Rack)

        def refuse_the_device(sender, instance, **kwargs):
            raise ValidationError("The device write is refused after the rack write.")

        pre_save.connect(refuse_the_device, sender=Device, weak=False)
        try:
            with self.assertRaises(JobFailed):
                self._run_import(self._cabinet_row(), self._server_row())
        finally:
            pre_save.disconnect(refuse_the_device, sender=Device)

        self.rack.refresh_from_db()
        self.assertEqual(self.rack.u_height, 20, "the rack write did not roll back")
        self.assertEqual(recorded_updates(self.rack), [])
        self.assertEqual(get_queue("default").count, 0)

        self._run_import(self._cabinet_row(), self._server_row())

        self.assertEqual(len(recorded_updates(self.rack)), 1)
        self.assertEqual(get_queue("default").count, 1)
