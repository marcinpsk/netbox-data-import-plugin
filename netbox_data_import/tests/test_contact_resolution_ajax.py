# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for Contact resolution through the deferred row-action endpoint."""

import json

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.urls import reverse

from netbox_data_import.models import ClassRoleMapping, ColumnMapping, ImportProfile, SourceDocument, SourceResolution
from netbox_data_import.preview_row_actions import (
    PREVIEW_DIRTY_SESSION_KEY,
    PREVIEW_REVISION_SESSION_KEY,
    record_recalculated_preview,
)
from netbox_data_import.tests.helpers import make_dcim_objects

JSON = "application/json"


class ContactResolutionSessionMixin:
    """Seed the preview session with one device row that still needs a Contact decision."""

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
            "_row_number": 2,
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
        user = get_user_model().objects.create_superuser(
            username="contact-ajax-user",
            email="contact-ajax@example.invalid",
            password="testpass",
        )
        self.user = user
        self.client.force_login(user)
        import io

        import openpyxl
        from openpyxl.worksheet.worksheet import Worksheet

        from netbox_data_import.import_engine import ImportEngine
        from netbox_data_import.review_workspace import ReviewWorkspace

        workbook = openpyxl.Workbook()
        worksheet = workbook.active
        if not isinstance(worksheet, Worksheet):
            worksheet = workbook.create_sheet()
        worksheet.title = "Data"
        worksheet.append(["Id", "Name", "Class", "Make", "Model", "Contact", "Contact Number"])
        worksheet.append(
            [
                self.row["source_id"],
                self.row["device_name"],
                self.row["device_class"],
                self.row["make"],
                self.row["model"],
                self.row["_candidate_values"]["contact"]["Contact"],
                self.row["_candidate_values"]["contact"]["Contact Number"],
            ]
        )
        content = io.BytesIO()
        workbook.save(content)
        self.document = SourceDocument.store(
            profile=self.profile,
            content=content.getvalue(),
            filename="contact-ajax.xlsx",
            uploaded_by=user,
        )
        self.planning_context = {"site_id": self.site.pk, "location_id": None, "tenant_id": None}
        plan = ImportEngine.plan(self.profile, self.document, user, self.planning_context)
        workspace = ReviewWorkspace(plan)
        session = self.client.session
        record_recalculated_preview(session, plan)
        session["import_rows"] = workspace.source_rows
        session["import_context"] = {
            "profile_id": self.profile.pk,
            "site_id": self.site.pk,
            "location_id": None,
            "tenant_id": None,
            "filename": "contact-ajax.xlsx",
            "source_document_id": self.document.pk,
        }
        session["import_preview_pending"] = True
        session[PREVIEW_REVISION_SESSION_KEY] = "revision-one"
        session[PREVIEW_DIRTY_SESSION_KEY] = False
        session.save()


