# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""A conflict badge has to say which field it is about.

Two source columns that disagree leave the Target Field unset. The row can still read as a
no-op, because the matched device needs no other write, so the count alone tells the operator
nothing about what to fix.
"""

import os
import re

from django.urls import reverse

from netbox_data_import.models import ColumnMapping
from netbox_data_import.tests.test_views import BaseViewTestCase, _make_profile

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "combined_name.xlsx")


class CombinedNamePreviewMixin:
    """Preview the combined-name workbook against a device that already carries its values."""

    def _setup_session(self):
        """Populate the preview session from the fixture, with 'Service Tag' fighting 'Serial Number'."""
        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Rack, Site

        from netbox_data_import.engine import parse_file, run_import
        from netbox_data_import.views import _serialize_rows

        site = Site.objects.create(name="ConflictSite", slug="conflict-site")
        profile = _make_profile("ConflictProfile")
        # The second column feeding `serial` is what makes the row conflict.
        ColumnMapping.objects.create(profile=profile, source_column="Service Tag", target_field="serial")

        manufacturer = Manufacturer.objects.create(name="Dell", slug="dell")
        device_type = DeviceType.objects.create(manufacturer=manufacturer, model="R660", slug="r660", u_height=1)
        role = DeviceRole.objects.create(name="Server", slug="server")
        rack = Rack.objects.create(name="Rack-CN", site=site, u_height=42)
        Device.objects.create(
            name="AT900 - host-900",
            site=site,
            rack=rack,
            position=37,
            face="front",
            device_type=device_type,
            role=role,
            asset_tag="AT900",
            status="active",
        )

        with open(FIXTURE_PATH, "rb") as workbook:
            rows = parse_file(workbook, profile)
        result = run_import(rows, profile, {"site": site}, dry_run=True)

        session = self.client.session
        session["import_result"] = result.to_session_dict()
        session["import_rows"] = _serialize_rows(rows)
        session["import_context"] = {
            "profile_id": profile.pk,
            "site_id": site.pk,
            "location_id": None,
            "tenant_id": None,
            "filename": "combined_name.xlsx",
        }
        session.save()
        return profile

    def _device_detail_row(self):
        """Return the detail row markup of the device row the fixture previews."""
        self._setup_session()
        response = self.client.get(reverse("plugins:netbox_data_import:import_preview"))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        # A detail row carries tables of its own, so the first `</tbody>` is not the table's end.
        body = html[html.index('<tbody id="previewRowsBody"') : html.index('id="previewNoFilterResults"')]
        chunks = re.split(r'<tr id="prow-\d+" data-action=', body)[1:]
        device_chunks = [chunk for chunk in chunks if 'data-object-type="device"' in chunk]
        self.assertEqual(len(device_chunks), 1, "the fixture must preview exactly one device row")
        chunk = device_chunks[0]
        detail_start = chunk.index('<tr id="diff-')
        return chunk[detail_start:]


class ConflictBadgeCountsTheFixtureConflictTest(CombinedNamePreviewMixin, BaseViewTestCase):
    """The fixture reproduces the report: one conflict on a row that writes nothing."""

    def test_the_device_row_carries_one_conflict(self):
        """Without this the rest of the file would assert against a row that never conflicts."""
        self._setup_session()
        response = self.client.get(reverse("plugins:netbox_data_import:import_preview"))
        html = response.content.decode()
        self.assertIn("1 conflict", html)


class ConflictDetailNamesTheFieldTest(CombinedNamePreviewMixin, BaseViewTestCase):
    """The row a click opens must answer which field conflicts, and with what."""

    def test_the_detail_row_names_the_conflicting_target_field(self):
        """A count on its own does not say whether the name, the serial or the rack is at stake."""
        detail = self._device_detail_row()
        self.assertIn("serial", detail)

    def test_the_detail_row_names_both_source_columns(self):
        """The operator resolves the conflict by choosing a column, so both have to be visible."""
        detail = self._device_detail_row()
        self.assertIn("Serial Number", detail)
        self.assertIn("Service Tag", detail)

    def test_the_detail_row_shows_the_value_each_source_column_holds(self):
        """The values are what the choice is between."""
        detail = self._device_detail_row()
        self.assertIn("SN900", detail)
        self.assertIn("ST900", detail)

    def test_the_detail_row_says_the_field_stays_unset(self):
        """A conflicted field is dropped, and a no-op row gives no other sign of it."""
        detail = self._device_detail_row()
        self.assertIn("unset", detail)

    def test_the_detail_row_offers_the_conflict_modal(self):
        """Resolving from the detail row saves the trip back to the badge in the action column."""
        detail = self._device_detail_row()
        self.assertIn('data-ndi-modal="#conflictModal"', detail)
