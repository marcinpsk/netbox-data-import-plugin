# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Integration tests for native NetBox contact synchronization."""

from io import BytesIO
from pathlib import Path
import json
import re
import subprocess
import threading
import uuid
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import close_old_connections
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.urls import reverse

from core.models import Job
from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from tenancy.models import Contact, ContactAssignment, ContactRole

from netbox_data_import.contact_resolution import ContactSelection, PrimaryContactResolver
from netbox_data_import.engine import parse_file, derive_effective_rows, run_import
from netbox_data_import.jobs import ImportJobRunner
from netbox_data_import.models import (
    ClassRoleMapping,
    ColumnMapping,
    DeviceExistingMatch,
    ImportProfile,
    SourceResolution,
    stored_import_source,
)
from netbox_data_import.object_permissions import ObjectPermissionDenied
from netbox_data_import.preview_row_actions import PREVIEW_REVISION_SESSION_KEY
from netbox_data_import.tests.helpers import set_import_source


LOCAL_EXAMPLE_PATH = Path(__file__).resolve().parents[3] / "libre" / "example.xlsx"


def _json_script(response, element_id):
    """Return the payload Django's `json_script` filter rendered under *element_id*."""
    import json
    import re

    match = re.search(
        rf'<script id="{re.escape(element_id)}" type="application/json">(.*?)</script>',
        response.content.decode(),
        re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"the preview did not render a '{element_id}' payload")
    return json.loads(match.group(1))


class ContactMappingWorkbookTest(TestCase):
    """Exercise Contact column mappings with a portable workbook."""

    @staticmethod
    def _workbook():
        """Return one in-memory workbook with the supported Contact columns."""
        import openpyxl

        workbook = openpyxl.Workbook()
        worksheet = workbook.active
        worksheet.title = "Data"
        worksheet.append(["Primary Contact", "Contact", "Contact Number", "Owner"])
        worksheet.append(
            [
                "primary.contact@example.invalid",
                "contact@example.invalid",
                "+1 202-555-0100",
                "Example Owner",
            ]
        )
        buffer = BytesIO()
        workbook.save(buffer)
        buffer.seek(0)
        return buffer

    def test_primary_contact_column_maps_to_native_contact_target(self):
        """A Primary Contact cell parses as a native Contact value."""
        profile = ImportProfile.objects.create(name="Portable Contact Mapping", adapter_config={"sheet_name": "Data"})
        ColumnMapping.objects.create(
            profile=profile,
            source_column="Primary Contact",
            target_field="primary_contact",
        )

        rows = parse_file(self._workbook(), profile)

        self.assertEqual(rows[0]["primary_contact"], "primary.contact@example.invalid")
        self.assertNotIn("_extra_columns", rows[0])

    def test_contact_candidate_columns_are_collected_per_row(self):
        """Every configured candidate column remains selectable per row."""
        profile = ImportProfile.objects.create(
            name="Portable Contact Candidates", adapter_config={"sheet_name": "Data"}
        )
        candidate_columns = {"Primary Contact", "Contact", "Contact Number", "Owner"}
        ColumnMapping.objects.bulk_create(
            [
                ColumnMapping(
                    profile=profile,
                    source_column=source_column,
                    target_field="candidate:contact",
                )
                for source_column in candidate_columns
            ]
        )

        rows = parse_file(self._workbook(), profile)

        self.assertEqual(set(rows[0]["_candidate_values"]["contact"]), candidate_columns)
        self.assertNotIn("candidate:contact", rows[0])


@skipUnless(LOCAL_EXAMPLE_PATH.is_file(), "Local LibreNMS example workbook is not available")
class LocalExamplePrivacyTest(SimpleTestCase):
    """Ensure the operator's local workbook does not leak into tracked files."""

    def test_private_workbook_values_are_not_embedded_in_repository(self):
        """Tracked files must not contain values copied from private workbook columns."""
        import openpyxl
        import tomllib

        private_headers = {
            "Asset Tag",
            "Asset_Tag",
            "Asset_Tag_Archived",
            "Audit Notes",
            "CANS",
            "City",
            "Company",
            "Contact",
            "Contact Number",
            "Country",
            "Department",
            "Department Director",
            "Description",
            "Dir. Department",
            "Equipment Notes",
            "Express Service Code",
            "Hostname",
            "IDRAC Default Password",
            "IDRAC MAC Address",
            "IP Address (IPv4)",
            "Id",
            "JIRA ID",
            "Location",
            "MAC Address",
            "Management IP Address",
            "Name",
            "Owner",
            "Primary Contact",
            "Project",
            "Purchase Order",
            "Purchase Price",
            "RACK",
            "Rack",
            "Room",
            "Serial Number",
            "Service Provider",
            "Service Tag",
            "SolarWinds ID",
            "VP Department",
            "Wave ID",
        }
        repository_root = Path(__file__).resolve().parents[2]
        with (repository_root / "pyproject.toml").open("rb") as metadata_file:
            project_metadata = tomllib.load(metadata_file)["project"]
        ignored_values = {"n/a", "none", "unknown", "yes", "no"}
        ignored_values.update(
            str(value).strip().casefold() for author in project_metadata.get("authors", []) for value in author.values()
        )
        synthetic_workbook = openpyxl.load_workbook(
            Path(__file__).resolve().parent / "fixtures" / "sample_cans.xlsx",
            read_only=True,
            data_only=True,
        )
        ignored_values.update(
            str(cell.value).strip().casefold()
            for synthetic_sheet in synthetic_workbook.worksheets
            for row in synthetic_sheet.iter_rows()
            for cell in row
            if cell.value is not None
        )
        workbook = openpyxl.load_workbook(LOCAL_EXAMPLE_PATH, read_only=True, data_only=True)
        worksheet = workbook["Data"]
        headers = {cell.column: str(cell.value).strip() for cell in worksheet[1] if cell.value is not None}
        private_values = {}
        for row in worksheet.iter_rows(min_row=2):
            for cell in row:
                if cell.value is None:
                    continue
                header = headers.get(cell.column)
                if header not in private_headers:
                    continue
                value = str(cell.value).strip()
                if len(value) < 4 or "\n" in value or value.casefold() in ignored_values:
                    continue
                if value.isdigit() and len(value) < 6:
                    continue
                private_values.setdefault(value, (header, cell.coordinate))

        token_pattern = re.compile(r"[A-Za-z0-9][A-Za-z0-9@._:/+-]{3,}")
        quoted_pattern = re.compile(r""""([^"\n]{4,})"|'([^'\n]{4,})' """, re.VERBOSE)
        tracked_paths = (
            subprocess.check_output(
                ["git", "-c", f"safe.directory={repository_root}", "ls-files", "-z"],
                cwd=repository_root,
            )
            .decode()
            .split("\0")
        )
        matches = []
        for relative_path in tracked_paths:
            if not relative_path:
                continue
            path = repository_root / relative_path
            try:
                lines = path.read_text().splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            for line_number, line in enumerate(lines, 1):
                lowered_line = line.casefold()
                if (
                    "copyright" in lowered_line
                    or "author_email" in lowered_line
                    or "author =" in lowered_line
                    or "authors =" in lowered_line
                ):
                    continue
                candidates = set(token_pattern.findall(line))
                candidates.update(value.strip() for match in quoted_pattern.findall(line) for value in match if value)
                for value in candidates & private_values.keys():
                    header, coordinate = private_values[value]
                    matches.append(f"{relative_path}:{line_number} matches {header} at Data!{coordinate}")

        self.assertFalse(matches, "Private workbook values are tracked:\n" + "\n".join(matches))


