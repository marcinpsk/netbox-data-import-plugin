# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Map a PortClass label outside the fixed vocabulary, on the profile or for one preview."""

from html.parser import HTMLParser
from io import BytesIO

from django.core.exceptions import ValidationError
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from netbox_data_import.models import ImportProfile, PortClassMapping
from netbox_data_import.plan import Disposition
from netbox_data_import.tests.helpers import (
    preview_claim,
    preview_coordinator,
    trace_termination,
    trace_workbook_bytes,
    upload_preview,
)
from netbox_data_import.tests.mixins import IsolatedRQQueueTestMixin
from netbox_data_import.tests.test_cable_module import DEVICE_B, CableTopologyMixin, direct_path

WIRELESS = trace_termination("DEV-A", "", "eth0", "802.11 Port")
LABEL = "802.11 Port"
# A trace whose Devices NetBox does not hold, so it stays listed before the mapped one and never resolves.
ELSEWHERE = direct_path(trace_termination("DEVICE-X", "", "p1", "NIC"), trace_termination("DEVICE-Y", "", "p2", "NIC"))


class _SelectScrollParser(HTMLParser):
    """Record, per workspace section, whether a select in it sits inside a `.table-responsive` box."""

    VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"})

    def __init__(self):
        super().__init__()
        self.stack: list[tuple[str, str | None, bool]] = []
        self.sections: dict[str, bool] = {}

    def handle_starttag(self, tag, attrs):
        if tag in self.VOID:
            return
        names = dict(attrs)
        section = next((name for name in names if name.startswith("data-trace-")), None)
        scrolls = "table-responsive" in (names.get("class") or "").split()
        self.stack.append((tag, section, scrolls))
        if tag == "select":
            owner = next((entry[1] for entry in reversed(self.stack) if entry[1]), "")
            clipped = any(entry[2] for entry in self.stack)
            self.sections[owner] = self.sections.get(owner, False) or clipped

    def handle_endtag(self, tag):
        while self.stack and self.stack.pop()[0] != tag:
            pass


def _select_sections(html):
    """Return each section that holds a select, mapped to whether a scroll box clips one of them."""
    parser = _SelectScrollParser()
    parser.feed(html)
    return parser.sections


class _PortClassWorkspaceMixin:
    """Upload a trace whose one endpoint states a PortClass label outside the fixed vocabulary."""

    def upload(self, *blocks):
        """Start a preview of *blocks*, by default the one wireless trace, and return the workspace page."""
        upload = BytesIO(trace_workbook_bytes(path_blocks=blocks or (direct_path(WIRELESS, DEVICE_B),)))
        upload.name = "traces.xlsx"
        setup = upload_preview(
            self.client, {"profile": self.profile.pk, "site": self.site.pk, "excel_file": upload}, follow=True
        )
        self.assertEqual(setup.status_code, 200)
        return self.workspace()

    def workspace(self, **params):
        """Return the rendered workspace page of the current preview."""
        return self.client.get(reverse("plugins:netbox_data_import:trace_workspace"), params)

    def wireless_trace(self, page):
        """Return the workspace entry of the trace that names DEV-A."""
        return next(trace for trace in page.context["traces"] if "DEV-A" in trace.trace_identity)

    def map_port_class(self, scope, port_class="NIC", label=LABEL, **extra):
        """Post one PortClass decision through the workspace route and follow its redirect."""
        data = {
            **preview_claim(self.client),
            "port_class_label": label,
            "port_class": port_class,
            "scope": scope,
            **extra,
        }
        return self.client.post(reverse("plugins:netbox_data_import:trace_port_class"), data, follow=True)

    def clear_port_class(self, scope, label=LABEL):
        """Post the Clear of one PortClass mapping through the workspace route."""
        data = {**preview_claim(self.client), "port_class_label": label, "scope": scope, "clear": "1"}
        return self.client.post(reverse("plugins:netbox_data_import:trace_port_class"), data, follow=True)

    def reread(self):
        """Re-read the preview from NetBox with the claim the page holds."""
        data = {**preview_claim(self.client), "next": reverse("plugins:netbox_data_import:trace_workspace")}
        return self.client.post(reverse("plugins:netbox_data_import:preview_reread"), data, follow=True)


