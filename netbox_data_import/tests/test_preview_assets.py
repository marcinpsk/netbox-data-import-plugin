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

from netbox_data_import.tests.test_views import BaseViewTestCase, PreviewSessionMixin

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


class PreviewAssetsSurviveABoostedSwapTest(PreviewSessionMixin, BaseViewTestCase):
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


class ResultsAssetsSurviveABoostedSwapTest(PreviewSessionMixin, BaseViewTestCase):
    """The results page is reached from the same boosted flow."""

    def test_the_results_stylesheet_renders_inside_the_body(self):
        """A results page reached through the boost keeps its own styling."""
        self._setup_session()
        response = self.client.get(reverse("plugins:netbox_data_import:import_results"))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        head_end = html.find("</head>")
        self.assertNotEqual(head_end, -1, "the results page must render a full document")
        body = html[head_end:]
        self.assertIn("<style", body)
        # The badges carry these names as plain attributes too, so the rule needs its leading dot.
        self.assertIn(".ndi-badge-create", body)


class EveryRowCarriesADetailRowTest(PreviewSessionMixin, BaseViewTestCase):
    """A detail row per source row is what makes a row click answer and a placement reachable."""

    def test_the_detail_row_count_matches_the_source_row_count(self):
        """The Sync placement button lives in the detail row, so gating that row hides the button."""
        self._setup_session()
        response = self.client.get(reverse("plugins:netbox_data_import:import_preview"))
        html = response.content.decode()
        body = html[html.index('<tbody id="previewRowsBody"') :]
        source_rows = len(re.findall(r'<tr id="prow-\d+" data-action=', body))
        detail_rows = len(re.findall(r'<tr id="diff-\d+" class="ndi-diff-row" hidden>', body))
        self.assertGreater(source_rows, 0, "the sample workbook must produce preview rows")
        self.assertEqual(detail_rows, source_rows)


class DetailRowIdsAreUniqueTest(PreviewSessionMixin, BaseViewTestCase):
    """Row numbers repeat across object types, so the detail row cannot be keyed on them."""

    def test_no_detail_row_id_is_rendered_twice(self):
        """A shared id makes setDiffExpanded flip a second row's toggle state."""
        self._setup_session()
        response = self.client.get(reverse("plugins:netbox_data_import:import_preview"))
        html = response.content.decode()
        ids = re.findall(r'<tr id="(diff-[^"]+)" class="ndi-diff-row"', html)
        self.assertTrue(ids, "the preview must render detail rows")
        self.assertEqual(sorted(ids), sorted(set(ids)), "detail row ids must be unique per render")

    def test_every_diff_toggle_points_at_an_existing_detail_row(self):
        """A toggle whose target is missing silently does nothing."""
        self._setup_session()
        response = self.client.get(reverse("plugins:netbox_data_import:import_preview"))
        html = response.content.decode()
        detail_ids = set(re.findall(r'<tr id="(diff-[^"]+)" class="ndi-diff-row"', html))
        targets = set(re.findall(r'data-diff-target="(diff-[^"]+)"', html))
        self.assertLessEqual(targets, detail_ids)

    def test_no_id_inside_the_preview_table_is_rendered_twice(self):
        """One net for the whole class: any id keyed on a repeating row number fails here."""
        self._setup_session()
        response = self.client.get(reverse("plugins:netbox_data_import:import_preview"))
        body = response.content.decode()
        body = body[body.index('<tbody id="previewRowsBody"') : body.index("</tbody>")]
        ids = re.findall(r'\sid="([^"]+)"', body)
        self.assertTrue(ids, "the preview table must render ids")
        duplicates = sorted({value for value in ids if ids.count(value) > 1})
        self.assertEqual(duplicates, [])

    def test_every_source_row_id_is_rendered_once(self):
        """A shared row id sent the conflict jump to the wrong row."""
        self._setup_session()
        response = self.client.get(reverse("plugins:netbox_data_import:import_preview"))
        html = response.content.decode()
        ids = re.findall(r'<tr id="(prow-[^"]+)" data-action=', html)
        self.assertTrue(ids, "the preview must render source rows")
        self.assertEqual(sorted(ids), sorted(set(ids)))

    def test_every_source_row_carries_a_native_detail_toggle(self):
        """The row click is a pointer shortcut, so the keyboard needs a real button per row."""
        self._setup_session()
        response = self.client.get(reverse("plugins:netbox_data_import:import_preview"))
        body = response.content.decode()
        body = body[body.index('<tbody id="previewRowsBody"') : body.index("</tbody>")]
        # Cut each source row at its detail row so a toggle inside the detail row does not count.
        chunks = [chunk.split('<tr id="diff-')[0] for chunk in re.split(r'<tr id="prow-\d+" data-action=', body)[1:]]
        self.assertTrue(chunks, "the preview must render source rows")
        without = [index for index, chunk in enumerate(chunks) if "ndi-diff-toggle" not in chunk]
        self.assertEqual(without, [], "every source row needs its own toggle button")

    def test_no_source_row_claims_a_button_role(self):
        """A button role makes the links and forms inside the row presentational."""
        self._setup_session()
        response = self.client.get(reverse("plugins:netbox_data_import:import_preview"))
        html = response.content.decode()
        rows = re.findall(r'<tr id="prow-\d+" data-action="[^"]*"\s+([^>]*)>', html)
        self.assertTrue(rows, "the preview must render source rows")
        self.assertTrue(all('role="button"' not in attrs for attrs in rows), rows[:2])
        self.assertTrue(all("tabindex=" not in attrs for attrs in rows), rows[:2])