@skipUnless(LOCAL_EXAMPLE_PATH.is_file(), "Local LibreNMS example workbook is not available")
class LocalExampleContactMappingTest(TestCase):
    """Check the operator's local workbook without recording its values."""

    def test_primary_contact_column_maps_to_native_contact_target(self):
        """Populated Primary Contact cells parse as primary_contact values."""
        profile = ImportProfile.objects.create(name="Local Contact Mapping", adapter_config={"sheet_name": "Data"})
        ColumnMapping.objects.create(
            profile=profile,
            source_column="Primary Contact",
            target_field="primary_contact",
        )

        with LOCAL_EXAMPLE_PATH.open("rb") as workbook:
            rows = parse_file(workbook, profile)

        contact_rows = [row for row in rows if row.get("primary_contact")]
        self.assertTrue(contact_rows)
        self.assertTrue(all("_extra_columns" not in row for row in contact_rows))

    def test_configured_contact_candidate_columns_are_collected_per_row(self):
        """Configured columns provide candidate values without asserting their meaning."""
        profile = ImportProfile.objects.create(name="Local Contact Candidates", adapter_config={"sheet_name": "Data"})
        candidate_columns = {"Primary Contact", "Contact", "Contact Number", "Owner"}
        ColumnMapping.objects.bulk_create(
            [
                ColumnMapping(
                    profile=profile,
                    source_column=source_column,
                    target_field="candidate:contact",
                )
                for source_column in candidate_columns
            ]
        )

        with LOCAL_EXAMPLE_PATH.open("rb") as workbook:
            rows = parse_file(workbook, profile)

        candidate_rows = [row for row in rows if row.get("_candidate_values", {}).get("contact")]
        self.assertTrue(candidate_rows)
        self.assertTrue(all(set(row["_candidate_values"]["contact"]) <= candidate_columns for row in candidate_rows))
        self.assertTrue(all("candidate:contact" not in row for row in candidate_rows))