class TraceWorkspacePortClassTest(_PortClassWorkspaceMixin, CableTopologyMixin, TransactionTestCase):
    """The PortClass section of the trace workspace, end to end through its route."""

    def setUp(self):
        self.build_topology()
        self.client.force_login(self.actor)

    def test_an_unknown_label_is_invalid_and_the_workspace_offers_a_row_for_it(self):
        page = self.upload()

        trace = self.wireless_trace(page)
        self.assertEqual(trace.disposition, Disposition.INVALID)
        self.assertEqual([finding["code"] for finding in trace.findings], ["trace.unknown_port_class"])
        (row,) = page.context["port_class_rows"]
        self.assertEqual((row["label"], row["port_class"], row["source"], row["trace_count"]), (LABEL, "", "", 1))
        self.assertContains(page, 'data-port-class-label="802.11 Port"')

    def test_no_select_sits_in_a_scroll_box_that_clips_its_dropdown(self):
        """A `.table-responsive` box clips the Tom Select dropdown of a select inside it."""
        page = self.upload()

        sections = _select_sections(page.content.decode())

        self.assertIn("data-trace-port-classes", sections)
        self.assertIn("data-trace-cable-policy", sections)
        self.assertEqual([section for section, clipped in sections.items() if clipped], [])

    def test_a_profile_mapping_turns_the_trace_valid_and_reopens_the_same_trace(self):
        self.upload(ELSEWHERE, direct_path(WIRELESS, DEVICE_B))
        before = preview_coordinator(self.client).revision

        saved = self.map_port_class("profile", trace=self.wireless_trace(self.workspace()).identity)

        self.assertEqual(saved.status_code, 200)
        self.assertContains(saved, "PortClass set to &#x27;NIC&#x27; for label &#x27;802.11 Port&#x27; in the profile.")
        row = PortClassMapping.objects.get(profile=self.profile, source_label=LABEL)
        self.assertEqual(row.port_class, "NIC")
        self.assertEqual(preview_coordinator(self.client).revision, before + 1)
        self.assertNotIn("DEV-A", saved.context["traces"][0].trace_identity)
        selected = saved.context["selected_trace"]
        self.assertIn("DEV-A", selected.trace_identity)
        self.assertEqual(selected.disposition, Disposition.ACTIONABLE)
        (state,) = saved.context["port_class_rows"]
        self.assertEqual((state["port_class"], state["source"]), ("NIC", "profile"))

    def test_a_preview_mapping_survives_reread_and_ends_with_the_preview(self):
        self.upload()

        mapped = self.map_port_class("preview")

        self.assertFalse(PortClassMapping.objects.exists())
        self.assertEqual(self.wireless_trace(mapped).disposition, Disposition.ACTIONABLE)
        self.assertEqual(preview_coordinator(self.client).context["port_class_map"], {LABEL: "NIC"})
        (state,) = mapped.context["port_class_rows"]
        self.assertEqual((state["port_class"], state["source"]), ("NIC", "preview"))

        reread = self.reread()
        self.assertEqual(reread.status_code, 200)
        self.assertEqual(self.wireless_trace(self.workspace()).disposition, Disposition.ACTIONABLE)

        fresh = self.upload()
        self.assertEqual(self.wireless_trace(fresh).disposition, Disposition.INVALID)
        self.assertNotIn("port_class_map", preview_coordinator(self.client).context)

    def test_a_preview_mapping_takes_priority_over_the_profile_mapping(self):
        PortClassMapping.objects.create(profile=self.profile, source_label=LABEL, port_class="Position Front")
        page = self.upload()
        (profile_state,) = page.context["port_class_rows"]
        self.assertEqual((profile_state["port_class"], profile_state["source"]), ("Position Front", "profile"))
        self.assertNotEqual(self.wireless_trace(page).disposition, Disposition.ACTIONABLE)

        mapped = self.map_port_class("preview", port_class="NIC")

        (state,) = mapped.context["port_class_rows"]
        self.assertEqual((state["port_class"], state["source"]), ("NIC", "preview"))
        self.assertEqual(self.wireless_trace(mapped).disposition, Disposition.ACTIONABLE)

    def test_clear_removes_a_preview_mapping_and_then_a_profile_mapping(self):
        PortClassMapping.objects.create(profile=self.profile, source_label=LABEL, port_class="Position Front")
        self.upload()
        self.map_port_class("preview", port_class="NIC")

        cleared_preview = self.clear_port_class("preview")

        self.assertContains(cleared_preview, "PortClass cleared for label &#x27;802.11 Port&#x27; in this preview.")
        self.assertNotIn("port_class_map", preview_coordinator(self.client).context)
        (state,) = cleared_preview.context["port_class_rows"]
        self.assertEqual((state["port_class"], state["source"]), ("Position Front", "profile"))

        cleared_profile = self.clear_port_class("profile")

        self.assertFalse(PortClassMapping.objects.exists())
        (state,) = cleared_profile.context["port_class_rows"]
        self.assertEqual((state["port_class"], state["source"]), ("", ""))
        self.assertEqual(self.wireless_trace(cleared_profile).disposition, Disposition.INVALID)

    def test_clear_of_a_mapping_that_does_not_exist_is_refused(self):
        self.upload()

        for scope, message in (
            ("preview", "This preview maps no PortClass label &#x27;802.11 Port&#x27;."),
            ("profile", "The profile maps no PortClass label &#x27;802.11 Port&#x27;."),
        ):
            with self.subTest(scope=scope):
                self.assertContains(self.clear_port_class(scope), message)

    def test_the_command_refuses_a_fixed_label_an_unstated_label_and_a_target_outside_the_vocabulary(self):
        self.upload()
        before = preview_coordinator(self.client).revision

        for scope in ("profile", "preview"):
            with self.subTest(scope=scope):
                fixed = self.map_port_class(scope, label="NIC")
                self.assertContains(fixed, "&#x27;NIC&#x27; is a fixed PortClass, so it takes no mapping.")
                unstated = self.map_port_class(scope, label="Undefined")
                self.assertContains(unstated, "This preview states no such PortClass label.")
                outside = self.map_port_class(scope, port_class="Wireless")
                self.assertContains(outside, "&#x27;Wireless&#x27; is not a fixed PortClass.")

        self.assertFalse(PortClassMapping.objects.exists())
        coordinator = preview_coordinator(self.client)
        self.assertEqual(coordinator.revision, before)
        self.assertNotIn("port_class_map", coordinator.context)

    def test_the_drift_check_on_load_finds_no_drift_after_a_preview_mapping(self):
        self.upload()
        self.map_port_class("preview")

        page = self.workspace()

        self.assertFalse(page.context["drift"])
        self.assertEqual(page.context["summary"]["preview_state"], "current")
        sync = next(action for action in self.wireless_trace(page).actions if action.key == "sync")
        self.assertTrue(sync.enabled, sync.reason)


