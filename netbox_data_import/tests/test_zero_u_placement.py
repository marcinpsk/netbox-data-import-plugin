# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""A zero-U device type holds no rack position and no face.

The import drops both whatever the source row says, so a preview that reports them as
differences offers a sync that cannot happen and hides the rows that write nothing.
"""

import io
import re

from django.urls import reverse

from netbox_data_import.tests.test_views import BaseViewTestCase, _make_profile


def _workbook(*, serial, position="1", side="Rear"):
    """Return a one-rack, one-PDU workbook whose PDU row carries a position and a face."""
    import openpyxl

    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Data"
    sheet.append(["Id", "Rack", "Name", "Class", "Side", "UPosition", "Make", "Model", "UHeight", "Serial Number"])
    sheet.append(["zu-1", "Rack-ZU", "Rack-ZU", "Cabinet", None, None, None, None, "42", None])
    sheet.append(["zu-2", "Rack-ZU", "ZU-PDU-A", "Server", side, position, "Eaton", "EMAB33", "1", serial])
    buffer = io.BytesIO()
    book.save(buffer)
    buffer.seek(0)
    return buffer


class ZeroUPlacementMixin:
    """Preview a zero-U device that NetBox already holds without a position or a face."""

    def _run(self, *, serial="SN-FILE"):
        """Return the previewed device row for a zero-U PDU."""
        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Rack, Site

        from netbox_data_import.engine import parse_file, run_import

        site = Site.objects.get_or_create(name="ZeroUSite", slug="zero-u-site")[0]
        profile = _make_profile("ZeroUProfile")
        manufacturer = Manufacturer.objects.create(name="Eaton", slug="eaton")
        device_type = DeviceType.objects.create(
            manufacturer=manufacturer, model="EMAB33", slug="eaton-emab33", u_height=0
        )
        role = DeviceRole.objects.create(name="Server", slug="server")
        rack = Rack.objects.create(name="Rack-ZU", site=site, u_height=42)
        Device.objects.create(
            name="ZU-PDU-A",
            site=site,
            rack=rack,
            device_type=device_type,
            role=role,
            serial="SN-NETBOX",
            status="active",
        )
        rows = parse_file(_workbook(serial=serial), profile)
        result = run_import(rows, profile, {"site": site}, dry_run=True)
        device_rows = [row for row in result.rows if row.object_type == "device"]
        self.assertEqual(len(device_rows), 1, [row.detail for row in result.rows])
        return profile, site, rows, result, device_rows[0]


class ZeroUPlacementIsNotADifferenceTest(ZeroUPlacementMixin, BaseViewTestCase):
    """The position and the face a zero-U row carries never reach NetBox."""

    def test_the_position_is_reported_as_not_written(self):
        """Offering it as a difference invites a sync that NetBox rejects."""
        _profile, _site, _rows, _result, row = self._run()
        self.assertIn("u_position", row.extra_data.get("field_diff", {}))
        self.assertIn("u_position", row.extra_data.get("field_informational", {}))

    def test_the_face_is_reported_as_not_written(self):
        """Same rule as the position: a zero-U type takes neither."""
        _profile, _site, _rows, _result, row = self._run()
        self.assertIn("face", row.extra_data.get("field_informational", {}))

    def test_a_field_the_row_does_write_stays_a_difference(self):
        """The move must not swallow the fields the import really assigns."""
        _profile, _site, _rows, _result, row = self._run()
        self.assertIn("serial", row.extra_data.get("field_diff", {}))
        self.assertNotIn("serial", row.extra_data.get("field_informational", {}))

    def test_a_row_left_with_only_placement_has_no_difference(self):
        """`writes_nothing` reads the same set, so a phantom difference keeps a no-op row an update."""
        _profile, _site, _rows, _result, row = self._run(serial="SN-NETBOX")
        diff = row.extra_data.get("field_diff", {})
        self.assertEqual(sorted(set(diff) - set(row.extra_data.get("field_informational", {}))), [])

    def test_the_row_says_the_device_type_takes_no_position(self):
        """The placement button is inert for a reason the operator cannot otherwise see."""
        _profile, _site, _rows, _result, row = self._run()
        self.assertTrue(row.extra_data.get("zero_u"))
        self.assertTrue(row.extra_data.get("placement_sync_writes_nothing"))


class ZeroUPlacementRendersAsNotWrittenTest(ZeroUPlacementMixin, BaseViewTestCase):
    """What the operator reads has to match what the import does."""

    def _detail_row(self):
        """Return the device row's detail markup from a rendered preview."""
        from netbox_data_import.views import _serialize_rows

        profile, site, rows, result, _row = self._run()
        session = self.client.session
        session["import_result"] = result.to_session_dict()
        session["import_rows"] = _serialize_rows(rows)
        session["import_context"] = {
            "profile_id": profile.pk,
            "site_id": site.pk,
            "location_id": None,
            "tenant_id": None,
            "filename": "zero_u.xlsx",
        }
        session.save()
        response = self.client.get(reverse("plugins:netbox_data_import:import_preview"))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        body = html[html.index('<tbody id="previewRowsBody"') : html.index('id="previewNoFilterResults"')]
        chunk = next(
            chunk
            for chunk in re.split(r'<tr id="prow-\d+" data-action=', body)[1:]
            if 'data-object-type="device"' in chunk
        )
        return chunk[chunk.index('<tr id="diff-') :]

    def _field_cell(self, detail, field_name):
        """Return the diff table row for one field."""
        match = re.search(rf'<tr id="diff-field-\d+-{field_name}">(.*?)</tr>', detail, re.DOTALL)
        self.assertIsNotNone(match, f"the detail row must list {field_name}")
        return match.group(1)

    def test_the_position_row_is_marked_not_written(self):
        """Without the mark the diff reads as something the import will apply."""
        self.assertIn("(not written)", self._field_cell(self._detail_row(), "u_position"))

    def test_the_position_row_offers_no_sync_button(self):
        """The quick action would set a position the device type cannot hold."""
        self.assertNotIn("ndi-sync-btn", self._field_cell(self._detail_row(), "u_position"))

    def test_a_written_field_keeps_its_sync_button(self):
        """The guard must not disarm the fields the quick action does apply."""
        self.assertIn("ndi-sync-btn", self._field_cell(self._detail_row(), "serial"))