class PluginScriptsAreVersionedTest(PreviewSessionMixin, BaseViewTestCase):
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


class ConflictJumpTargetsOneRowTest(SimpleTestCase):
    """The conflict jump has to name the row it means, not a row number several rows share."""

    TEMPLATE = TEMPLATE_DIR / "import_preview.html"

    def test_the_jump_control_carries_the_object_type_with_the_row_number(self):
        """`getElementById('row-N')` returned the first match, which was often the rack row."""
        source = self.TEMPLATE.read_text()
        jump = re.search(r"<button[^>]*ndi-jump-to-row.*?</button>", source, re.DOTALL)
        self.assertIsNotNone(jump, "the preview must render the conflict jump control")
        self.assertIn("data-target-row=", jump.group(0))
        self.assertIn("data-target-type=", jump.group(0))

    def test_the_jump_handler_matches_on_both_attributes(self):
        """A handler that still resolves an id would reintroduce the wrong-row jump."""
        source = self.TEMPLATE.read_text()
        self.assertNotIn("getElementById('row-' + ", source)
        self.assertIn('tr[data-object-type="', source)


class FieldRowIdsFollowTheDetailRowTest(SimpleTestCase):
    """Both field id families must count with the detail row that holds them.

    They are decorative anchors nothing reads, so the invariant is asserted on the template.
    The rendered-page duplicate check lives in `DetailRowIdsAreUniqueTest`.
    """

    def test_both_field_id_families_use_the_detail_row_index(self):
        """`ignored-field-*` used the source row number, which is a different number entirely."""
        source = (TEMPLATE_DIR / "import_preview.html").read_text()
        for family in ("diff-field", "ignored-field"):
            match = re.search(rf'id="{family}-{{{{ ([^}}]+) }}}}-', source)
            self.assertIsNotNone(match, f"the preview must render the {family} rows")
            self.assertEqual(match.group(1).strip(), "forloop.parentloop.counter", family)


