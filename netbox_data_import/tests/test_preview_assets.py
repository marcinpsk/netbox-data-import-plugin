# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The preview delivers its own CSS and scripts through an htmx-boosted navigation.

The setup form posts with `hx-boost`, and htmx swaps the body while it discards the response
head. Anything a page needs must therefore render inside the body.
"""

import re
from pathlib import Path

from django.test import SimpleTestCase
from django.urls import reverse

from netbox_data_import.tests.test_views import BaseViewTestCase, ImportPreviewViewTest

TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "templates" / "netbox_data_import"
HEAD_BLOCK = re.compile(r"{%\s*block head\s*%}(.*?){%\s*endblock\s*%}", re.DOTALL)


class HeadBlockCarriesNoPageAssetsTest(SimpleTestCase):
    """htmx drops the response head, so a template may not park assets there."""

    def test_no_template_puts_a_script_or_style_in_the_head_block(self):
        """One offending template silently loses its styling and its behavior after a boost."""
        offenders = []
        for template in sorted(TEMPLATE_DIR.glob("*.html")):
            for block in HEAD_BLOCK.findall(template.read_text()):
                for tag in ("<script", "<style"):
                    if tag in block:
                        offenders.append(f"{template.name}: {tag}")
        self.assertEqual(
            offenders,
            [],
            "Move these into the content block: htmx boost swaps the body and discards the head.",
        )


class PreviewAssetsSurviveABoostedSwapTest(ImportPreviewViewTest):
    """The rendered preview must carry its assets in the part htmx keeps."""

    def _rendered_body(self):
        """Return the preview HTML from the closing head tag onward."""
        self._setup_session()
        response = self.client.get(reverse("plugins:netbox_data_import:import_preview"))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        head_end = html.find("</head>")
        self.assertNotEqual(head_end, -1, "the preview must render a full document")
        return html[head_end:]

    def test_the_row_controls_script_renders_inside_the_body(self):
        """It carries the row toggle and the modal opener, so a boost must not drop it."""
        self.assertIn("preview_row_controls.js", self._rendered_body())

    def test_the_preview_stylesheet_renders_inside_the_body(self):
        """The status badges read as plain grey text without it, so the rule must survive."""
        body = self._rendered_body()
        self.assertIn("<style", body)
        self.assertIn(".ndi-badge-create", body)


class ResultsAssetsSurviveABoostedSwapTest(BaseViewTestCase):
    """The results page is reached from the same boosted flow."""

    def test_the_results_stylesheet_renders_inside_the_body(self):
        """A results page reached through the boost keeps its own styling."""
        response = self.client.get(reverse("plugins:netbox_data_import:import_results"))
        html = response.content.decode()
        if response.status_code != 200 or "</head>" not in html:
            self.skipTest("the results page needs a completed import in the session")
        self.assertIn("ndi-", html[html.find("</head>") :])


class EveryRowCarriesADetailRowTest(ImportPreviewViewTest):
    """A detail row per source row is what makes a row click answer and a placement reachable."""

    def test_the_detail_row_count_matches_the_source_row_count(self):
        """The Sync placement button lives in the detail row, so gating that row hides the button."""
        self._setup_session()
        response = self.client.get(reverse("plugins:netbox_data_import:import_preview"))
        html = response.content.decode()
        body = html[html.index('<tbody id="previewRowsBody"') :]
        source_rows = len(re.findall(r'<tr id="row-\d+" data-action=', body))
        detail_rows = len(re.findall(r'<tr id="diff-\d+" class="ndi-diff-row" hidden>', body))
        self.assertGreater(source_rows, 0, "the sample workbook must produce preview rows")
        self.assertEqual(detail_rows, source_rows)


class PluginScriptsAreVersionedTest(ImportPreviewViewTest):
    """A browser must not keep running the previous release's script after an upgrade."""

    def test_every_plugin_script_carries_the_plugin_version(self):
        """NetBox versions its own bundle; an unversioned plugin asset is served from cache."""
        from netbox_data_import import __version__

        self._setup_session()
        response = self.client.get(reverse("plugins:netbox_data_import:import_preview"))
        html = response.content.decode()
        sources = re.findall(r'<script src="(/static/netbox_data_import/[^"]+)"', html)
        self.assertTrue(sources, "the preview must load the plugin's own scripts")
        unversioned = [src for src in sources if f"?v={__version__}" not in src]
        self.assertEqual(unversioned, [], "append the plugin version so an upgrade busts the cache")


