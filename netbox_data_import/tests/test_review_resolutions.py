# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Duplicate review commands replan the stored source through ImportEngine."""

from io import BytesIO
from unittest.mock import patch

import openpyxl
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import Client, TransactionTestCase
from django.urls import reverse

from netbox_data_import.import_engine import ImportEngine
from netbox_data_import.models import (
    ClassRoleMapping,
    ColumnMapping,
    DeviceExistingMatch,
    ImportProfile,
    SourceDocument,
    SourceResolution,
)
from netbox_data_import.object_permissions import ObjectPermissionDenied
from netbox_data_import.preview_row_actions import record_recalculated_preview
from netbox_data_import.review_workspace import ReviewWorkspace


class TargetNeutralDuplicateResolutionTest(TransactionTestCase):
    """Name and serial decisions bind to one current source row and target."""

    def setUp(self):
        """Create a fully mapped profile and an import target."""
        from dcim.models import DeviceRole, DeviceType, Manufacturer, Rack, Site

        self.actor = get_user_model().objects.create_superuser(
            username="duplicate-resolution-operator",
            email="duplicate-resolution@example.invalid",
            password="testpass",
        )
        self.client = Client()
        self.client.force_login(self.actor)
        self.site = Site.objects.create(name="Duplicate Resolution Site", slug="duplicate-resolution-site")
        self.rack = Rack.objects.create(name="resolution-rack", site=self.site, u_height=42)
        manufacturer = Manufacturer.objects.create(name="Resolution Make", slug="resolution-make")
        DeviceType.objects.create(
            manufacturer=manufacturer,
            model="Resolution Model",
            slug="resolution-make-resolution-model",
            u_height=1,
        )
        self.role = DeviceRole.objects.create(name="Resolution Role", slug="resolution-role")
        self.profile = ImportProfile.objects.create(
            name="Duplicate Resolution Profile",
            adapter_config={"sheet_name": "Data", "update_existing": True},
        )
        ClassRoleMapping.objects.create(
            profile=self.profile,
            source_class="Server",
            role_slug=self.role.slug,
        )
        for source_column, target_field in (
            ("Source ID", "source_id"),
            ("Class", "device_class"),
            ("Name", "device_name"),
            ("Rack", "rack_name"),
            ("Make", "make"),
            ("Model", "model"),
            ("Serial", "serial"),
        ):
            ColumnMapping.objects.create(
                profile=self.profile,
                source_column=source_column,
                target_field=target_field,
            )
        self._materialize(duplicate="name")

    def _workbook(self, duplicate):
        """Return a workbook with either one duplicate name or one duplicate serial."""
        book = openpyxl.Workbook()
        sheet = book.active or book.create_sheet()
        sheet.title = "Data"
        sheet.append(["Source ID", "Class", "Name", "Rack", "Make", "Model", "Serial"])
        first_name = "duplicate-device" if duplicate == "name" else "serial-device-a"
        second_name = "duplicate-device" if duplicate == "name" else "serial-device-b"
        first_serial = "DUPLICATE-SERIAL" if duplicate == "serial" else "SERIAL-A"
        second_serial = "DUPLICATE-SERIAL" if duplicate == "serial" else "SERIAL-B"
        sheet.append(
            ["RESOLUTION-A", "Server", first_name, self.rack.name, "Resolution Make", "Resolution Model", first_serial]
        )
        sheet.append(
            [
                "RESOLUTION-B",
                "Server",
                second_name,
                self.rack.name,
                "Resolution Make",
                "Resolution Model",
                second_serial,
            ]
        )
        buffer = BytesIO()
        book.save(buffer)
        return buffer.getvalue()

    def _materialize(self, *, duplicate):
        """Plan a real stored workbook and put that plan in the browser session."""
        content = self._workbook(duplicate)
        document = SourceDocument.store(
            profile=self.profile,
            content=content,
            filename=f"duplicate-{duplicate}.xlsx",
            uploaded_by=self.actor,
        )
        planning_context = {"site_id": self.site.pk, "location_id": None, "tenant_id": None}
        plan = ImportEngine.plan(self.profile, document, self.actor, planning_context)
        workspace = ReviewWorkspace(plan)
        session = self.client.session
        record_recalculated_preview(session, plan)
        session["import_rows"] = workspace.source_rows
        session["import_context"] = {
            "profile_id": self.profile.pk,
            **planning_context,
            "source_document_id": document.pk,
        }
        session["import_preview_pending"] = True
        session.save()
        return document

    def _post_name(self, **values):
        """Resolve the first duplicate name with valid request identity defaults."""
        data = {
            "profile_id": self.profile.pk,
            "row_number": 2,
            "source_id": "RESOLUTION-A",
            "new_name": "resolved-device-a",
            "preview_revision": self.client.session["import_preview_revision"],
            **values,
        }
        return self.client.post(reverse("plugins:netbox_data_import:resolve_duplicate_name"), data)

    def _post_serial(self, **values):
        """Give up the first duplicate serial with valid request identity defaults."""
        data = {
            "profile_id": self.profile.pk,
            "row_number": 2,
            "source_id": "RESOLUTION-A",
            "preview_revision": self.client.session["import_preview_revision"],
            **values,
        }
        return self.client.post(reverse("plugins:netbox_data_import:ignore_duplicate_serial"), data)

    def test_duplicate_name_resolution_replans_for_an_htmx_caller(self):
        """A valid replacement is saved, then an HTMX caller receives the recalculated preview."""
        response = self.client.post(
            reverse("plugins:netbox_data_import:resolve_duplicate_name"),
            {
                "profile_id": self.profile.pk,
                "row_number": 2,
                "source_id": "RESOLUTION-A",
                "new_name": "resolved-device-a",
                "preview_revision": self.client.session["import_preview_revision"],
            },
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        resolution = SourceResolution.objects.get(profile=self.profile, source_id="RESOLUTION-A")
        self.assertEqual(resolution.resolved_fields, {"device_name": "resolved-device-a"})

    def test_duplicate_name_decision_rejects_invalid_request_identity(self):
        """The command rejects an invalid profile, stale preview, row, and source identity."""
        self.assertEqual(self._post_name(profile_id="invalid").status_code, 302)

        other = ImportProfile.objects.create(name="Other Duplicate Profile")
        self.assertEqual(self._post_name(profile_id=other.pk).status_code, 302)

        session = self.client.session
        session["import_preview_pending"] = False
        session.save()
        self.assertEqual(self._post_name().status_code, 302)

        self._materialize(duplicate="name")
        self.assertEqual(self._post_name(preview_revision="stale").status_code, 302)
        self.assertEqual(self._post_name(row_number="invalid").status_code, 302)
        self.assertEqual(self._post_name(source_id="OTHER").status_code, 302)
        self.assertFalse(SourceResolution.objects.filter(profile=self.profile).exists())

    def test_duplicate_name_rejects_invalid_and_claimed_replacements(self):
        """The replacement must fit the model and be unique in both source and target."""
        from dcim.models import Device, DeviceType

        self.assertEqual(self._post_name(new_name="").status_code, 302)
        self.assertEqual(self._post_name(new_name="duplicate-device").status_code, 302)

        Device.objects.create(
            name="claimed-in-netbox",
            site=self.site,
            device_type=DeviceType.objects.get(slug="resolution-make-resolution-model"),
            role=self.role,
        )
        self.assertEqual(self._post_name(new_name="claimed-in-netbox").status_code, 302)
        self.assertFalse(SourceResolution.objects.filter(profile=self.profile).exists())

    def test_duplicate_name_rejects_a_stale_target_and_expected_write_failures(self):
        """Target loss and bounded persistence failures do not leave a resolution."""
        context = self.client.session["import_context"]
        context["site_id"] = 999999
        session = self.client.session
        session["import_context"] = context
        session.save()
        self.assertEqual(self._post_name().status_code, 302)

        self._materialize(duplicate="name")
        endpoint = "netbox_data_import.views.save_permission_scoped_object"
        for failure in (ObjectPermissionDenied("denied"), ValidationError("invalid"), IntegrityError("duplicate")):
            with self.subTest(failure=type(failure).__name__), patch(endpoint, side_effect=failure):
                self.assertEqual(self._post_name().status_code, 302)

    def test_duplicate_serial_resolution_replans_the_stored_source(self):
        """Giving up a serial is saved only while another current row still claims it."""
        self._materialize(duplicate="serial")

        response = self._post_serial()

        self.assertEqual(response.status_code, 302)
        resolution = SourceResolution.objects.get(profile=self.profile, source_id="RESOLUTION-A")
        self.assertEqual(resolution.resolved_fields, {"serial": ""})

    def test_duplicate_serial_rejects_missing_and_changed_evidence(self):
        """The current plan, source row, stored bytes, and fresh replan must agree."""
        self.assertEqual(self._post_serial().status_code, 302)

        self._materialize(duplicate="serial")
        session = self.client.session
        session["import_rows"][0]["serial"] = ""
        session.save()
        self.assertEqual(self._post_serial().status_code, 302)

        document = self._materialize(duplicate="serial")
        document.delete()
        self.assertEqual(self._post_serial().status_code, 302)

        self._materialize(duplicate="serial")
        SourceResolution.objects.create(
            profile=self.profile,
            source_id="RESOLUTION-B",
            source_column="serial",
            original_value="DUPLICATE-SERIAL",
            resolved_fields={"serial": ""},
        )
        self.assertEqual(self._post_serial().status_code, 302)
        self.assertFalse(SourceResolution.objects.filter(profile=self.profile, source_id="RESOLUTION-A").exists())

    def test_duplicate_serial_sanitizes_expected_write_failures(self):
        """Permission, validation, and concurrency failures return to the current preview."""
        endpoint = "netbox_data_import.views.save_permission_scoped_object"
        for failure in (ObjectPermissionDenied("denied"), ValidationError("invalid"), IntegrityError("duplicate")):
            with self.subTest(failure=type(failure).__name__):
                self._materialize(duplicate="serial")
                with patch(endpoint, side_effect=failure):
                    self.assertEqual(self._post_serial().status_code, 302)

    def test_manual_device_match_rechecks_row_device_scope_and_existing_bindings(self):
        """A manual link names one active row and one unclaimed Device at the import site."""
        from dcim.models import Device, DeviceType, Site

        endpoint = reverse("plugins:netbox_data_import:match_existing_device")
        device_type = DeviceType.objects.get(slug="resolution-make-resolution-model")
        target = Device.objects.create(
            name="manual-match-target",
            site=self.site,
            device_type=device_type,
            role=self.role,
        )
        response = self.client.post(
            endpoint,
            {"profile_id": self.profile.pk, "source_id": "UNKNOWN", "netbox_device_id": target.pk},
        )
        self.assertEqual(response.status_code, 302)

        response = self.client.post(
            endpoint,
            {"profile_id": self.profile.pk, "source_id": "RESOLUTION-A", "netbox_device_id": "invalid"},
        )
        self.assertEqual(response.status_code, 302)

        other_site = Site.objects.create(name="Manual Match Other Site", slug="manual-match-other-site")
        outside = Device.objects.create(
            name="manual-match-outside",
            site=other_site,
            device_type=device_type,
            role=self.role,
        )
        response = self.client.post(
            endpoint,
            {"profile_id": self.profile.pk, "source_id": "RESOLUTION-A", "netbox_device_id": outside.pk},
        )
        self.assertEqual(response.status_code, 302)

        DeviceExistingMatch.objects.create(
            profile=self.profile,
            source_id="OTHER-SOURCE",
            netbox_device_id=target.pk,
            device_name=target.name,
        )
        response = self.client.post(
            endpoint,
            {"profile_id": self.profile.pk, "source_id": "RESOLUTION-A", "netbox_device_id": target.pk},
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(DeviceExistingMatch.objects.filter(source_id="RESOLUTION-A").exists())

    def test_manual_device_match_sanitizes_expected_write_failures(self):
        """Permission, validation, and concurrency failures never leave a partial link."""
        from dcim.models import Device, DeviceType

        target = Device.objects.create(
            name="manual-match-write-target",
            site=self.site,
            device_type=DeviceType.objects.get(slug="resolution-make-resolution-model"),
            role=self.role,
        )
        endpoint = reverse("plugins:netbox_data_import:match_existing_device")
        writer = "netbox_data_import.views.save_permission_scoped_object"
        for failure in (ObjectPermissionDenied("denied"), ValidationError("invalid"), IntegrityError("duplicate")):
            with self.subTest(failure=type(failure).__name__), patch(writer, side_effect=failure):
                response = self.client.post(
                    endpoint,
                    {
                        "profile_id": self.profile.pk,
                        "source_id": "RESOLUTION-A",
                        "netbox_device_id": target.pk,
                    },
                )
                self.assertEqual(response.status_code, 302)

    def test_auto_match_rejects_an_inactive_preview_and_a_stale_target(self):
        """Auto-match uses only the active plan and its still-visible target."""
        endpoint = reverse("plugins:netbox_data_import:auto_match_devices")
        other = ImportProfile.objects.create(name="Auto Match Other Profile")
        response = self.client.post(endpoint, {"profile_id": other.pk})
        self.assertEqual(response.status_code, 302)

        session = self.client.session
        session["import_context"]["site_id"] = 999999
        session.save()
        response = self.client.post(endpoint, {"profile_id": self.profile.pk})
        self.assertEqual(response.status_code, 302)

    def test_contact_lookup_returns_real_visible_contact_shapes(self):
        """The picker searches name, email, and phone and returns bounded Contact data."""
        from tenancy.models import Contact

        contact = Contact.objects.create(
            name="Resolution Contact",
            email="resolution-contact@example.invalid",
            phone="+1-555-0100",
        )
        endpoint = reverse("plugins:netbox_data_import:contact_lookup")
        self.assertEqual(self.client.get(endpoint, {"q": "r"}).json(), {"results": []})

        response = self.client.get(endpoint, {"q": "resolution"})

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

    def test_contact_suggestion_rejects_a_missing_profile(self):
        """A stale picker cannot resolve candidates against a deleted profile."""
        response = self.client.get(
            reverse("plugins:netbox_data_import:contact_suggestion"),
            {"profile_id": 999999, "source_id": "RESOLUTION-A"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("valid import profile", response.json()["error"])

    def test_quick_class_mapping_rejects_unknown_actions_and_empty_roles(self):
        """The preview shortcut accepts only one complete class-policy action."""
        endpoint = reverse("plugins:netbox_data_import:quick_add_class_mapping")
        response = self.client.post(
            endpoint,
            {"profile_id": self.profile.pk, "source_class": "Switch", "mapping_action": "unknown"},
        )
        self.assertEqual(response.status_code, 302)
        response = self.client.post(
            endpoint,
            {"profile_id": self.profile.pk, "source_class": "Switch", "mapping_action": "role"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.profile.class_role_mappings.filter(source_class="Switch").exists())

    def test_quick_role_creation_rejects_an_invalid_profile_identity(self):
        """The JSON shortcut requires a valid integer profile identity."""
        response = self.client.post(
            reverse("plugins:netbox_data_import:quick_create_role"),
            {"profile_id": "invalid", "name": "Switch", "slug": "switch"},
        )

        self.assertEqual(response.status_code, 400)
