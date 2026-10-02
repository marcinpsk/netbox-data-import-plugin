# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for Contact resolution through the deferred row-action endpoint."""

import json

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from netbox_data_import.models import (
    ClassRoleMapping,
    ColumnMapping,
    ImportProfile,
    PreviewCoordinator,
    PreviewState,
    SourceResolution,
)
from netbox_data_import.tests.helpers import (
    make_dcim_objects,
    preview_claim,
    preview_coordinator,
    seed_preview,
    store_plan,
    store_workbook_document,
    stored_plan,
)

JSON = "application/json"


class ContactResolutionSessionMixin:
    """Seed the preview with one device row that still needs a Contact decision."""

    def _reread(self):
        """Re-read the preview from NetBox, as the page's button does after a change made elsewhere."""
        response = self.client.post(reverse("plugins:netbox_data_import:preview_reread"), preview_claim(self.client))
        self.assertEqual(response.status_code, 302, response.content[:300])

    def _stale_claim(self):
        """Return the claim the page held before a re-read retired it."""
        claim = preview_claim(self.client)
        self._reread()
        return claim

    def _submit(self, status="pending"):
        """Leave the preview submitted on a final import Job, as Run Import does, and return the Job."""
        import uuid

        from core.models import Job

        job = Job.objects.create(
            name="Data Import",
            user=self.user,
            status=status,
            job_id=uuid.uuid4(),
            data={"job_type": "netbox_data_import.import"},
        )
        PreviewCoordinator.objects.filter(pk=preview_coordinator(self.client).pk).update(
            state=PreviewState.SUBMITTED, job_id=job.pk
        )
        return job

    def _stored_device_row(self):
        """Return the Device row of the plan the preview now stores."""
        from netbox_data_import.plan import ImportPlan
        from netbox_data_import.review_workspace import ReviewWorkspace

        workspace = ReviewWorkspace(ImportPlan.from_dict(stored_plan(self.client)), self.user)
        return next(unit for unit in workspace.units if unit.object_type == "device")

    def _matched_device(self):
        """Give the row a matched Device and a profile role, then let the first decision replan onto it."""
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
        self._reread()
        # The first decision unblocks the row, so the second one meets a matched device.
        self.assertEqual(self._post_decision({"name": "Contact", "email": "Contact"}).status_code, 200)
        self.assertEqual(self._stored_device_row().extra_data.get("netbox_device_id"), device.pk)
        return device

    def _post_decision(self, sources, *, values=None):
        """Save one Contact decision for the row through the deferred endpoint."""
        return self.client.post(
            reverse("plugins:netbox_data_import:save_resolution"),
            {
                **preview_claim(self.client),
                "source_id": "AJAX-001",
                "source_column": "candidate:contact",
                "resolved_fields": json.dumps(
                    {
                        "contact_resolution_applied": True,
                        "contact_field_sources": sources,
                        "contact_field_values": values or {},
                        "contact_id": None,
                    }
                ),
                "next": reverse("plugins:netbox_data_import:import_preview"),
            },
            HTTP_ACCEPT=JSON,
        )

    def setUp(self):
        """Put one device row that needs a Contact decision into the preview."""
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

        from netbox_data_import.import_engine import ImportEngine

        self.document = store_workbook_document(
            self.profile,
            ["Id", "Name", "Class", "Make", "Model", "Contact", "Contact Number"],
            [
                [
                    self.row["source_id"],
                    self.row["device_name"],
                    self.row["device_class"],
                    self.row["make"],
                    self.row["model"],
                    self.row["_candidate_values"]["contact"]["Contact"],
                    self.row["_candidate_values"]["contact"]["Contact Number"],
                ]
            ],
            user,
            "contact-ajax.xlsx",
        )
        self.planning_context = {"site_id": self.site.pk, "location_id": None, "tenant_id": None}
        seed_preview(
            self.client,
            profile=self.profile,
            document=self.document,
            plan=ImportEngine.plan(self.profile, self.document, user, self.planning_context),
            context={**self.planning_context, "filename": "contact-ajax.xlsx"},
        )