class NativeContactSyncTest(TestCase):
    """Exercise contact synchronization through the public import engine."""

    def setUp(self):
        """Create a profile and an existing device for an update sync."""
        self.site = Site.objects.create(name="Contact Test Site", slug="contact-test-site")
        manufacturer = Manufacturer.objects.create(name="Contact Test Vendor", slug="contact-test-vendor")
        self.device_type = DeviceType.objects.create(
            manufacturer=manufacturer,
            model="Contact Test Model",
            slug="contact-test-vendor-contact-test-model",
        )
        self.device_role = DeviceRole.objects.create(name="Contact Test Device", slug="contact-test-device")
        self.contact_role = ContactRole.objects.create(name="Primary Contact", slug="primary-contact")
        self.profile = ImportProfile.objects.create(
            name="Native Contact Sync",
            adapter_config={
                "update_existing": True,
                "create_missing_device_types": False,
                "primary_contact_role": self.contact_role.name,
                "primary_contact_lookup_field": "email",
            },
        )
        ClassRoleMapping.objects.create(
            profile=self.profile,
            source_class="Server",
            creates_rack=False,
            role_slug=self.device_role.slug,
        )
        self.device = Device.objects.create(
            name="contact-test-device",
            site=self.site,
            device_type=self.device_type,
            role=self.device_role,
        )
        self._set_extra_columns({"depth": 750, "primary_contact": "primary.contact@example.com"})

    def _set_extra_columns(self, extra_columns):
        """Store the unmapped source columns the device carries from an earlier import."""
        return set_import_source(self.device, self.profile, "CONTACT-001", extra_columns=extra_columns)

    def _stored_extra_columns(self, device=None):
        """Return the extra columns held by one device import record."""
        return stored_import_source(device or self.device).extra_columns

    def _row(self, **overrides):
        """Return one valid source row for this profile."""
        row = {
            "_row_number": 2,
            "source_id": "CONTACT-001",
            "device_name": self.device.name,
            "device_class": "Server",
            "make": self.device_type.manufacturer.name,
            "model": self.device_type.model,
            "u_height": 1,
            "rack_name": "",
            "u_position": "",
            "serial": "",
            "asset_tag": "",
            "status": "active",
        }
        row.update(overrides)
        return row

    def _sync(self, row=None):
        """Run one real update sync."""
        return run_import([row or self._row()], self.profile, {"site": self.site}, dry_run=False)

    def _grant_object_permission(self, user, model, actions):
        """Grant unrestricted NetBox object permissions for one model."""
        from users.models import ObjectPermission

        permission = ObjectPermission.objects.create(
            name=f"Contact sync {model._meta.label_lower} {' '.join(actions)}",
            actions=actions,
        )
        permission.object_types.add(ContentType.objects.get_for_model(model))
        permission.users.add(user)
        return permission

    def _cache_contact_preview(self, row):
        """Store one candidate preview for a real resolution request."""
        user = get_user_model().objects.create_superuser(
            username="contact-preview-action-user",
            email="contact-preview-action-user@example.invalid",
            password="testpass",
        )
        self.client.force_login(user)
        result = run_import([row], self.profile, {"site": self.site}, dry_run=True, user=user)
        session = self.client.session
        session["import_result"] = result.to_session_dict()
        session["import_rows"] = [row]
        session["import_context"] = {
            "profile_id": self.profile.pk,
            "site_id": self.site.pk,
            "location_id": None,
            "tenant_id": None,
            "filename": "contact-candidates.xlsx",
        }
        session["import_preview_pending"] = True
        session[PREVIEW_REVISION_SESSION_KEY] = "contact-sync-preview"
        session.save()
        return user

    def _preview_revision(self):
        return self.client.session[PREVIEW_REVISION_SESSION_KEY]

    def test_sync_migrates_legacy_primary_contact_to_native_assignment(self):
        """An update sync moves only the legacy contact value out of JSON."""
        result = self._sync()

        self.assertFalse(result.has_errors, [row.to_dict() for row in result.rows])
        self.device.refresh_from_db()
        contact = Contact.objects.get(email__iexact="primary.contact@example.com")
        assignment = ContactAssignment.objects.get(
            contact=contact,
            object_type=ContentType.objects.get_for_model(self.device),
            object_id=self.device.pk,
        )
        self.assertEqual(contact.name, "primary.contact@example.com")
        self.assertEqual(assignment.priority, "primary")
        self.assertEqual(assignment.role.slug, "primary-contact")
        self.assertEqual(self._stored_extra_columns(), {"depth": 750})

    def test_contact_candidates_require_a_saved_row_resolution(self):
        """Candidate values cannot silently become Contact fields."""
        self._set_extra_columns({"depth": 750})
        row = self._row(
            _candidate_values={
                "contact": {
                    "Primary Contact": "candidate.contact@example.invalid",
                    "Owner": "Candidate Operator",
                }
            }
        )

        result = run_import([row], self.profile, {"site": self.site}, dry_run=True)

        self.assertTrue(result.has_errors)
        device_row = next(item for item in result.rows if item.object_type == "device")
        self.assertEqual(device_row.extra_data["identity_conflict"], "candidate_resolution_required")
        self.assertEqual(
            device_row.extra_data["candidate_values"]["contact"],
            row["_candidate_values"]["contact"],
        )

    def test_review_loads_candidate_columns_from_the_profile(self):
        """The resolver owns candidate mapping lookup and extra-column removal."""
        ColumnMapping.objects.create(
            profile=self.profile,
            source_column="Candidate email",
            target_field="candidate:contact",
        )
        row = self._row(
            _extra_columns={"Candidate email": "candidate@example.invalid"},
            contact_resolution_applied=True,
            contact_field_sources={
                "name": "Candidate email",
                "email": "Candidate email",
            },
        )

        review = PrimaryContactResolver.review(None, row, self.profile)
        result = run_import([row], self.profile, {"site": self.site}, dry_run=True)

        self.assertEqual(review.candidate_values, {"Candidate email": "candidate@example.invalid"})
        self.assertEqual(review.extra_columns, {})
        self.assertEqual(review.plan["assignment_action"], "create")
        self.assertFalse(result.has_errors, [item.to_dict() for item in result.rows])

    def test_review_preserves_validation_errors_without_candidate_values(self):
        """A literal-only resolution keeps its precise validation failure."""
        row = self._row(
            contact_resolution_applied=True,
            contact_field_sources={},
            contact_field_values={
                "name": "Invalid Contact",
                "email": "not-an-email",
            },
        )

        with self.assertRaisesMessage(ValidationError, "valid email"):
            PrimaryContactResolver.review(self.device, row, self.profile)

    def test_apply_refuses_a_role_deleted_after_the_review(self):
        """One profile instance serves both calls, and it memoizes the role it resolved."""
        from tenancy.models import ContactRole

        row = self._row(
            contact_resolution_applied=True,
            contact_field_values={"name": "Late Contact", "email": "late@example.invalid"},
            contact_field_sources={},
        )
        review = PrimaryContactResolver.review(self.device, row, self.profile)
        ContactRole.objects.filter(name=self.profile.adapter_settings.primary_contact_role).delete()

        with self.assertRaisesMessage(ValidationError, "no longer exists"):
            PrimaryContactResolver.apply(self.device, self.profile, review)

    def test_review_without_contact_data_has_no_contact_plan(self):
        """A row without Contact data leaves native assignments unchanged."""
        self._set_extra_columns({"depth": 750})

        review = PrimaryContactResolver.review(self.device, self._row(), self.profile)

        self.assertIsNone(review.selection)
        self.assertIsNone(review.plan)

    def test_a_row_whose_contact_assignment_is_missing_is_still_an_update(self):
        """A device row writes more than its own fields, so an absent Contact is a write to report."""
        row = self._row(
            contact_resolution_applied=True,
            contact_field_sources={},
            contact_field_values={"name": "Noop Contact", "email": "noop.contact@example.invalid"},
        )
        run_import([dict(row)], self.profile, {"site": self.site}, dry_run=False)
        settled = run_import([dict(row)], self.profile, {"site": self.site}, dry_run=True)
        settled_row = next(r for r in settled.rows if r.object_type == "device")
        self.assertEqual(settled_row.action, "skip", settled_row.detail)

        ContactAssignment.objects.all().delete()

        preview = run_import([dict(row)], self.profile, {"site": self.site}, dry_run=True)

        device_row = next(r for r in preview.rows if r.object_type == "device")
        self.assertEqual(device_row.action, "update", device_row.detail)

    def test_a_candidate_value_that_identifies_no_contact_suggests_nothing(self):
        """A blank value and a value no Contact carries leave the picker empty."""
        suggestion = PrimaryContactResolver.suggest(
            {"Blank": "", "Name": "Not an email"},
            self.profile,
        )

        self.assertIsNone(suggestion)

    def test_a_row_carrying_only_a_name_finds_the_contact_with_that_name(self):
        """The lookup field is email, so a name-only row matched nothing and had to be typed in."""
        contact = Contact.objects.create(name="Piet Janssen", email="", phone="")

        suggestion = PrimaryContactResolver.suggest({"Owner": "Piet Janssen"}, self.profile)

        self.assertIsNotNone(suggestion)
        self.assertEqual(suggestion["id"], contact.pk)

    def test_the_configured_lookup_field_still_answers_first(self):
        """A row carrying both an email and another Contact's name keeps the email's answer."""
        by_email = Contact.objects.create(name="Email Owner", email="owner@example.invalid")
        Contact.objects.create(name="Name Owner", email="")

        suggestion = PrimaryContactResolver.suggest(
            {"Contact": "owner@example.invalid", "Owner": "Name Owner"},
            self.profile,
        )

        self.assertEqual(suggestion["id"], by_email.pk)

    def test_two_contacts_answering_the_same_name_suggest_nothing(self):
        """A suggestion is only offered when the row identifies exactly one Contact."""
        Contact.objects.create(name="Shared Name", email="one@example.invalid")
        Contact.objects.create(name="Shared Name", email="two@example.invalid")

        self.assertIsNone(PrimaryContactResolver.suggest({"Owner": "Shared Name"}, self.profile))

    def test_new_device_contact_plan_requires_assignment_permission(self):
        """A new Device plan checks assignment permission before any write."""
        user = get_user_model().objects.create_user(username="new-device-contact-plan-user")
        row = self._row(
            contact_resolution_applied=True,
            contact_field_sources={},
            contact_field_values={
                "name": "New Device Contact",
                "email": "new-device-contact@example.invalid",
            },
        )
        self._grant_object_permission(user, Contact, ["add"])

        with self.assertRaisesMessage(ObjectPermissionDenied, "tenancy.add_contactassignment"):
            PrimaryContactResolver.review(None, row, self.profile, user=user)

    def test_selected_contact_validation_uses_the_current_netbox_identity(self):
        """A saved selection fails when its Contact disappears or changes identity."""
        contact = Contact.objects.create(name="Selected Contact", email="selected@example.invalid")
        row = self._row(
            contact_resolution_applied=True,
            contact_field_sources={},
            contact_field_values={},
            contact_id=contact.pk,
        )
        contact.delete()

        with self.assertRaisesMessage(ValidationError, "no longer exists"):
            PrimaryContactResolver.review(self.device, row, self.profile)

        replacement = Contact.objects.create(name="Selected Contact", email="selected@example.invalid")
        row.update(
            contact_id=replacement.pk,
            contact_field_values={
                "name": replacement.name,
                "email": "changed@example.invalid",
            },
        )
        with self.assertRaisesMessage(ValidationError, "no longer has the chosen email"):
            PrimaryContactResolver.review(self.device, row, self.profile)

    def test_selected_contact_must_be_visible_to_the_operator(self):
        """A valid saved Contact ID cannot bypass object visibility."""
        contact = Contact.objects.create(name="Hidden Contact", email="hidden@example.invalid")
        user = get_user_model().objects.create_user(username="hidden-contact-selection-user")
        row = self._row(
            contact_resolution_applied=True,
            contact_field_sources={},
            contact_field_values={},
            contact_id=contact.pk,
        )

        with self.assertRaisesMessage(ObjectPermissionDenied, "tenancy.view_contact"):
            PrimaryContactResolver.review(self.device, row, self.profile, user=user)

    def test_contact_plan_requires_the_configured_lookup_value(self):
        """The planner rejects an incomplete selection at its write boundary."""
        selection = ContactSelection(values={"name": "Incomplete Contact"})

        with self.assertRaisesMessage(ValidationError, "Contact email lookup field"):
            PrimaryContactResolver._plan(self.device, self.profile, selection)

    def test_saved_contact_resolution_maps_candidate_values_to_native_fields(self):
        """Ensure saved contact resolutions populate native contact fields and create the corresponding assignment."""
        self._set_extra_columns({"depth": 750})
        email = "resolved.contact@example.invalid"
        phone = "+1 202-555-0100"
        SourceResolution.objects.create(
            profile=self.profile,
            source_id="CONTACT-001",
            source_column="candidate:contact",
            original_value="",
            resolved_fields={
                "contact_resolution_applied": True,
                "contact_field_sources": {
                    "name": "Contact",
                    "email": "Contact",
                    "phone": "Contact Number",
                },
            },
        )
        row = self._row(
            _candidate_values={
                "contact": {
                    "Contact": email,
                    "Contact Number": phone,
                }
            }
        )

        [resolved_row] = derive_effective_rows([row], self.profile)
        result = self._sync(resolved_row)

        self.assertFalse(result.has_errors, [item.to_dict() for item in result.rows])
        contact = Contact.objects.get(email=email)
        self.assertEqual(contact.name, email)
        self.assertEqual(contact.phone, phone)
        self.assertTrue(ContactAssignment.objects.filter(contact=contact, object_id=self.device.pk).exists())

    def test_saved_no_contact_resolution_allows_a_row_without_an_assignment(self):
        """An explicit no-contact decision is replayed without creating an assignment."""
        self._set_extra_columns({"depth": 750})
        SourceResolution.objects.create(
            profile=self.profile,
            source_id="CONTACT-001",
            source_column="candidate:contact",
            original_value="",
            resolved_fields={
                "contact_resolution_applied": True,
                "contact_field_sources": {},
            },
        )
        row = self._row(
            _candidate_values={"contact": {"Owner": "Candidate Operator"}},
        )

        [resolved_row] = derive_effective_rows([row], self.profile)
        result = self._sync(resolved_row)

        self.assertFalse(result.has_errors, [item.to_dict() for item in result.rows])
        self.assertFalse(Contact.objects.exists())
        self.assertFalse(ContactAssignment.objects.exists())

    def test_preview_can_replace_a_resolution_when_its_selected_source_is_blank(self):
        """A stale candidate choice remains editable from the preview row."""
        self._set_extra_columns({"depth": 750})
        SourceResolution.objects.create(
            profile=self.profile,
            source_id="CONTACT-001",
            source_column="candidate:contact",
            original_value="",
            resolved_fields={
                "contact_resolution_applied": True,
                "contact_field_sources": {
                    "name": "Contact",
                    "email": "Contact",
                },
            },
        )
        row = self._row(
            _candidate_values={"contact": {"Owner": "Replacement Operator"}},
        )

        [resolved_row] = derive_effective_rows([row], self.profile)
        result = run_import([resolved_row], self.profile, {"site": self.site}, dry_run=True)

        self.assertTrue(result.has_errors)
        device_row = next(item for item in result.rows if item.object_type == "device")
        self.assertEqual(device_row.extra_data["identity_conflict"], "candidate_resolution_required")
        self.assertEqual(
            device_row.extra_data["candidate_values"]["contact"],
            {"Owner": "Replacement Operator"},
        )

    def test_preview_can_save_a_contact_candidate_row_resolution(self):
        """The preview ships every candidate value and the role proposed for it."""
        self._set_extra_columns({"depth": 750})
        row = self._row(
            _candidate_values={
                "contact": {
                    "Contact": "preview.contact@example.invalid",
                    "Contact Number": "+1 202-555-0100",
                    "Owner": "Preview Operator",
                }
            }
        )
        result = run_import([row], self.profile, {"site": self.site}, dry_run=True)
        user = get_user_model().objects.create_superuser(
            username="contact-candidate-preview-user",
            email="contact-candidate-preview@example.invalid",
            password="testpass",
        )
        self.client.force_login(user)
        session = self.client.session
        session["import_result"] = result.to_session_dict()
        session["import_rows"] = [row]
        session["import_context"] = {
            "profile_id": self.profile.pk,
            "site_id": self.site.pk,
            "location_id": None,
            "tenant_id": None,
            "filename": "contact-candidates.xlsx",
        }
        session["import_preview_pending"] = True
        session[PREVIEW_REVISION_SESSION_KEY] = "contact-sync-preview"
        session.save()

        preview_response = self.client.get(reverse("plugins:netbox_data_import:import_preview"))

        self.assertContains(preview_response, "Resolve contact fields")
        self.assertContains(preview_response, "No contact for this row")
        # The value rows are built from these two payloads, so they carry the contract now.
        candidates = _json_script(preview_response, "ndi-candidate-values-by-row")
        row_candidates = candidates[str(result.rows[0].row_number)]["contact"]
        self.assertEqual(
            row_candidates,
            {
                "Contact": "preview.contact@example.invalid",
                "Contact Number": "+1 202-555-0100",
                "Owner": "Preview Operator",
            },
        )
        roles = _json_script(preview_response, "ndi-contact-role-suggestions-by-row")
        # `Owner` holds a person here, but the header does not say so, so no field claims it.
        self.assertEqual(
            roles[str(result.rows[0].row_number)],
            {"email": "Contact", "phone": "Contact Number"},
        )

        resolution_response = self.client.post(
            reverse("plugins:netbox_data_import:save_resolution"),
            {
                "profile_id": self.profile.pk,
                "source_id": "CONTACT-001",
                "source_column": "candidate:contact",
                "preview_revision": self._preview_revision(),
                "original_value": json.dumps(row["_candidate_values"]["contact"]),
                "resolved_fields": json.dumps(
                    {
                        "contact_resolution_applied": True,
                        "contact_field_sources": {
                            "name": "Contact",
                            "email": "Contact",
                            "phone": "Contact Number",
                        },
                    }
                ),
                "next": reverse("plugins:netbox_data_import:import_preview"),
            },
        )

        self.assertRedirects(
            resolution_response,
            reverse("plugins:netbox_data_import:import_preview"),
            fetch_redirect_response=False,
        )
        resolution = SourceResolution.objects.get(
            profile=self.profile,
            source_id="CONTACT-001",
            source_column="candidate:contact",
        )
        self.assertEqual(resolution.resolved_fields["contact_field_sources"]["name"], "Contact")
        self.assertEqual(resolution.resolved_fields["contact_field_sources"]["email"], "Contact")

    def test_linked_contact_resolution_applies_immediately_and_proposes_reuse(self):
        """Saving a resolution updates its Device and suggests the Contact on another row."""
        existing_contact = Contact.objects.create(
            name="Existing Contact",
            email="existing.contact@example.invalid",
            phone="+1 202-555-0101",
        )
        second_device = Device.objects.create(
            name="second-contact-test-device",
            site=self.site,
            device_type=self.device_type,
            role=self.device_role,
        )
        DeviceExistingMatch.objects.bulk_create(
            [
                DeviceExistingMatch(
                    profile=self.profile,
                    source_id="CONTACT-001",
                    netbox_device_id=self.device.pk,
                    device_name=self.device.name,
                ),
                DeviceExistingMatch(
                    profile=self.profile,
                    source_id="CONTACT-002",
                    netbox_device_id=second_device.pk,
                    device_name=second_device.name,
                ),
            ]
        )
        self._set_extra_columns({"depth": 750, "primary_contact": "not an email"})
        first_row = self._row(
            _candidate_values={
                "contact": {
                    "Owner": "Existing Contact",
                    "Contact": "existing.contact@example.invalid",
                    "Contact Number": "+1 202-555-0101",
                }
            }
        )
        second_row = self._row(
            _row_number=3,
            source_id="CONTACT-002",
            device_name=second_device.name,
            _candidate_values={
                "contact": {
                    "Owner": "Existing Contact",
                    "Contact": "existing.contact@example.invalid",
                }
            },
        )
        user = get_user_model().objects.create_superuser(
            username="linked-contact-resolution-user",
            email="linked-contact-resolution@example.invalid",
            password="testpass",
        )
        self.client.force_login(user)
        preview = run_import([first_row, second_row], self.profile, {"site": self.site}, dry_run=True, user=user)
        session = self.client.session
        session["import_result"] = preview.to_session_dict()
        session["import_rows"] = [first_row, second_row]
        session["import_context"] = {
            "profile_id": self.profile.pk,
            "site_id": self.site.pk,
            "location_id": None,
            "tenant_id": None,
            "filename": "contact-candidates.xlsx",
        }
        session["import_preview_pending"] = True
        session[PREVIEW_REVISION_SESSION_KEY] = "contact-sync-preview"
        session.save()

        response = self.client.post(
            reverse("plugins:netbox_data_import:save_resolution"),
            {
                "profile_id": self.profile.pk,
                "source_id": "CONTACT-001",
                "source_column": "candidate:contact",
                "preview_revision": self._preview_revision(),
                "original_value": "",
                "resolved_fields": json.dumps(
                    {
                        "contact_resolution_applied": True,
                        "contact_field_sources": {},
                        "contact_field_values": {
                            "name": "Existing Contact",
                            "email": "existing.contact@example.invalid",
                            "phone": "+1 202-555-0101",
                        },
                        "contact_id": existing_contact.pk,
                    }
                ),
                "next": reverse("plugins:netbox_data_import:import_preview"),
            },
        )

        self.assertRedirects(
            response,
            reverse("plugins:netbox_data_import:import_preview"),
            fetch_redirect_response=False,
        )
        assignment = ContactAssignment.objects.get(
            object_type=ContentType.objects.get_for_model(self.device),
            object_id=self.device.pk,
            role=self.contact_role,
            priority="primary",
        )
        self.assertEqual(assignment.contact, existing_contact)
        self.device.refresh_from_db()
        self.assertEqual(self._stored_extra_columns(), {"depth": 750})

        preview_response = self.client.get(reverse("plugins:netbox_data_import:import_preview"))
        second_result = next(row for row in preview_response.context["result"].rows if row.source_id == "CONTACT-002")
        self.assertEqual(second_result.extra_data["contact_suggestion"]["id"], existing_contact.pk)
        self.assertContains(preview_response, "Existing NetBox Contact")

    def test_contact_lookup_finds_visible_contacts(self):
        """The contact picker searches real NetBox Contacts."""
        contact = Contact.objects.create(
            name="Lookup Contact",
            email="lookup.contact@example.invalid",
            phone="+1 202-555-0102",
        )
        user = get_user_model().objects.create_superuser(
            username="contact-lookup-user",
            email="contact-lookup-user@example.invalid",
            password="testpass",
        )
        self.client.force_login(user)

        response = self.client.get(
            reverse("plugins:netbox_data_import:contact_lookup"),
            {"q": "lookup.contact"},
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["results"],
            [
                {
                    "id": contact.pk,
                    "name": contact.name,
                    "email": contact.email,
                    "phone": contact.phone,
                }
            ],
        )

    def test_contact_lookup_requires_two_search_characters(self):
        """The Contact search endpoint avoids broad one-character queries."""
        user = get_user_model().objects.create_superuser(
            username="short-contact-lookup-user",
            email="short-contact-lookup-user@example.invalid",
            password="testpass",
        )
        self.client.force_login(user)

        response = self.client.get(
            reverse("plugins:netbox_data_import:contact_lookup"),
            {"q": "x"},
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"results": []})

    def test_contact_resolution_requires_one_active_preview_row(self):
        """A resolution cannot bind to source data outside the active preview."""
        user = get_user_model().objects.create_superuser(
            username="missing-contact-preview-user",
            email="missing-contact-preview-user@example.invalid",
            password="testpass",
        )
        self.client.force_login(user)

        response = self.client.post(
            reverse("plugins:netbox_data_import:save_resolution"),
            {
                "profile_id": self.profile.pk,
                "source_id": "CONTACT-001",
                "source_column": "candidate:contact",
                "original_value": "",
                "resolved_fields": json.dumps(
                    {
                        "contact_resolution_applied": True,
                        "contact_field_sources": {},
                    }
                ),
                "next": reverse("plugins:netbox_data_import:import_preview"),
            },
        )

        self.assertRedirects(
            response,
            reverse("plugins:netbox_data_import:import_preview"),
            fetch_redirect_response=False,
        )
        self.assertFalse(SourceResolution.objects.exists())

    def test_contact_resolution_requires_candidate_values_in_the_preview(self):
        """A stale row without Contact candidates cannot save a decision."""
        row = self._row(
            _candidate_values={"contact": {"Contact": "candidate@example.invalid"}},
        )
        self._cache_contact_preview(row)
        session = self.client.session
        device_row = next(item for item in session["import_result"]["rows"] if item["object_type"] == "device")
        device_row["extra_data"]["candidate_values"]["contact"] = {}
        session.save()

        response = self.client.post(
            reverse("plugins:netbox_data_import:save_resolution"),
            {
                "profile_id": self.profile.pk,
                "source_id": "CONTACT-001",
                "source_column": "candidate:contact",
                "preview_revision": self._preview_revision(),
                "original_value": "",
                "resolved_fields": json.dumps(
                    {
                        "contact_resolution_applied": True,
                        "contact_field_sources": {},
                    }
                ),
                "next": reverse("plugins:netbox_data_import:import_preview"),
            },
        )

        self.assertRedirects(
            response,
            reverse("plugins:netbox_data_import:import_preview"),
            fetch_redirect_response=False,
        )
        self.assertFalse(SourceResolution.objects.exists())

    def test_contact_resolution_rechecks_the_linked_device(self):
        """An immediate Contact update fails if the previewed Device was deleted."""
        row = self._row(
            _candidate_values={"contact": {"Contact": "candidate@example.invalid"}},
        )
        self._cache_contact_preview(row)
        self.device.delete()

        response = self.client.post(
            reverse("plugins:netbox_data_import:save_resolution"),
            {
                "profile_id": self.profile.pk,
                "source_id": "CONTACT-001",
                "source_column": "candidate:contact",
                "preview_revision": self._preview_revision(),
                "original_value": "",
                "resolved_fields": json.dumps(
                    {
                        "contact_resolution_applied": True,
                        "contact_field_sources": {
                            "name": "Contact",
                            "email": "Contact",
                        },
                    }
                ),
                "next": reverse("plugins:netbox_data_import:import_preview"),
            },
        )

        self.assertRedirects(
            response,
            reverse("plugins:netbox_data_import:import_preview"),
            fetch_redirect_response=False,
        )
        self.assertFalse(SourceResolution.objects.exists())

    def test_contact_resolution_rejects_invalid_literal_details(self):
        """Immediate Contact validation rolls back the saved row decision."""
        row = self._row(
            _candidate_values={"contact": {"Contact": "candidate@example.invalid"}},
        )
        self._cache_contact_preview(row)

        response = self.client.post(
            reverse("plugins:netbox_data_import:save_resolution"),
            {
                "profile_id": self.profile.pk,
                "source_id": "CONTACT-001",
                "source_column": "candidate:contact",
                "preview_revision": self._preview_revision(),
                "original_value": "",
                "resolved_fields": json.dumps(
                    {
                        "contact_resolution_applied": True,
                        "contact_field_sources": {},
                        "contact_field_values": {
                            "name": "Invalid Contact",
                            "email": "not-an-email",
                        },
                    }
                ),
                "next": reverse("plugins:netbox_data_import:import_preview"),
            },
        )

        self.assertRedirects(
            response,
            reverse("plugins:netbox_data_import:import_preview"),
            fetch_redirect_response=False,
        )
        self.assertFalse(SourceResolution.objects.exists())

    def test_contact_resolution_rejects_a_non_integral_contact_id(self):
        """A fractional Contact ID is rejected instead of truncated to another Contact."""
        contact = Contact.objects.create(name="Truncation Target", email="truncation@example.invalid")
        row = self._row(
            _candidate_values={"contact": {"Contact": "candidate@example.invalid"}},
        )
        self._cache_contact_preview(row)

        response = self.client.post(
            reverse("plugins:netbox_data_import:save_resolution"),
            {
                "profile_id": self.profile.pk,
                "source_id": "CONTACT-001",
                "source_column": "candidate:contact",
                "preview_revision": self._preview_revision(),
                "original_value": "",
                "resolved_fields": json.dumps(
                    {
                        "contact_resolution_applied": True,
                        "contact_field_sources": {},
                        "contact_id": contact.pk + 0.9,
                    }
                ),
                "next": reverse("plugins:netbox_data_import:import_preview"),
            },
        )

        self.assertRedirects(
            response,
            reverse("plugins:netbox_data_import:import_preview"),
            fetch_redirect_response=False,
        )
        self.assertFalse(SourceResolution.objects.exists())

    def test_contact_resolution_rejects_a_source_outside_the_row_candidates(self):
        """The resolution boundary rejects source columns that the row did not provide."""
        self._set_extra_columns({"depth": 750})
        row = self._row(
            _candidate_values={"contact": {"Owner": "Candidate Operator"}},
        )
        result = run_import([row], self.profile, {"site": self.site}, dry_run=True)
        user = get_user_model().objects.create_superuser(
            username="contact-candidate-validation-user",
            email="contact-candidate-validation@example.invalid",
            password="testpass",
        )
        self.client.force_login(user)
        session = self.client.session
        session["import_result"] = result.to_session_dict()
        session["import_rows"] = [row]
        session["import_context"] = {
            "profile_id": self.profile.pk,
            "site_id": self.site.pk,
            "location_id": None,
            "tenant_id": None,
            "filename": "contact-candidates.xlsx",
        }
        session["import_preview_pending"] = True
        session[PREVIEW_REVISION_SESSION_KEY] = "contact-sync-preview"
        session.save()

        response = self.client.post(
            reverse("plugins:netbox_data_import:save_resolution"),
            {
                "profile_id": self.profile.pk,
                "source_id": "CONTACT-001",
                "source_column": "candidate:contact",
                "preview_revision": self._preview_revision(),
                "original_value": "",
                "resolved_fields": json.dumps(
                    {
                        "contact_resolution_applied": True,
                        "contact_field_sources": {
                            "name": "Missing Column",
                            "email": "Missing Column",
                        },
                    }
                ),
                "next": reverse("plugins:netbox_data_import:import_preview"),
            },
        )

        self.assertRedirects(
            response,
            reverse("plugins:netbox_data_import:import_preview"),
            fetch_redirect_response=False,
        )
        self.assertFalse(
            SourceResolution.objects.filter(
                profile=self.profile,
                source_id="CONTACT-001",
                source_column="candidate:contact",
            ).exists()
        )

    def test_sync_reuses_contact_by_email_after_name_is_edited(self):
        """Email lookup does not depend on a Contact's editable name."""
        existing_contact = Contact.objects.create(
            name="Edited Contact Name",
            email="Primary.Contact@example.com",
        )

        result = self._sync(self._row(primary_contact="primary.contact@example.com"))

        self.assertFalse(result.has_errors, [row.to_dict() for row in result.rows])
        self.assertEqual(Contact.objects.filter(email__iexact="primary.contact@example.com").count(), 1)
        assignment = ContactAssignment.objects.get(contact=existing_contact, object_id=self.device.pk)
        self.assertEqual(assignment.role, self.contact_role)
        self.assertEqual(assignment.priority, "primary")
        existing_contact.refresh_from_db()
        self.assertEqual(existing_contact.name, "Edited Contact Name")

    def test_sync_is_idempotent_for_explicit_primary_contact(self):
        """Repeated syncs keep one Contact and one assignment."""
        row = self._row(primary_contact="primary.contact@example.com")

        first_result = self._sync(row)
        second_result = self._sync(row)

        self.assertFalse(first_result.has_errors, [item.to_dict() for item in first_result.rows])
        self.assertFalse(second_result.has_errors, [item.to_dict() for item in second_result.rows])
        self.assertEqual(Contact.objects.filter(email__iexact="primary.contact@example.com").count(), 1)
        self.assertEqual(ContactAssignment.objects.filter(object_id=self.device.pk).count(), 1)

    def test_sync_reassigns_the_configured_primary_role_when_source_changes(self):
        """A changed source contact does not leave two primary assignments."""
        previous_contact = Contact.objects.create(
            name="Previous Contact",
            email="previous.contact@example.com",
        )
        assignment = ContactAssignment.objects.create(
            object_type=ContentType.objects.get_for_model(self.device),
            object_id=self.device.pk,
            contact=previous_contact,
            role=self.contact_role,
            priority="primary",
        )

        result = self._sync(self._row(primary_contact="replacement.contact@example.com"))

        self.assertFalse(result.has_errors, [item.to_dict() for item in result.rows])
        assignment.refresh_from_db()
        self.assertEqual(assignment.contact.email, "replacement.contact@example.com")
        self.assertEqual(
            ContactAssignment.objects.filter(
                object_id=self.device.pk,
                role=self.contact_role,
                priority="primary",
            ).count(),
            1,
        )
        self.assertTrue(Contact.objects.filter(pk=previous_contact.pk).exists())

    def test_sync_promotes_an_existing_assignment_for_the_selected_contact(self):
        """The selected Contact becomes primary without creating a duplicate assignment."""
        previous_contact = Contact.objects.create(
            name="Previous Contact",
            email="previous.contact@example.com",
        )
        selected_contact = Contact.objects.create(
            name="Selected Contact",
            email="selected.contact@example.com",
        )
        previous_assignment = ContactAssignment.objects.create(
            object_type=ContentType.objects.get_for_model(self.device),
            object_id=self.device.pk,
            contact=previous_contact,
            role=self.contact_role,
            priority="primary",
        )
        selected_assignment = ContactAssignment.objects.create(
            object_type=ContentType.objects.get_for_model(self.device),
            object_id=self.device.pk,
            contact=selected_contact,
            role=self.contact_role,
            priority="secondary",
        )

        result = self._sync(self._row(primary_contact="selected.contact@example.com"))

        self.assertFalse(result.has_errors, [item.to_dict() for item in result.rows])
        previous_assignment.refresh_from_db()
        selected_assignment.refresh_from_db()
        self.assertEqual(previous_assignment.priority, "secondary")
        self.assertEqual(selected_assignment.priority, "primary")
        self.assertEqual(ContactAssignment.objects.filter(object_id=self.device.pk).count(), 2)

    def test_sync_promotes_a_secondary_assignment_without_a_current_primary(self):
        """An existing secondary assignment becomes the only primary assignment."""
        contact = Contact.objects.create(
            name="Secondary Contact",
            email="secondary.contact@example.invalid",
        )
        assignment = ContactAssignment.objects.create(
            object_type=ContentType.objects.get_for_model(self.device),
            object_id=self.device.pk,
            contact=contact,
            role=self.contact_role,
            priority="secondary",
        )

        result = self._sync(self._row(primary_contact=contact.email))

        self.assertFalse(result.has_errors, [item.to_dict() for item in result.rows])
        assignment.refresh_from_db()
        self.assertEqual(assignment.priority, "primary")
        self.assertEqual(ContactAssignment.objects.filter(object_id=self.device.pk).count(), 1)

    def test_sync_rejects_ambiguous_primary_assignments_for_the_role(self):
        """Two native primary assignments fail instead of selecting one to replace."""
        object_type = ContentType.objects.get_for_model(self.device)
        for index in range(2):
            contact = Contact.objects.create(
                name=f"Existing Primary {index}",
                email=f"existing.primary.{index}@example.com",
            )
            ContactAssignment.objects.create(
                object_type=object_type,
                object_id=self.device.pk,
                contact=contact,
                role=self.contact_role,
                priority="primary",
            )

        result = self._sync(self._row(primary_contact="replacement.contact@example.com"))

        self.assertTrue(result.has_errors)
        self.assertIn("More than one primary assignment", result.rows[0].detail)
        self.assertFalse(Contact.objects.filter(email="replacement.contact@example.com").exists())

    def test_sync_merges_row_extra_data_and_removes_both_contact_values(self):
        """Explicit contact data wins while all contact keys leave extra JSON."""
        result = self._sync(
            self._row(
                primary_contact="explicit.contact@example.com",
                _extra_columns={
                    "primary_contact": "captured.contact@example.com",
                    "room": "Test Room",
                },
            )
        )

        self.assertFalse(result.has_errors, [item.to_dict() for item in result.rows])
        self.device.refresh_from_db()
        self.assertTrue(Contact.objects.filter(email="explicit.contact@example.com").exists())
        self.assertEqual(self._stored_extra_columns(), {"depth": 750, "room": "Test Room"})

    def test_sync_removes_an_empty_legacy_extra_mapping(self):
        """Migrating the last legacy value removes the empty extra mapping."""
        self._set_extra_columns({"primary_contact": "primary.contact@example.com"})
        self.device.save(update_fields=["custom_field_data"])

        result = self._sync()

        self.assertFalse(result.has_errors, [item.to_dict() for item in result.rows])
        self.device.refresh_from_db()
        self.assertEqual(self._stored_extra_columns(), {})

    def test_sync_can_match_primary_contacts_by_name(self):
        """A profile can treat the source contact value as a name."""
        self.profile.adapter_config["primary_contact_lookup_field"] = "name"
        self.profile.save(update_fields=["adapter_config"])

        result = self._sync(self._row(primary_contact="Operations Team"))

        self.assertFalse(result.has_errors, [row.to_dict() for row in result.rows])
        contact = Contact.objects.get(name="Operations Team")
        self.assertEqual(contact.email, "")
        self.assertTrue(ContactAssignment.objects.filter(contact=contact, object_id=self.device.pk).exists())

    def test_sync_requires_a_contact_role_when_contact_data_exists(self):
        """Contact data fails fast when the profile has no assignment role."""
        self.profile.adapter_config["primary_contact_role"] = None
        self.profile.save(update_fields=["adapter_config"])

        result = self._sync(self._row(primary_contact="primary.contact@example.com"))

        self.assertTrue(result.has_errors)
        self.assertIn("Select a primary contact role", result.rows[0].detail)
        self.assertFalse(Contact.objects.exists())
        self.device.refresh_from_db()
        self.assertEqual(self._stored_extra_columns()["primary_contact"], "primary.contact@example.com")

    def test_sync_requires_permission_to_create_a_contact(self):
        """Device change permission does not grant Contact creation permission."""
        user = get_user_model().objects.create_user(username="contact-sync-limited-user")
        self._grant_object_permission(user, Device, ["view", "change"])

        result = run_import(
            [self._row(primary_contact="primary.contact@example.com")],
            self.profile,
            {"site": self.site},
            dry_run=False,
            user=user,
        )

        self.assertTrue(result.has_errors)
        self.assertEqual(result.rows[0].detail, "Permission denied: tenancy.add_contact")
        self.assertFalse(Contact.objects.exists())

    def test_sync_requires_permission_to_view_an_existing_contact(self):
        """A matching Contact outside view scope cannot be assigned."""
        Contact.objects.create(name="Existing Contact", email="primary.contact@example.com")
        user = get_user_model().objects.create_user(username="contact-sync-no-contact-view")
        self._grant_object_permission(user, Device, ["view", "change"])

        result = run_import(
            [self._row(primary_contact="primary.contact@example.com")],
            self.profile,
            {"site": self.site},
            dry_run=False,
            user=user,
        )

        self.assertTrue(result.has_errors)
        self.assertEqual(result.rows[0].detail, "Permission denied: tenancy.view_contact")
        self.assertFalse(ContactAssignment.objects.exists())

    def test_sync_requires_permission_to_create_a_contact_assignment(self):
        """Contact creation permission does not grant assignment creation permission."""
        user = get_user_model().objects.create_user(username="contact-sync-no-assignment-add")
        self._grant_object_permission(user, Device, ["view", "change"])
        self._grant_object_permission(user, Contact, ["add"])

        result = run_import(
            [self._row(primary_contact="primary.contact@example.com")],
            self.profile,
            {"site": self.site},
            dry_run=False,
            user=user,
        )

        self.assertTrue(result.has_errors)
        self.assertEqual(result.rows[0].detail, "Permission denied: tenancy.add_contactassignment")
        self.assertFalse(Contact.objects.exists())
        self.assertFalse(ContactAssignment.objects.exists())

    def test_sync_requires_permission_to_change_a_contact_assignment(self):
        """A source change cannot rewrite an assignment outside change scope."""
        previous_contact = Contact.objects.create(
            name="Previous Contact",
            email="previous.contact@example.com",
        )
        Contact.objects.create(
            name="Selected Contact",
            email="selected.contact@example.com",
        )
        assignment = ContactAssignment.objects.create(
            object_type=ContentType.objects.get_for_model(self.device),
            object_id=self.device.pk,
            contact=previous_contact,
            role=self.contact_role,
            priority="primary",
        )
        user = get_user_model().objects.create_user(username="contact-sync-no-assignment-change")
        self._grant_object_permission(user, Device, ["view", "change"])
        self._grant_object_permission(user, Contact, ["view"])

        result = run_import(
            [self._row(primary_contact="selected.contact@example.com")],
            self.profile,
            {"site": self.site},
            dry_run=False,
            user=user,
        )

        self.assertTrue(result.has_errors)
        self.assertEqual(result.rows[0].detail, "Permission denied: tenancy.change_contactassignment")
        assignment.refresh_from_db()
        self.assertEqual(assignment.contact, previous_contact)

    def test_sync_with_native_contact_permissions_creates_the_assignment(self):
        """Native Contact permissions allow the complete synchronization path."""
        user = get_user_model().objects.create_user(username="contact-sync-authorized")
        self._grant_object_permission(user, Device, ["view", "change"])
        self._grant_object_permission(user, Contact, ["add"])
        self._grant_object_permission(user, ContactAssignment, ["add"])

        result = run_import(
            [self._row(primary_contact="primary.contact@example.com")],
            self.profile,
            {"site": self.site},
            dry_run=False,
            user=user,
        )

        self.assertFalse(result.has_errors, [item.to_dict() for item in result.rows])
        self.assertTrue(ContactAssignment.objects.filter(object_id=self.device.pk).exists())

    def test_email_lookup_rejects_non_email_contact_values(self):
        """Email lookup does not create a Contact from a name-only value."""
        result = self._sync(self._row(primary_contact="Operations Team"))

        self.assertTrue(result.has_errors)
        self.assertIn("valid email", result.rows[0].detail)
        self.assertFalse(Contact.objects.exists())

    def test_preview_rejects_a_non_email_contact_value(self):
        """The approved preview includes contact validation failures."""
        result = run_import(
            [self._row(primary_contact="Operations Team")],
            self.profile,
            {"site": self.site},
            dry_run=True,
        )

        self.assertTrue(result.has_errors)
        self.assertIn("valid email", result.rows[0].detail)
        self.assertFalse(Contact.objects.exists())

    def test_preview_rejects_contact_data_without_a_role(self):
        """The preview fails before a contact can use an undefined role."""
        self.profile.adapter_config["primary_contact_role"] = None
        self.profile.save(update_fields=["adapter_config"])

        result = run_import(
            [self._row(primary_contact="primary.contact@example.com")],
            self.profile,
            {"site": self.site},
            dry_run=True,
        )

        self.assertTrue(result.has_errors)
        self.assertIn("Select a primary contact role", result.rows[0].detail)

    def test_contact_profile_changes_invalidate_an_approved_preview(self):
        """Changing contact assignment policy requires a new approval."""
        from netbox_data_import.views import _previewed_writes_changed

        row = self._row(primary_contact="primary.contact@example.com")
        approved = run_import([row], self.profile, {"site": self.site}, dry_run=True)
        replacement_role = ContactRole.objects.create(name="Replacement Contact", slug="replacement-contact")
        self.profile.adapter_config["primary_contact_role"] = replacement_role.name
        self.profile.save(update_fields=["adapter_config"])

        current = run_import([row], self.profile, {"site": self.site}, dry_run=True)

        self.assertFalse(approved.has_errors)
        self.assertFalse(current.has_errors)
        self.assertTrue(_previewed_writes_changed(approved, current))

    def test_email_lookup_rejects_ambiguous_existing_contacts(self):
        """Duplicate case-insensitive emails fail instead of selecting one contact."""
        Contact.objects.create(name="First Contact", email="primary.contact@example.com")
        Contact.objects.create(name="Second Contact", email="PRIMARY.CONTACT@example.com")

        result = self._sync(self._row(primary_contact="Primary.Contact@example.com"))

        self.assertTrue(result.has_errors)
        self.assertIn("More than one contact", result.rows[0].detail)
        self.assertFalse(ContactAssignment.objects.exists())

    def test_sync_assigns_explicit_primary_contact_to_a_new_device(self):
        """A contact source field creates the native assignment with the device."""
        row = self._row(
            source_id="CONTACT-NEW",
            device_name="new-contact-test-device",
            primary_contact="new.contact@example.com",
        )

        result = self._sync(row)

        self.assertFalse(result.has_errors, [item.to_dict() for item in result.rows])
        device = Device.objects.get(name="new-contact-test-device")
        contact = Contact.objects.get(email="new.contact@example.com")
        assignment = ContactAssignment.objects.get(contact=contact, object_id=device.pk)
        self.assertEqual(assignment.role, self.contact_role)
        self.assertEqual(assignment.priority, "primary")
        self.assertEqual(self._stored_extra_columns(device), {})