class DetailRowSummaryReadsTheActionTest(PreviewSessionMixin, BaseViewTestCase):
    """`netbox_device_id` is a device-only key, so it cannot decide what any other row says."""

    CREATE_MESSAGE = "This row creates a new object"
    MATCH_MESSAGE = "This row matches NetBox"
    NO_WRITE_MESSAGE = "This row writes nothing to NetBox"

    def _detail_for(self, match):
        """Return (action, detail-row HTML) for the first preview row `match` accepts."""
        response = self.client.get(reverse("plugins:netbox_data_import:import_preview"))
        rows = list(response.context["result"].rows)
        index = next(i for i, row in enumerate(rows, start=1) if match(row))
        html = response.content.decode()
        start = html.index(f'<tr id="diff-{index}" class="ndi-diff-row"')
        return rows[index - 1].action, html[start : html.index("</tr>", start)]

    def _rack_detail(self):
        """Return (action, detail-row HTML) for the workbook's rack row in the current preview."""
        return self._detail_for(lambda row: row.object_type == "rack")

    def _preview_the_rack_that_netbox_already_holds(self, u_height=24):
        """Create the workbook's rack in NetBox, so its row turns from a creation into an update.

        The default height differs from the workbook's, so the row still writes something.
        """
        from dcim.models import Rack, Site

        self._setup_session()
        response = self.client.get(reverse("plugins:netbox_data_import:import_preview"))
        rack_row = next(row for row in response.context["result"].rows if row.object_type == "rack")
        site = Site.objects.get(pk=self.client.session["import_context"]["site_id"])
        Rack.objects.create(name=rack_row.name, site=site, u_height=u_height)

    def test_a_rack_row_that_creates_says_so(self):
        """The create message is the baseline the existing-rack case has to differ from."""
        self._setup_session()
        action, detail = self._rack_detail()
        self.assertEqual(action, "create")
        self.assertIn(self.CREATE_MESSAGE, detail)

    def test_an_existing_rack_row_is_not_called_a_creation(self):
        """A dry-run rack row carries `netbox_rack_id`, so the device key reported it as a creation."""
        self._preview_the_rack_that_netbox_already_holds()

        action, detail = self._rack_detail()
        self.assertNotEqual(action, "create", "the rack now exists, so the row cannot be a creation")
        self.assertNotIn(self.CREATE_MESSAGE, detail)

    def test_an_error_row_is_not_called_a_match(self):
        """An error row is never compared against NetBox, so the summary cannot claim a match."""

        def blank_the_server_name(rows):
            # The Cabinet row creates the rack, so only a device row can miss a device name.
            target = next(row for row in rows if row.get("device_class") == "Server")
            target["device_name"] = ""
            target["asset_tag"] = ""

        self._setup_session(mutate_rows=blank_the_server_name)
        action, detail = self._detail_for(lambda row: row.action == "error")
        self.assertEqual(action, "error")
        self.assertNotIn(self.MATCH_MESSAGE, detail)
        self.assertNotIn(self.CREATE_MESSAGE, detail)
        self.assertIn(self.NO_WRITE_MESSAGE, detail)

    def test_an_update_row_with_no_field_diff_still_reports_a_match(self):
        """Scoping the match message to `update` must not remove it from the row it belongs to."""
        self._preview_the_rack_that_netbox_already_holds()

        action, detail = self._rack_detail()
        self.assertEqual(action, "update")
        self.assertIn(self.MATCH_MESSAGE, detail)

    def test_a_rack_row_that_writes_nothing_is_not_called_an_update(self):
        """The rack already carries every value this row sets, so calling it an update misleads."""
        self._preview_the_rack_that_netbox_already_holds(u_height=42)

        action, _detail = self._rack_detail()
        self.assertEqual(action, "skip")

    def _rack_main_row(self):
        """Return the HTML of the rack row itself, not of its detail row."""
        response = self.client.get(reverse("plugins:netbox_data_import:import_preview"))
        rows = list(response.context["result"].rows)
        index = next(i for i, row in enumerate(rows, start=1) if row.object_type == "rack")
        html = response.content.decode()
        end = html.index(f'<tr id="diff-{index}" class="ndi-diff-row"')
        return html[html.rindex("<tr", 0, end) : end]

    def test_a_row_that_writes_nothing_reads_as_a_no_op_not_a_skip(self):
        """`skip` also covers a row updates were turned off for, which is a different answer."""
        self._preview_the_rack_that_netbox_already_holds(u_height=42)

        main_row = self._rack_main_row()

        self.assertIn(">No-op<", main_row)
        self.assertNotIn(">Skip<", main_row)


class DuplicateSerialActionTest(PreviewSessionMixin, BaseViewTestCase):
    """A refused row needs an action on it, or the operator can do nothing about the collision."""

    def _preview_html_with_a_shared_serial(self):
        """Give two workbook rows one serial and render the preview."""
        self._setup_session()
        session = self.client.session
        rows = session["import_rows"]
        # A row that already carries a serial is a device row, not the Cabinet row.
        devices = [row for row in rows if row.get("serial")][:2]
        self.assertEqual(len(devices), 2, "the sample workbook must carry two device rows")
        for row in devices:
            row["serial"] = "SHARED-PREVIEW-SERIAL"
        session["import_rows"] = rows
        session.save()
        return self.client.get(reverse("plugins:netbox_data_import:import_preview")).content.decode()

    def test_a_duplicate_serial_row_offers_the_ignore_action(self):
        """The operator gives the serial up on one row so the other keeps it."""
        html = self._preview_html_with_a_shared_serial()

        self.assertIn("Duplicate serial", html)
        self.assertIn("ignore-duplicate-serial/", html)

    def test_a_duplicate_serial_row_names_the_other_row(self):
        """Finding the other row is what makes the choice possible."""
        html = self._preview_html_with_a_shared_serial()

        self.assertIn("also on row", html)