class ContactResolutionAjaxTest(ContactResolutionSessionMixin, TestCase):
    """The save endpoint answers JSON for the modal and keeps the redirect for a plain form."""

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

    def test_malformed_candidate_values_answer_json_not_an_internal_error(self):
        """A serialized plan can be stale or corrupt, so its display data is untrusted input."""
        session = self.client.session
        device_unit = next(
            unit for unit in session["import_plan"]["units"] if unit["display"].get("source_id") == "AJAX-001"
        )
        device_unit["display"].setdefault("extra_data", {})["candidate_values"] = ["invalid"]
        session.save()

        response = self._post()

        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response["Content-Type"].split(";")[0], JSON)
        self.assertIs(json.loads(response.content)["ok"], False)
        self.assertFalse(SourceResolution.objects.filter(source_id="AJAX-001").exists())

    def test_the_envelope_names_the_row(self):
        """The row action contract carries the row number, so the caller can address the row."""
        body = json.loads(self._post().content)

        self.assertEqual(body["row_number"], 2)

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
                "accepted_plan": session["import_plan"],
                "context_data": session["import_context"],
                "source_document_id": self.document.pk,
            },
        )
        session.save()

        request = self.client.request().wsgi_request
        request.session = self.client.session
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

    def test_a_queued_import_refuses_a_duplicate_name_resolution(self):
        """The endpoint refuses a replacement name once Run Import has queued the rows."""
        session = self.client.session
        session["import_preview_pending"] = False
        session.save()

        response = self.client.post(
            reverse("plugins:netbox_data_import:resolve_duplicate_name"),
            {
                "profile_id": self.profile.pk,
                "source_id": "AJAX-001",
                "row_number": 1,
                "new_name": "queued-name-resolution",
                "next": reverse("plugins:netbox_data_import:import_preview"),
            },
            follow=True,
        )

        self.assertFalse(SourceResolution.objects.filter(source_column="device_name").exists())
        self.assertContains(response, "The import already started")

    def test_a_queued_duplicate_name_refusal_redirects_htmx(self):
        """A refused decision must navigate, not swap a preview the queued import has frozen."""
        session = self.client.session
        session["import_preview_pending"] = False
        session.save()
        next_url = reverse("plugins:netbox_data_import:import_preview")

        response = self.client.post(
            reverse("plugins:netbox_data_import:resolve_duplicate_name"),
            {
                "profile_id": self.profile.pk,
                "source_id": "AJAX-001",
                "row_number": 1,
                "new_name": "queued-htmx-name-resolution",
                "next": next_url,
            },
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.headers["HX-Redirect"], next_url)
        self.assertFalse(SourceResolution.objects.filter(source_column="device_name").exists())

    def test_a_resolution_with_no_preview_in_the_session_is_still_saved(self):
        """A decision saved outside a preview is standalone and must not need one."""
        session = self.client.session
        for key in ("import_rows", "import_context", "import_plan", "import_preview_pending"):
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

        html = response.content.decode()
        form_start = html.index('id="contactCandidateForm"')
        form_end = html.index("</form>", form_start)
        self.assertIn('name="preview_revision"', html[form_start:form_end])

    def test_a_stale_revision_is_refused_on_the_form_path_when_it_supplies_one(self):
        """The rendered page carries its own token, so a retired one must not be honoured."""
        response = self.client.post(
            reverse("plugins:netbox_data_import:save_resolution"),
            self._payload(preview_revision="revision-zero"),
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(SourceResolution.objects.filter(source_id="AJAX-001").exists())

    def test_an_active_preview_refuses_a_form_post_without_a_revision(self):
        """An incomplete active-preview form cannot bypass the revision check."""
        payload = self._payload()
        payload.pop("preview_revision")

        response = self.client.post(
            reverse("plugins:netbox_data_import:save_resolution"),
            payload,
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

        from netbox_data_import.import_engine import ImportEngine
        from netbox_data_import.review_workspace import ReviewWorkspace

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
        plan = ImportEngine.plan(
            self.profile,
            self.document,
            self.user,
            self.planning_context,
        )
        result = ReviewWorkspace(plan)
        device_row = next(row for row in result.units if row.object_type == "device")
        self.assertEqual(device_row.extra_data.get("netbox_device_id"), device.pk)

        session = self.client.session
        record_recalculated_preview(session, plan)
        session["import_rows"] = result.source_rows
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


class ContactSuggestionEndpointTest(ContactResolutionSessionMixin, TestCase):
    """The picker asks the server on open, so a Contact created since the preview is offered."""

    def _suggest(self, **overrides):
        """Ask the endpoint for one row's current Contact suggestion."""
        params = {"profile_id": self.profile.pk, "source_id": "AJAX-001"}
        params.update(overrides)
        return self.client.get(
            reverse("plugins:netbox_data_import:contact_suggestion"),
            params,
            HTTP_ACCEPT=JSON,
        )

    def test_no_matching_contact_suggests_nothing(self):
        """The row's candidate values identify no Contact, so the picker stays empty."""
        response = self._suggest()

        self.assertEqual(response.status_code, 200, response.content)
        self.assertIsNone(json.loads(response.content)["suggestion"])

    def test_a_contact_created_after_the_preview_is_offered(self):
        """This is the answer the page's baked map cannot give without a recalculation."""
        from tenancy.models import Contact

        contact = Contact.objects.create(name="Ajax Person", email="ajax.person@example.invalid")

        response = self._suggest()

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(json.loads(response.content)["suggestion"]["id"], contact.pk)

    def test_a_contact_without_an_email_is_still_offered(self):
        """The lookup field is email, so a Contact carrying only the row's phone matched nothing."""
        from tenancy.models import Contact

        contact = Contact.objects.create(name="Ajax Phone Person", email="", phone="+1 202-555-0180")

        response = self._suggest()

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(json.loads(response.content)["suggestion"]["id"], contact.pk)

    def test_a_contact_deleted_after_the_preview_is_no_longer_offered(self):
        """The page still holds the deleted Contact, so the endpoint has to answer that it is gone."""
        from tenancy.models import Contact

        contact = Contact.objects.create(name="Ajax Person", email="ajax.person@example.invalid")
        self.assertEqual(json.loads(self._suggest().content)["suggestion"]["id"], contact.pk)

        contact.delete()

        response = self._suggest()

        self.assertEqual(response.status_code, 200, response.content)
        self.assertIsNone(json.loads(response.content)["suggestion"])

    def test_a_row_outside_the_active_preview_is_refused(self):
        """The suggestion reads session state, so it must name one active row."""
        response = self._suggest(source_id="NOT-A-ROW")

        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("one active preview row", json.loads(response.content)["error"])

    def test_a_retired_adapter_is_refused_instead_of_raising(self):
        """The open picker outlives an upgrade, so the row can name a profile the release dropped."""
        ImportProfile.objects.filter(pk=self.profile.pk).update(source_adapter="retired_adapter")

        response = self._suggest()

        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("retired_adapter", json.loads(response.content)["error"])

    def test_a_missing_profile_is_refused(self):
        """A request that names no profile cannot be tied to a preview."""
        response = self._suggest(profile_id="")

        self.assertEqual(response.status_code, 400, response.content)