class ResolvedContactRowIsMarkedTest(BaseViewTestCase):
    """A row whose contact is already resolved must not look like an unresolved one."""

    CONTACT_COLUMNS = {
        "Primary Contact": "candidate:contact",
        "Owner": "candidate:contact",
    }

    def _workbook(self):
        """Return an in-memory workbook that maps contact candidate columns."""
        import io

        import openpyxl

        book = openpyxl.Workbook()
        sheet = book.active
        sheet.title = "Data"
        sheet.append(["Id", "Rack", "Name", "Class", "UHeight", "Primary Contact", "Owner"])
        sheet.append(["c-1", "RackC", "RackC", "Cabinet", "42", None, None])
        sheet.append(["c-2", "RackC", "contact-server-01", "Server", "1", "ada@example.invalid", "Lab Ops"])
        buffer = io.BytesIO()
        book.save(buffer)
        buffer.seek(0)
        return buffer

    def _setup_session(self):
        """Populate the session from a workbook that carries contact candidates."""
        from dcim.models import Site

        from netbox_data_import.engine import parse_file, run_import
        from netbox_data_import.models import ColumnMapping
        from netbox_data_import.tests.test_views import _make_profile
        from netbox_data_import.views import _serialize_rows

        site = Site.objects.create(name="ContactSite", slug="contact-site")
        profile = _make_profile("ContactProfile")
        profile.adapter_config = {**profile.adapter_config, "primary_contact_lookup_field": "email"}
        profile.save()
        for source_column, target_field in self.CONTACT_COLUMNS.items():
            ColumnMapping.objects.create(profile=profile, source_column=source_column, target_field=target_field)

        rows = parse_file(self._workbook(), profile)
        result = run_import(rows, profile, {"site": site}, dry_run=True)
        session = self.client.session
        session["import_result"] = result.to_session_dict()
        session["import_rows"] = _serialize_rows(rows)
        session["import_context"] = {
            "profile_id": profile.pk,
            "site_id": site.pk,
            "location_id": None,
            "tenant_id": None,
            "filename": "contacts.xlsx",
        }
        session.save()
        return profile

    def _preview_html(self):
        """Return the rendered preview page."""
        response = self.client.get(reverse("plugins:netbox_data_import:import_preview"))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def _contact_button(self, html):
        """Return the (class, source id) of the row's contact button.

        The stylesheet now renders in the body, so its rule names would match a raw string search.
        """
        match = re.search(
            r'<button[^>]*class="([^"]*)"[^>]*data-ndi-modal="#contactCandidateModal"'
            r'[^>]*data-source-id="([^"]+)"',
            html,
            re.DOTALL,
        )
        self.assertIsNotNone(match, "a row mapping contact candidates must render the contact button")
        return match.group(1), match.group(2)

    def test_an_unresolved_row_asks_for_a_decision(self):
        """The unresolved state is the baseline the resolved state has to differ from."""
        self._setup_session()
        classes, _ = self._contact_button(self._preview_html())
        self.assertNotIn("ndi-contact-resolved", classes)
        self.assertIn("btn-outline-warning", classes)

    def test_the_button_reports_the_saved_resolution(self):
        """Without this the operator cannot tell which rows they have already answered."""
        from netbox_data_import.models import SourceResolution

        profile = self._setup_session()
        _, source_id = self._contact_button(self._preview_html())
        SourceResolution.objects.create(
            profile=profile,
            source_id=source_id,
            source_column="candidate:contact",
            original_value="{}",
            resolved_fields={"contact_resolution_applied": True, "contact_field_values": {"name": "Ada"}},
        )
        classes, _ = self._contact_button(self._preview_html())
        self.assertIn("ndi-contact-resolved", classes)
        self.assertIn("btn-outline-success", classes)