class MatchedDeviceBadgeTest(PreviewSessionMixin, BaseViewTestCase):
    """A row that matched a NetBox device says so, whatever its field diff turns out to be."""

    def _preview_html(self):
        """Return the rendered preview for the current session."""
        return self.client.get(reverse("plugins:netbox_data_import:import_preview")).content.decode()

    def _match_a_workbook_device(self):
        """Create the NetBox device one workbook row names, so that row matches it."""
        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site

        self._setup_session()
        rows = self.client.session["import_rows"]
        row = next(r for r in rows if r.get("device_name") and r.get("u_position"))
        site = Site.objects.get(pk=self.client.session["import_context"]["site_id"])
        manufacturer = Manufacturer.objects.create(name="MatchMfg", slug="match-mfg")
        device_type = DeviceType.objects.create(manufacturer=manufacturer, model="MatchModel", slug="match-model")
        role = DeviceRole.objects.create(name="MatchRole", slug="match-role")
        device = Device.objects.create(name=row["device_name"], site=site, device_type=device_type, role=role)
        return row, device

    def test_a_row_that_matched_a_device_carries_the_badge(self):
        """The row reports an update the field diff cannot explain, so it names what it matched."""
        _row, device = self._match_a_workbook_device()

        html = self._preview_html()

        self.assertIn("ndi-matched-badge", html)
        self.assertIn(f"/dcim/devices/{device.pk}/", html)

    def test_a_row_that_matched_nothing_carries_no_badge(self):
        """A device this import creates has nothing to point at."""
        self._setup_session()

        html = self._preview_html()

        self.assertNotIn("ndi-matched-badge", html)


class PlacementBadgeTest(PreviewSessionMixin, BaseViewTestCase):
    """Sync placement sits in the collapsed detail row, so the main row has to advertise it."""

    def _preview_html(self):
        """Return the rendered preview for the current session."""
        return self.client.get(reverse("plugins:netbox_data_import:import_preview")).content.decode()

    def _place_the_workbook_device_in_netbox(self, *, same_rack):
        """Match a workbook device to NetBox, either already placed as the row asks or not.

        `same_rack=True` leaves the row nothing to write, which is the case the badge must skip.
        """
        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Rack, Site

        self._setup_session()
        rows = self.client.session["import_rows"]
        # The Cabinet row also carries a rack name, so the position is what marks a real device.
        row = next(r for r in rows if r.get("rack_name") and r.get("u_position"))
        site = Site.objects.get(pk=self.client.session["import_context"]["site_id"])
        rack = Rack.objects.create(name=row["rack_name"], site=site, u_height=42)
        manufacturer = Manufacturer.objects.create(name="BadgeMfg", slug="badge-mfg")
        device_type = DeviceType.objects.create(manufacturer=manufacturer, model="BadgeModel", slug="badge-model")
        role = DeviceRole.objects.create(name="BadgeRole", slug="badge-role")
        Device.objects.create(
            name=row["device_name"],
            site=site,
            device_type=device_type,
            role=role,
            rack=rack if same_rack else None,
            position=int(row["u_position"]) if same_rack else None,
            face=(row.get("face") or "").lower() if same_rack else "",
        )
        return row

    def test_a_row_that_can_apply_a_placement_carries_the_badge(self):
        """The operator has to open the row to see the green button, so the badge answers first."""
        row = self._place_the_workbook_device_in_netbox(same_rack=False)

        html = self._preview_html()

        self.assertIn("ndi-placement-badge", html)
        self.assertIn(f"rack={row['rack_name']}", html)

    def test_a_row_whose_placement_writes_nothing_carries_no_badge(self):
        """The badge tracks the green button, so an inert placement must not advertise one."""
        self._place_the_workbook_device_in_netbox(same_rack=True)

        self.assertNotIn("ndi-placement-badge", self._preview_html())