class TracePortClassWorkerTest(
    _PortClassWorkspaceMixin, IsolatedRQQueueTestMixin, CableTopologyMixin, TransactionTestCase
):
    """The import worker replans with the preview's PortClass map, so it finds the unit the operator selected."""

    def setUp(self):
        super().setUp()
        self.build_topology()
        self.client.force_login(self.actor)

    def test_a_queued_sync_after_a_preview_mapping_writes_the_selected_trace(self):
        from core.choices import JobStatusChoices
        from core.models import Job
        from dcim.models import Cable
        from django_rq import get_queue

        from netbox_data_import.jobs import ImportJobRunner
        from netbox_data_import.models import ImportExecution

        self.upload()
        mapped = self.map_port_class("preview")
        trace = self.wireless_trace(mapped)

        queued = self.client.post(
            reverse("plugins:netbox_data_import:trace_sync"),
            {"selection": trace.sync_selection, **preview_claim(self.client)},
        )
        self.assertEqual(queued.status_code, 302, queued.content[:500])
        job = Job.objects.get(data__job_type=ImportJobRunner.job_type)
        ImportJobRunner.handle(**get_queue(job.queue_name).fetch_job(str(job.job_id)).kwargs)

        job.refresh_from_db()
        self.assertEqual(job.status, JobStatusChoices.STATUS_COMPLETED, job.error)
        execution = ImportExecution.objects.get()
        self.assertEqual(execution.outcome, "succeeded", execution.failure_detail)
        cable = Cable.objects.get()
        self.assertEqual(
            {termination.termination for termination in cable.terminations.all()},
            {self.eth0, self.eth1},
        )