class ContactResolutionAjaxTest(ContactResolutionSessionMixin, TestCase):
    """The save endpoint answers JSON for the modal and keeps the redirect for a plain form."""

    def _payload(self, **overrides):
        payload = {
            **preview_claim(self.client),
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
        self.assertEqual(body["preview_state"], "replanned")
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

    def test_the_decision_replans_the_preview(self):
        """The command replays the decision into the stored plan and moves the claim forward."""
        before = preview_coordinator(self.client).revision
        self.assertNotIn("source_contact_resolution_applied", self._stored_device_row().extra_data, "fixture")

        self._post()

        self.assertEqual(preview_coordinator(self.client).revision, before + 1)
        self.assertIs(self._stored_device_row().extra_data["source_contact_resolution_applied"], True)

    def test_a_stale_preview_claim_is_refused(self):
        """A second tab can re-read the preview between opening the modal and saving it."""
        response = self._post(**self._stale_claim())

        self.assertEqual(response.status_code, 409, response.content)
        body = json.loads(response.content)
        self.assertIs(body["ok"], False)
        self.assertEqual(body["code"], "preview_stale")
        self.assertIn("reload it", body["error"].lower())
        self.assertFalse(SourceResolution.objects.filter(source_id="AJAX-001").exists())

    def test_a_queued_import_refuses_a_later_decision(self):
        """Run Import consumes the rows it queued, so a decision saved after it never applies."""
        self._submit()

        response = self._post()

        self.assertEqual(response.status_code, 409, response.content)
        self.assertIn("import already started", response.json()["error"])
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

    def test_an_unknown_contact_resolution_key_uses_contact_vocabulary(self):
        """The modal must describe a Contact-policy error without naming Target Fields."""
        response = self._post(
            resolved_fields=json.dumps(
                {
                    "contact_resolution_applied": True,
                    "contact_field_sources": {"name": "Contact", "email": "Contact"},
                    "contact_field_values": {},
                    "contact_id": None,
                    "unexpected_contact_field": True,
                }
            )
        )

        self.assertEqual(response.status_code, 400, response.content)
        error = response.json()["error"]
        self.assertIn("not a Contact resolution field", error)
        self.assertNotIn("Target Field", error)
        self.assertFalse(SourceResolution.objects.filter(source_id="AJAX-001").exists())

    def test_model_validation_uses_contact_vocabulary_for_an_unknown_key(self):
        """Every SourceResolution writer must report the same Contact-policy vocabulary."""
        resolution = SourceResolution(
            profile=self.profile,
            source_id="AJAX-001",
            source_column="candidate:contact",
            original_value="{}",
            resolved_fields={"unexpected_contact_field": True},
        )

        with self.assertRaisesMessage(ValidationError, "not a Contact resolution field"):
            resolution.full_clean()

    def test_malformed_candidate_values_answer_json_not_an_internal_error(self):
        """A serialized plan can be stale or corrupt, so its display data is untrusted input."""
        plan = stored_plan(self.client)
        device_unit = next(unit for unit in plan["units"] if unit["display"].get("source_id") == "AJAX-001")
        device_unit["display"].setdefault("extra_data", {})["candidate_values"] = ["invalid"]
        store_plan(self.client, plan)

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

    def test_a_form_post_also_replans_the_preview(self):
        """The rendered rows go stale whichever path saved the decision, so the form path replans too."""
        before = preview_coordinator(self.client).revision

        response = self.client.post(reverse("plugins:netbox_data_import:save_resolution"), self._payload())

        self.assertEqual(response.status_code, 302)
        self.assertEqual(preview_coordinator(self.client).revision, before + 1)
        self.assertIs(self._stored_device_row().extra_data["source_contact_resolution_applied"], True)

    def test_a_queued_import_refuses_a_decision_from_the_form_path_too(self):
        """Without scripts the same decision would still never reach the queued run."""
        self._submit()

        response = self.client.post(reverse("plugins:netbox_data_import:save_resolution"), self._payload())

        self.assertContains(response, "The import already started", status_code=409)
        self.assertFalse(SourceResolution.objects.filter(source_id="AJAX-001").exists())

    def test_restoring_a_failed_import_retires_the_open_tab(self):
        """A restored preview can match a Device the open tab never saw, so its claim must expire."""
        job = self._submit(status="errored")
        before = preview_claim(self.client)

        restored = self.client.post(reverse("plugins:netbox_data_import:import_restore", kwargs={"pk": job.pk}), before)

        self.assertEqual(restored.status_code, 302, restored.content[:300])
        self.assertEqual(preview_coordinator(self.client).state, PreviewState.READY)
        response = self._post(**before)
        self.assertEqual(response.status_code, 409, response.content)
        self.assertFalse(SourceResolution.objects.filter(source_id="AJAX-001").exists())

    def test_a_queued_import_refuses_a_conflict_merge_too(self):
        """`_merge_*` is replayed onto the rows as well, so it is preview-coupled the same way."""
        self._submit()

        response = self.client.post(
            reverse("plugins:netbox_data_import:save_resolution"),
            self._payload(source_column="_merge_serial", resolved_fields=json.dumps({"serial": "ABC"})),
        )

        self.assertEqual(response.status_code, 409)
        self.assertFalse(SourceResolution.objects.filter(source_column="_merge_serial").exists())

    def test_a_queued_import_refuses_a_duplicate_name_resolution(self):
        """The endpoint refuses a replacement name once Run Import has queued the rows."""
        self._submit()

        response = self.client.post(
            reverse("plugins:netbox_data_import:resolve_duplicate_name"),
            {
                **preview_claim(self.client),
                "source_id": "AJAX-001",
                "row_number": 2,
                "new_name": "queued-name-resolution",
                "next": reverse("plugins:netbox_data_import:import_preview"),
            },
            follow=True,
        )

        self.assertFalse(SourceResolution.objects.filter(source_column="device_name").exists())
        self.assertContains(response, "The import already started", status_code=409)

    def test_a_queued_duplicate_name_refusal_redirects_htmx(self):
        """A refused decision must navigate, not swap a preview the queued import has frozen."""
        self._submit()
        next_url = reverse("plugins:netbox_data_import:import_preview")

        response = self.client.post(
            reverse("plugins:netbox_data_import:resolve_duplicate_name"),
            {
                **preview_claim(self.client),
                "source_id": "AJAX-001",
                "row_number": 2,
                "new_name": "queued-htmx-name-resolution",
                "next": next_url,
            },
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.headers["HX-Redirect"], next_url)
        self.assertFalse(SourceResolution.objects.filter(source_column="device_name").exists())

    def test_an_ordinary_resolution_cannot_replace_the_source_identity(self):
        """The resolution endpoint rejects a target-neutral planning key."""
        response = self._post(
            source_column="device_name",
            resolved_fields=json.dumps({"source_id": "REPLACED"}),
        )

        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("reserved", response.json()["error"])
        self.assertFalse(SourceResolution.objects.filter(source_id="AJAX-001").exists())

    def test_the_native_contact_form_carries_the_preview_claim(self):
        """Without scripts the form is the only thing that can present a claim to check."""
        response = self.client.get(reverse("plugins:netbox_data_import:import_preview"))

        html = response.content.decode()
        form_start = html.index('id="contactCandidateForm"')
        form = html[form_start : html.index("</form>", form_start)]
        claim = preview_claim(self.client)
        self.assertIn(f'name="preview_token" value="{claim["preview_token"]}"', form)
        self.assertIn('name="preview_revision"', form)

    def test_a_stale_claim_is_refused_on_the_form_path(self):
        """The rendered page carries its own claim, so a retired one must not be honoured."""
        response = self.client.post(
            reverse("plugins:netbox_data_import:save_resolution"), self._payload(**self._stale_claim())
        )

        self.assertEqual(response.status_code, 409)
        self.assertFalse(SourceResolution.objects.filter(source_id="AJAX-001").exists())

    def test_an_active_preview_refuses_a_form_post_without_a_claim(self):
        """An incomplete active-preview form cannot bypass the claim check."""
        payload = self._payload()
        payload.pop("preview_revision")

        response = self.client.post(
            reverse("plugins:netbox_data_import:save_resolution"),
            payload,
        )

        self.assertEqual(response.status_code, 409)
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
        from tenancy.models import ContactAssignment, ContactRole

        device = self._matched_device()
        ContactAssignment.objects.all().delete()

        response = self._post()

        self.assertEqual(response.status_code, 200, response.content)
        body = json.loads(response.content)
        self.assertIn("Device Contact", body["message"])
        self.assertEqual(body["preview_state"], "replanned")
        # The message names a Contact write, so the assignment has to exist.
        assignment = ContactAssignment.objects.get(
            object_id=device.pk,
            object_type=ContentType.objects.get_for_model(Device),
        )
        self.assertEqual(assignment.contact.email, "ajax.person@example.invalid")
        self.assertEqual(assignment.role, ContactRole.objects.get(slug="ctcajax-primary"))


class MatchedDeviceContactReportTest(ContactResolutionSessionMixin, TestCase):
    """The response must claim a Device Contact write only when one happened."""

    def test_a_no_contact_decision_on_a_matched_device_claims_no_write(self):
        """`apply()` writes nothing for a no-contact decision, so the message must not claim one."""
        from tenancy.models import ContactAssignment

        device = self._matched_device()
        ContactAssignment.objects.all().delete()

        response = self._post_decision({})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertNotIn("Device Contact", json.loads(response.content)["message"])
        self.assertFalse(
            ContactAssignment.objects.filter(
                object_id=device.pk,
                object_type=ContentType.objects.get_for_model(device),
            ).exists()
        )

    def test_a_decision_that_assigns_a_contact_still_reports_the_write(self):
        """The report must stay for the case it was written for."""
        self._matched_device()

        response = self._post_decision({"name": "Contact", "email": "Contact"})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn("Device Contact", json.loads(response.content)["message"])


class MatchedDeviceContactDetailTest(ContactResolutionSessionMixin, TestCase):
    """The matched path reports the Contact it created, and never claims an assignment it kept."""

    def test_a_contact_created_for_a_matched_device_is_named_in_the_detail(self):
        """The unmatched path reports its Contact write, so the matched path must not stay silent."""
        self._matched_device()

        response = self._post_decision(
            {},
            values={"name": "Second Person", "email": "second.person@example.invalid"},
        )

        self.assertEqual(response.status_code, 200, response.content)
        body = json.loads(response.content)
        self.assertIn("Second Person", body["detail"])
        self.assertIn("was created in NetBox", body["detail"])

    def test_the_saved_resolution_names_the_contact_the_matched_path_created(self):
        """Reopening the row must show the Contact this save created, not an unlinked candidate."""
        from tenancy.models import Contact

        self._matched_device()

        self._post_decision(
            {},
            values={"name": "Second Person", "email": "second.person@example.invalid"},
        )

        contact = Contact.objects.get(email="second.person@example.invalid")
        saved = SourceResolution.objects.get(
            profile=self.profile, source_id="AJAX-001", source_column="candidate:contact"
        )
        self.assertEqual(saved.resolved_fields["contact_id"], contact.pk)

    def test_the_response_returns_the_resolution_that_was_saved(self):
        """The page stores what it gets back, so the response has to carry the persisted identity."""
        from tenancy.models import Contact

        self._matched_device()

        response = self._post_decision(
            {},
            values={"name": "Second Person", "email": "second.person@example.invalid"},
        )

        contact = Contact.objects.get(email="second.person@example.invalid")
        resolution = json.loads(response.content)["resolution"]
        self.assertEqual(resolution["resolved_fields"]["contact_id"], contact.pk)
        # The picker rebuilds from the page's own data, so it needs the Contact itself to show it.
        self.assertEqual(
            resolution["contact"],
            {
                "id": contact.pk,
                "name": "Second Person",
                "email": "second.person@example.invalid",
                "phone": contact.phone,
            },
        )

    def test_an_unmoved_assignment_does_not_claim_a_contact_update(self):
        """`apply` returns a plan for an unchanged assignment, which is not a Device Contact write."""
        self._matched_device()
        first = self._post_decision({"name": "Contact", "email": "Contact"})
        self.assertIn("was updated", json.loads(first.content)["message"])

        response = self._post_decision({"name": "Contact", "email": "Contact"})

        self.assertEqual(response.status_code, 200, response.content)
        message = json.loads(response.content)["message"]
        self.assertIn("already stood as decided", message)
        self.assertNotIn("was updated", message)


class RefusedRowContactAssignmentTest(ContactResolutionSessionMixin, TestCase):
    """A refused row still names the Device it matched, but its Contact must not reach it."""

    def _refused_row_device(self):
        """Match the row to a Device another source row already owns, then let the first decision replan onto it."""
        from dcim.models import Device
        from tenancy.models import ContactRole

        from netbox_data_import.models import DeviceExistingMatch

        role = ContactRole.objects.create(name="CtcAjax Primary", slug="ctcajax-primary")
        self.profile.adapter_config["primary_contact_role"] = role.name
        self.profile.save()
        device = Device.objects.create(
            name="ajax-contact-device",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
        )
        DeviceExistingMatch.objects.create(
            profile=self.profile,
            source_id="AJAX-OTHER",
            netbox_device_id=device.pk,
            device_name=device.name,
        )
        self._reread()
        # The first decision settles the Contact question, so the replan reaches the binding check.
        self.assertEqual(self._post_decision({"name": "Contact", "email": "Contact"}).status_code, 200)
        return device, self._stored_device_row()

    def test_a_refused_row_writes_no_contact_onto_the_device_it_named(self):
        """`device.already_bound` still carries `netbox_device_id`, and the row plans no update."""
        from tenancy.models import ContactAssignment

        device, row = self._refused_row_device()
        self.assertEqual(row.action, "error", "the row must be refused for this test to mean anything")
        self.assertEqual(row.extra_data.get("netbox_device_id"), device.pk)
        ContactAssignment.objects.all().delete()

        response = self._post_decision({"name": "Contact", "email": "Contact"})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(
            ContactAssignment.objects.filter(
                object_id=device.pk,
                object_type=ContentType.objects.get_for_model(device),
            ).exists(),
            "the import refused this row, so its Contact must not reach the Device",
        )


class ContactSuggestionEndpointTest(ContactResolutionSessionMixin, TestCase):
    """The picker asks the server on open, so a Contact created since the preview is offered."""

    def _suggest(self, **overrides):
        """Ask the endpoint for one row's current Contact suggestion."""
        params = {**preview_claim(self.client), "source_id": "AJAX-001"}
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
        """The suggestion reads the stored preview, so it must name one active row."""
        response = self._suggest(source_id="NOT-A-ROW")

        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("one active preview row", json.loads(response.content)["error"])

    def test_a_retired_adapter_is_refused_instead_of_raising(self):
        """The open picker outlives an upgrade, so the row can name a profile the release dropped."""
        ImportProfile.objects.filter(pk=self.profile.pk).update(source_adapter="retired_adapter")

        response = self._suggest()

        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("retired_adapter", json.loads(response.content)["error"])

    def test_a_request_without_a_claim_is_refused(self):
        """A request that names no preview cannot be tied to one."""
        response = self.client.get(
            reverse("plugins:netbox_data_import:contact_suggestion"), {"source_id": "AJAX-001"}, HTTP_ACCEPT=JSON
        )

        self.assertEqual(response.status_code, 409, response.content)

    def test_a_stale_claim_is_refused(self):
        """The picker of a page another tab re-read must not read the newer preview."""
        from tenancy.models import Contact

        Contact.objects.create(name="Ajax Person", email="ajax.person@example.invalid")

        response = self._suggest(**self._stale_claim())

        self.assertEqual(response.status_code, 409, response.content)
        self.assertNotIn("suggestion", response.json())


class ContactCreatedOnSaveTest(ContactResolutionSessionMixin, TestCase):
    """A row that still has to create its Device has nothing to assign a Contact to yet.

    The Contact itself is stored as soon as the decision is saved, because the operator would
    otherwise only get it by syncing the row or running the whole import.
    """

    def _payload(self, **overrides):
        payload = {
            **preview_claim(self.client),
            "source_id": "AJAX-001",
            "source_column": "candidate:contact",
            "resolved_fields": json.dumps(
                {
                    "contact_resolution_applied": True,
                    "contact_field_sources": {"email": "Contact", "phone": "Contact Number"},
                    "contact_field_values": {"name": "Ajax Person"},
                    "contact_id": None,
                }
            ),
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

    def _saved_resolution(self):
        return SourceResolution.objects.get(
            profile=self.profile,
            source_id="AJAX-001",
            source_column="candidate:contact",
        )

    def test_the_contact_is_stored_when_the_decision_is_saved(self):
        """This is the whole point: the Contact exists in NetBox before any import runs."""
        from tenancy.models import Contact

        response = self._post()

        self.assertEqual(response.status_code, 200, response.content)
        contact = Contact.objects.get(email="ajax.person@example.invalid")
        self.assertEqual(contact.name, "Ajax Person")
        self.assertEqual(contact.phone, "+1 202-555-0180")

    def test_the_message_names_the_contact_it_created(self):
        """A silent write to NetBox is the one thing the operator must not have to guess at."""
        body = json.loads(self._post().content)

        self.assertIn("Ajax Person", body["message"])
        self.assertIn("created", body["message"].lower())
        self.assertEqual(body["preview_state"], "replanned")

    def test_the_resolution_records_the_contact_it_created(self):
        """The stored decision names the Contact, so planning reuses it instead of proposing one."""
        from tenancy.models import Contact

        self._post()

        contact = Contact.objects.get(email="ajax.person@example.invalid")
        self.assertEqual(self._saved_resolution().resolved_fields["contact_id"], contact.pk)

    def test_saving_the_same_decision_twice_creates_one_contact(self):
        """The second save meets the Contact the first one made, so it must reuse it."""
        from tenancy.models import Contact

        self.assertEqual(self._post().status_code, 200)

        response = self._post()

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(Contact.objects.filter(email="ajax.person@example.invalid").count(), 1)

    def test_a_contact_that_already_exists_is_reused_not_duplicated(self):
        """The lookup field identifies the Contact, so a stored one answers for these values."""
        from tenancy.models import Contact

        existing = Contact.objects.create(name="Already Here", email="ajax.person@example.invalid")

        body = json.loads(self._post().content)

        self.assertEqual(Contact.objects.filter(email="ajax.person@example.invalid").count(), 1)
        self.assertEqual(self._saved_resolution().resolved_fields["contact_id"], existing.pk)
        self.assertIn("already", body["message"].lower())
        # Reuse must not rewrite the stored Contact from the source row.
        existing.refresh_from_db()
        self.assertEqual(existing.name, "Already Here")

    def test_a_selected_contact_whose_lookup_value_moved_is_refused(self):
        """`_plan` rejects this later, so storing it here reports success on a resolution that fails."""
        from tenancy.models import Contact

        selected = Contact.objects.create(name="Moved Person", email="moved.person@example.invalid")

        response = self._post(
            resolved_fields=json.dumps(
                {
                    "contact_resolution_applied": True,
                    "contact_field_sources": {"email": "Contact"},
                    "contact_field_values": {"name": "Ajax Person"},
                    "contact_id": selected.pk,
                }
            )
        )

        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("no longer has the chosen email value", json.loads(response.content)["error"])
        self.assertFalse(SourceResolution.objects.filter(source_id="AJAX-001").exists())

    def test_the_response_names_the_netbox_write_in_its_own_field(self):
        """The modal closes on save, so the page needs the write in a field it can keep showing."""
        body = json.loads(self._post().content)

        self.assertIn("Ajax Person", body["detail"])
        self.assertIn("created", body["detail"].lower())

    def test_a_decision_that_writes_nothing_reports_no_detail(self):
        """A recorded decision is not news, so it must not claim a NetBox write."""
        body = json.loads(
            self._post(
                resolved_fields=json.dumps(
                    {
                        "contact_resolution_applied": True,
                        "contact_field_sources": {},
                        "contact_field_values": {},
                        "contact_id": None,
                    }
                )
            ).content
        )

        self.assertEqual(body["detail"], "")

    def test_no_contact_for_this_row_creates_nothing(self):
        """An operator who answered "no contact" is not asking for one to be made."""
        from tenancy.models import Contact

        response = self._post(
            resolved_fields=json.dumps(
                {
                    "contact_resolution_applied": True,
                    "contact_field_sources": {},
                    "contact_field_values": {},
                    "contact_id": None,
                }
            )
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(Contact.objects.count(), 0)

    def test_linking_an_existing_contact_creates_nothing(self):
        """The operator already chose the Contact, so nothing new belongs in NetBox."""
        from tenancy.models import Contact

        linked = Contact.objects.create(name="Linked Person", email="linked@example.invalid")

        response = self._post(
            resolved_fields=json.dumps(
                {
                    "contact_resolution_applied": True,
                    "contact_field_sources": {},
                    "contact_field_values": {},
                    "contact_id": linked.pk,
                }
            )
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(Contact.objects.count(), 1)
        self.assertEqual(self._saved_resolution().resolved_fields["contact_id"], linked.pk)

    def test_a_refused_decision_stores_no_contact(self):
        """A rejected save must leave NetBox exactly as it was."""
        from tenancy.models import Contact

        response = self._post(**self._stale_claim())

        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(Contact.objects.count(), 0)