class ConcurrentNativeContactSyncTest(TransactionTestCase):
    """Exercise Contact identity serialization with real database transactions."""

    def setUp(self):
        """Create two devices that can synchronize the same new Contact."""
        self.user = get_user_model().objects.create_superuser(
            username="concurrent-contact-user",
            email="concurrent-contact-user@example.invalid",
            password="testpass",
        )
        self.site = Site.objects.create(name="Concurrent Contact Site", slug="concurrent-contact-site")
        manufacturer = Manufacturer.objects.create(name="Concurrent Contact Vendor", slug="concurrent-contact-vendor")
        self.device_type = DeviceType.objects.create(
            manufacturer=manufacturer,
            model="Concurrent Contact Model",
            slug="concurrent-contact-vendor-concurrent-contact-model",
        )
        self.device_role = DeviceRole.objects.create(name="Concurrent Contact Device", slug="concurrent-contact-device")
        contact_role = ContactRole.objects.create(name="Concurrent Primary Contact", slug="concurrent-primary-contact")
        self.profile = ImportProfile.objects.create(
            name="Concurrent Native Contact Sync",
            adapter_config={
                "update_existing": True,
                "create_missing_device_types": False,
                "primary_contact_role": contact_role.name,
                "primary_contact_lookup_field": "email",
            },
        )
        ClassRoleMapping.objects.create(
            profile=self.profile,
            source_class="Server",
            creates_rack=False,
            role_slug=self.device_role.slug,
        )
        self.rows = []
        for index in range(2):
            source_id = f"CONCURRENT-CONTACT-{index}"
            device_name = f"concurrent-contact-device-{index}"
            set_import_source(
                Device.objects.create(
                    name=device_name,
                    site=self.site,
                    device_type=self.device_type,
                    role=self.device_role,
                ),
                self.profile,
                source_id,
            )
            self.rows.append(
                {
                    "_row_number": index + 2,
                    "source_id": source_id,
                    "device_name": device_name,
                    "device_class": "Server",
                    "make": manufacturer.name,
                    "model": self.device_type.model,
                    "u_height": 1,
                    "rack_name": "",
                    "u_position": "",
                    "serial": "",
                    "asset_tag": "",
                    "status": "active",
                    "primary_contact": "shared.contact@example.com",
                }
            )

    def test_parallel_imports_reuse_one_new_contact(self):
        """Concurrent jobs cannot create duplicate case-insensitive identities."""
        start = threading.Barrier(len(self.rows))
        results = []
        errors = []

        def synchronize(row):
            close_old_connections()
            try:
                profile = ImportProfile.objects.get(pk=self.profile.pk)
                site = Site.objects.get(pk=self.site.pk)
                start.wait()
                results.append(run_import([row], profile, {"site": site}, dry_run=False))
            except Exception as exc:  # pragma: no cover - asserted by the main test thread
                start.abort()
                errors.append(exc)
            finally:
                close_old_connections()

        threads = [threading.Thread(target=synchronize, args=(row,)) for row in self.rows]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertTrue(
            all(not result.has_errors for result in results),
            [[row.to_dict() for row in result.rows] for result in results],
        )
        contact = Contact.objects.get(email__iexact="shared.contact@example.com")
        self.assertEqual(ContactAssignment.objects.filter(contact=contact).count(), 2)

    def test_opposite_contact_order_does_not_deadlock_import_jobs(self):
        """Concurrent multi-row Job runners complete when Contact order differs."""
        contact_orders = (
            ("first.contact@example.com", "second.contact@example.com"),
            ("second.contact@example.com", "first.contact@example.com"),
        )
        Contact.objects.bulk_create(
            [
                Contact(name="First Contact", email=contact_orders[0][0]),
                Contact(name="Second Contact", email=contact_orders[0][1]),
            ]
        )
        import_rows = []
        for import_index, contact_order in enumerate(contact_orders):
            rows = []
            for row_index, email in enumerate(contact_order):
                source_id = f"ORDERED-CONTACT-{import_index}-{row_index}"
                device_name = f"ordered-contact-device-{import_index}-{row_index}"
                set_import_source(
                    Device.objects.create(
                        name=device_name,
                        site=self.site,
                        device_type=self.device_type,
                        role=self.device_role,
                    ),
                    self.profile,
                    source_id,
                )
                rows.append(
                    {
                        "_row_number": row_index + 2,
                        "source_id": source_id,
                        "device_name": device_name,
                        "device_class": "Server",
                        "make": self.device_type.manufacturer.name,
                        "model": self.device_type.model,
                        "u_height": 1,
                        "rack_name": "",
                        "u_position": "",
                        "serial": "",
                        "asset_tag": "",
                        "status": "active",
                        "primary_contact": email,
                    }
                )
            import_rows.append(rows)

        start = threading.Barrier(len(import_rows))
        first_rows_written = threading.Barrier(len(import_rows))
        job_inputs = []
        for rows in import_rows:
            context_data = {
                "profile_id": self.profile.pk,
                "site_id": self.site.pk,
                "location_id": None,
                "tenant_id": None,
                "filename": "concurrent-contact-test.xlsx",
            }
            stored_preview = run_import(
                rows,
                self.profile,
                {"site": self.site},
                dry_run=True,
                user=self.user,
            ).to_session_dict()
            job = Job.objects.create(
                name="Data Import",
                user=self.user,
                status="pending",
                job_id=uuid.uuid4(),
                queue_name="default",
                data={"job_type": ImportJobRunner.job_type},
            )
            job_inputs.append((job.pk, rows, context_data, stored_preview))

        errors = []

        class PausingImportJobRunner(ImportJobRunner):
            """Coordinate the first row without changing the production transaction path."""

            @staticmethod
            def _publish_progress(processed, _total):
                if processed != 1:
                    return
                try:
                    first_rows_written.wait(timeout=2)
                except threading.BrokenBarrierError:
                    pass

        def synchronize(job_pk, rows, context_data, stored_preview):
            close_old_connections()
            try:
                job = Job.objects.get(pk=job_pk)
                start.wait(timeout=5)
                PausingImportJobRunner(job).run(rows, context_data, stored_preview)
            except Exception as exc:  # pragma: no cover - asserted by the main test thread
                start.abort()
                first_rows_written.abort()
                errors.append(exc)
            finally:
                close_old_connections()

        threads = [threading.Thread(target=synchronize, args=job_input) for job_input in job_inputs]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        for job_pk, *_ in job_inputs:
            job = Job.objects.get(pk=job_pk)
            self.assertEqual(job.data["phase"], "completed", job.data)
            self.assertFalse(job.data["result"]["has_errors"], job.data)
        self.assertEqual(Contact.objects.filter(email__in=contact_orders[0]).count(), 2)
        self.assertEqual(ContactAssignment.objects.filter(contact__email__in=contact_orders[0]).count(), 4)