class PortClassMappingValidationTest(TestCase):
    """The model and its form accept only a label outside the vocabulary, mapped to a fixed PortClass."""

    @classmethod
    def setUpTestData(cls):
        cls.profile = ImportProfile.objects.create(name="PortClass traces", source_adapter="trace_workbook")

    def test_the_model_refuses_a_fixed_label_and_a_target_outside_the_vocabulary(self):
        PortClassMapping(profile=self.profile, source_label=LABEL, port_class="NIC").full_clean()
        for label, port_class, field in (("NIC", "Port", "source_label"), (LABEL, "Wireless", "port_class")):
            with self.subTest(label=label, port_class=port_class):
                with self.assertRaises(ValidationError) as raised:
                    PortClassMapping(profile=self.profile, source_label=label, port_class=port_class).full_clean()
                self.assertIn(field, raised.exception.message_dict)

    def test_the_form_refuses_a_fixed_label(self):
        from netbox_data_import.forms import PortClassMappingForm

        form = PortClassMappingForm(
            {"source_label": "Switch Port", "port_class": "NIC"},
            instance=PortClassMapping(profile=self.profile),
        )

        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors["source_label"], ["'Switch Port' is a fixed PortClass, so it takes no mapping."])

    def test_a_flat_profile_cannot_hold_a_portclass_mapping(self):
        flat = ImportProfile.objects.create(name="Flat devices")

        with self.assertRaises(ValidationError):
            PortClassMapping(profile=flat, source_label=LABEL, port_class="NIC").full_clean()


class PortClassMappingProfileViewTest(TestCase):
    """The profile page lists PortClass mappings and adds, edits and deletes them."""

    @classmethod
    def setUpTestData(cls):
        from django.contrib.auth import get_user_model

        cls.actor = get_user_model().objects.create_superuser("port-class-ui", "port-class-ui@example.com", "x")
        cls.trace_profile = ImportProfile.objects.create(name="PortClass UI", source_adapter="trace_workbook")
        cls.flat_profile = ImportProfile.objects.create(name="Flat PortClass UI")

    def setUp(self):
        self.client.force_login(self.actor)

    def test_only_a_trace_profile_shows_the_section(self):
        PortClassMapping.objects.create(profile=self.trace_profile, source_label="Undefined", port_class="Port")

        trace = self.client.get(self.trace_profile.get_absolute_url())
        flat = self.client.get(self.flat_profile.get_absolute_url())

        self.assertContains(trace, "PortClass Mappings")
        self.assertContains(trace, "Undefined")
        self.assertNotContains(flat, "PortClass Mappings")

    def test_add_edit_and_delete_through_the_profile_routes(self):
        add_url = reverse(
            "plugins:netbox_data_import:portclassmapping_add", kwargs={"profile_pk": self.trace_profile.pk}
        )

        refused = self.client.post(add_url, {"source_label": "Port", "port_class": "NIC"})
        self.assertEqual(refused.status_code, 200)
        self.assertContains(refused, "is a fixed PortClass, so it takes no mapping.")

        added = self.client.post(add_url, {"source_label": LABEL, "port_class": "NIC"})
        self.assertEqual(added.status_code, 302)
        mapping = PortClassMapping.objects.get(profile=self.trace_profile, source_label=LABEL)

        edit_url = reverse("plugins:netbox_data_import:portclassmapping_edit", kwargs={"pk": mapping.pk})
        self.assertEqual(
            self.client.post(edit_url, {"source_label": LABEL, "port_class": "Switch Port"}).status_code, 302
        )
        mapping.refresh_from_db()
        self.assertEqual(mapping.port_class, "Switch Port")

        delete_url = reverse("plugins:netbox_data_import:portclassmapping_delete", kwargs={"pk": mapping.pk})
        self.assertEqual(self.client.post(delete_url, {"confirm": True}).status_code, 302)
        self.assertFalse(PortClassMapping.objects.exists())
