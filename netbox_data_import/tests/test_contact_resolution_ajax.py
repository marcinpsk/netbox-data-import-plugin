# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Saving a Contact resolution answers the row action contract instead of re-rendering.

A full preview render costs about six times the engine run it wraps, and it drops the operator
back at the top of the page. The Contact save now joins the deferred row actions: it stores the
decision, marks the preview stale, and leaves the recalculation to the operator.
"""

import json

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from unittest.mock import patch
from django.test import TestCase
from django.urls import reverse

from netbox_data_import.engine import reapply_saved_resolutions, run_import
from netbox_data_import.models import ClassRoleMapping, ColumnMapping, ImportProfile, SourceResolution
from netbox_data_import.preview_row_actions import (
    PREVIEW_DIRTY_SESSION_KEY,
    PREVIEW_REVISION_SESSION_KEY,
)
from netbox_data_import.tests.helpers import make_dcim_objects

JSON = "application/json"


class ContactResolutionAjaxTest(TestCase):
    """The save endpoint answers JSON for the modal and keeps the redirect for a plain form."""

    def setUp(self):
        """Put one device row that needs a Contact decision into the preview session."""
        self.site, self.manufacturer, self.device_type, self.role = make_dcim_objects("CtcAjax")
        self.profile = ImportProfile.objects.create(
            name="ContactAjaxProfile",
            adapter_config={"sheet_name": "Data", "source_id_column": "Id", "update_existing": True},
        )
        for source, target in {
            "Id": "source_id",
            "Name": "device_name",
            "Class": "device_class",
            "Make": "make",
            "Model": "model",
            "Contact": "candidate:contact",
            "Contact Number": "candidate:contact",
        }.items():
            ColumnMapping.objects.create(profile=self.profile, source_column=source, target_field=target)
        ClassRoleMapping.objects.create(
            profile=self.profile, source_class="Server", creates_rack=False, role_slug=self.role.slug
        )

        self.row = {
            "_row_number": 1,
            "source_id": "AJAX-001",
            "device_name": "ajax-contact-device",
            "device_class": "Server",
            "make": "CtcAjaxMfg",
            "model": "CtcAjaxModel",
            "_candidate_values": {
                "contact": {
                    "Contact": "ajax.person@example.invalid",
                    "Contact Number": "+1 202-555-0180",
                }
            },
        }
        result = run_import([self.row], self.profile, {"site": self.site}, dry_run=True)

        user = get_user_model().objects.create_superuser(
            username="contact-ajax-user",
            email="contact-ajax@example.invalid",
            password="testpass",
        )
        self.client.force_login(user)
        session = self.client.session
        session["import_result"] = result.to_session_dict()
        session["import_rows"] = [self.row]
        session["import_context"] = {
            "profile_id": self.profile.pk,
            "site_id": self.site.pk,
            "location_id": None,
            "tenant_id": None,
            "filename": "contact-ajax.xlsx",
        }
        session["import_preview_pending"] = True
        session[PREVIEW_REVISION_SESSION_KEY] = "revision-one"
        session[PREVIEW_DIRTY_SESSION_KEY] = False
        session.save()

    def _payload(self, **overrides):
        payload = {
            "profile_id": self.profile.pk,
            "source_id": "AJAX-001",
            "source_column": "candidate:contact",
            "resolved_fields": json.dumps(
                {
                    "contact_resolution_applied": True,
                    "contact_field_sources": {"name": "Contact", "email": "Contact"},
                    "contact_field_values": {},
                    "contact_id": None,
                }
            ),
            "preview_revision": "revision-one",
            "next": reverse("plugins:netbox_data_import:import_preview"),
        }
        payload.update(overrides)
        return payload

    def _post(self, **overrides):
        return self.client.post(
            reverse("plugins:netbox_data_import:save_resolution"),
            self._payload(**overrides),
            HTTP_ACCEPT=JSON,
        )

    def test_the_modal_gets_json_instead_of_a_rendered_preview(self):
        """The response is the deferred row-action envelope the preview scripts already read."""
        response = self._post()

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response["Content-Type"].split(";")[0], JSON)
        body = json.loads(response.content)
        self.assertEqual(body["ok"], True)
        self.assertEqual(body["preview_state"], "recalculation_required")
        self.assertIn("message", body)

    def test_the_decision_is_stored(self):
        """The saved row is what a later recalculation replays, so it must survive the AJAX call."""
        self._post()

        resolution = SourceResolution.objects.get(
            profile=self.profile,
            source_id="AJAX-001",
            source_column="candidate:contact",
        )
        self.assertEqual(resolution.resolved_fields["contact_field_sources"]["email"], "Contact")

    def test_the_preview_is_marked_stale(self):
        """The row still shows the old action, so the page must ask for a recalculation."""
        self.assertIs(self.client.session.get(PREVIEW_DIRTY_SESSION_KEY), False)

        self._post()

        self.assertIs(self.client.session.get(PREVIEW_DIRTY_SESSION_KEY), True)

    def test_a_stale_preview_revision_is_refused(self):
        """A second tab can recalculate between opening the modal and saving it."""
        response = self._post(preview_revision="revision-zero")

        self.assertEqual(response.status_code, 409, response.content)
        body = json.loads(response.content)
        self.assertIs(body["ok"], False)
        self.assertIn("reload the preview", body["error"].lower())
        self.assertFalse(SourceResolution.objects.filter(source_id="AJAX-001").exists())

    def test_a_queued_import_refuses_a_later_decision(self):
        """Run Import consumes the rows it queued, so a decision saved after it never applies."""
        session = self.client.session
        session["import_preview_pending"] = False
        session.save()

        response = self._post()

        self.assertEqual(response.status_code, 409, response.content)
        self.assertFalse(SourceResolution.objects.filter(source_id="AJAX-001").exists())

    def test_an_invalid_decision_answers_json_not_a_redirect(self):
        """The modal shows the reason inline, so a rejected save must not answer with HTML."""
        response = self._post(
            resolved_fields=json.dumps(
                {
                    "contact_resolution_applied": True,
                    "contact_field_sources": {"email": "No Such Column"},
                    "contact_field_values": {},
                    "contact_id": None,
                }
            )
        )

        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response["Content-Type"].split(";")[0], JSON)
        body = json.loads(response.content)
        self.assertIs(body["ok"], False)
        self.assertTrue(body["error"])
        self.assertFalse(SourceResolution.objects.filter(source_id="AJAX-001").exists())

    def test_the_envelope_names_the_row(self):
        """The row action contract carries the row number, so the caller can address the row."""
        body = json.loads(self._post().content)

        self.assertEqual(body["row_number"], 1)

    def test_a_json_caller_never_gets_a_redirect(self):
        """`fetch` follows a redirect, which would recalculate the preview and rotate its revision."""
        response = self._post(source_column="")

        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response["Content-Type"].split(";")[0], JSON)
        self.assertIs(json.loads(response.content)["ok"], False)

    def test_a_form_post_also_marks_the_preview_stale(self):
        """The rendered rows go stale whichever path saved the decision."""
        self.client.post(reverse("plugins:netbox_data_import:save_resolution"), self._payload())

        self.assertIs(self.client.session.get(PREVIEW_DIRTY_SESSION_KEY), True)

    def test_a_queued_import_refuses_a_decision_from_the_form_path_too(self):
        """Without scripts the same decision would still never reach the queued run."""
        session = self.client.session
        session["import_preview_pending"] = False
        session.save()

        response = self.client.post(reverse("plugins:netbox_data_import:save_resolution"), self._payload())

        self.assertEqual(response.status_code, 302)
        self.assertFalse(SourceResolution.objects.filter(source_id="AJAX-001").exists())

    def test_restoring_a_replacement_preview_retires_the_open_tab(self):
        """A worker preview can match a Device the open tab never saw, so its token must expire."""
        import uuid

        from core.models import Job

        from netbox_data_import.views import _restore_import_session

        before = self.client.session[PREVIEW_REVISION_SESSION_KEY]
        session = self.client.session
        session["import_preview_pending"] = False
        session.save()
        job = Job.objects.create(
            name="Data Import",
            status="errored",
            job_id=uuid.uuid4(),
            queue_name="default",
            data={
                "job_type": "netbox_data_import.import",
                "preview_result": session["import_result"],
                "context_data": session["import_context"],
            },
        )

        request = self.client.request().wsgi_request
        request.session = self.client.session
        with patch("netbox_data_import.views._import_source_rows_available", return_value=True):
            _restore_import_session(request, job)

        self.assertIs(request.session.get("import_preview_pending"), True)
        self.assertNotEqual(request.session[PREVIEW_REVISION_SESSION_KEY], before)

    def test_a_queued_import_refuses_a_conflict_merge_too(self):
        """`_merge_*` is replayed onto the rows as well, so it is preview-coupled the same way."""
        session = self.client.session
        session["import_preview_pending"] = False
        session.save()

        self.client.post(
            reverse("plugins:netbox_data_import:save_resolution"),
            self._payload(source_column="_merge_serial", resolved_fields=json.dumps({"serial": "ABC"})),
        )

        self.assertFalse(SourceResolution.objects.filter(source_column="_merge_serial").exists())

    def test_a_resolution_with_no_preview_in_the_session_is_still_saved(self):
        """A decision saved outside a preview is standalone and must not need one."""
        session = self.client.session
        for key in ("import_rows", "import_context", "import_result", "import_preview_pending"):
            session.pop(key, None)
        session.save()

        self.client.post(
            reverse("plugins:netbox_data_import:save_resolution"),
            {
                "profile_id": self.profile.pk,
                "source_id": "STANDALONE-1",
                "source_column": "device_name",
                "original_value": "old",
                "resolved_fields": json.dumps({"device_name": "new"}),
                "next": "/",
            },
        )

        self.assertTrue(SourceResolution.objects.filter(source_id="STANDALONE-1").exists())

    def test_the_native_contact_form_carries_the_preview_revision(self):
        """Without scripts the form is the only thing that can present a token to check."""
        response = self.client.get(reverse("plugins:netbox_data_import:import_preview"))

        self.assertContains(response, 'name="preview_revision"')

    def test_a_stale_revision_is_refused_on_the_form_path_when_it_supplies_one(self):
        """The rendered page carries its own token, so a retired one must not be honoured."""
        response = self.client.post(
            reverse("plugins:netbox_data_import:save_resolution"),
            self._payload(preview_revision="revision-zero"),
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(SourceResolution.objects.filter(source_id="AJAX-001").exists())

    def test_a_plain_form_post_still_redirects(self):
        """The form works without scripts, so the browser path must keep its redirect."""
        response = self.client.post(
            reverse("plugins:netbox_data_import:save_resolution"),
            self._payload(),
        )

        self.assertRedirects(
            response,
            reverse("plugins:netbox_data_import:import_preview"),
            fetch_redirect_response=False,
        )
        self.assertTrue(SourceResolution.objects.filter(source_id="AJAX-001").exists())

    def test_a_decision_on_a_matched_device_reports_the_contact_write(self):
        """When the row already points at a Device the save applies the Contact at once."""
        from dcim.models import Device
        from tenancy.models import ContactRole

        role = ContactRole.objects.create(name="CtcAjax Primary", slug="ctcajax-primary")
        self.profile.adapter_config["primary_contact_role"] = role.name
        self.profile.save()
        device = Device.objects.create(
            name="ajax-contact-device",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
        )
        # The first decision unblocks the row, so the second one meets a matched device.
        self._post()
        rows = reapply_saved_resolutions([self.row], self.profile)
        result = run_import(rows, self.profile, {"site": self.site}, dry_run=True)
        device_row = next(r for r in result.rows if r.object_type == "device")
        self.assertEqual(device_row.extra_data.get("netbox_device_id"), device.pk)

        session = self.client.session
        session["import_result"] = result.to_session_dict()
        session["import_rows"] = rows
        session[PREVIEW_REVISION_SESSION_KEY] = "revision-two"
        session.save()

        response = self._post(preview_revision="revision-two")

        self.assertEqual(response.status_code, 200, response.content)
        body = json.loads(response.content)
        self.assertIn("Device Contact", body["message"])
        self.assertEqual(body["preview_state"], "recalculation_required")
        # The message names a Contact write, so the assignment has to exist.
        from tenancy.models import ContactAssignment

        assignment = ContactAssignment.objects.get(
            object_id=device.pk,
            object_type=ContentType.objects.get_for_model(Device),
        )
        self.assertEqual(assignment.contact.email, "ajax.person@example.invalid")
        self.assertEqual(assignment.role, role)
