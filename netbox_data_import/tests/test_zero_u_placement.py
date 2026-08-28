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

    def _run(self, *, serial="SN-FILE", u_height=0):
        """Return the previewed device row for a PDU of the given device-type height."""
        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Rack, Site

        from netbox_data_import.engine import parse_file, run_import

        site = Site.objects.get_or_create(name="ZeroUSite", slug="zero-u-site")[0]
        profile = _make_profile("ZeroUProfile")
        manufacturer = Manufacturer.objects.create(name="Eaton", slug="eaton")
        device_type = DeviceType.objects.create(
            manufacturer=manufacturer, model="EMAB33", slug="eaton-emab33", u_height=u_height
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
        self.assertIn("u_position", row.extra_data.get("field_informational", {}))
        self.assertNotIn("u_position", row.extra_data.get("field_diff", {}))

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
        self.assertEqual(row.extra_data.get("field_diff", {}), {})
        self.assertIn("u_position", row.extra_data.get("field_informational", {}))

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

    def _field_cell(self, detail, field_name, prefix="diff"):
        """Return the detail table row for one field."""
        match = re.search(rf'<tr id="{prefix}-field-\d+-{field_name}">(.*?)</tr>', detail, re.DOTALL)
        self.assertIsNotNone(match, f"the detail row must list {field_name}")
        return match.group(1)

    def test_the_position_row_is_marked_not_written(self):
        """Without the mark the diff reads as something the import will apply."""
        self.assertIn("(not written)", self._field_cell(self._detail_row(), "u_position", prefix="informational"))

    def test_the_position_row_offers_no_sync_button(self):
        """The quick action would set a position the device type cannot hold."""
        self.assertNotIn("ndi-sync-btn", self._field_cell(self._detail_row(), "u_position", prefix="informational"))

    def test_a_written_field_keeps_its_sync_button(self):
        """The guard must not disarm the fields the quick action does apply."""
        self.assertIn("ndi-sync-btn", self._field_cell(self._detail_row(), "serial"))


class ZeroUImportAgreesWithItsPreviewTest(ZeroUPlacementMixin, BaseViewTestCase):
    """The execute guard compares the writer's action to the previewed one, so both must agree."""

    def _imported_row(self, *, serial="SN-NETBOX"):
        """Import the workbook once, then return its preview and its second import."""
        from netbox_data_import.engine import parse_file, run_import
        from netbox_data_import.views import _import_intents

        profile, site, _rows, _result, _row = self._run(serial=serial)
        run_import(parse_file(_workbook(serial=serial), profile), profile, {"site": site}, dry_run=False)

        rows = parse_file(_workbook(serial=serial), profile)
        preview = run_import(rows, profile, {"site": site}, dry_run=True)
        written = run_import(
            parse_file(_workbook(serial=serial), profile),
            profile,
            {"site": site},
            dry_run=False,
            expected_intents=_import_intents(preview),
        )
        return (
            next(r for r in preview.rows if r.object_type == "device"),
            next(r for r in written.rows if r.object_type == "device"),
        )

    def test_an_imported_zero_u_row_previews_as_a_no_op(self):
        """The position and the face it carries were never written, so nothing is left to write."""
        preview_row, _written_row = self._imported_row()
        self.assertEqual(preview_row.action, "skip", preview_row.detail)
        self.assertTrue(preview_row.extra_data.get("writes_nothing"))

    def test_the_writer_reaches_the_same_action_as_the_preview(self):
        """A writer that still counts the dropped fields fails the row on the intent guard."""
        _preview_row, written_row = self._imported_row()
        self.assertNotEqual(written_row.action, "error", written_row.detail)
        self.assertEqual(written_row.action, "skip", written_row.detail)


class OnlyAnInformationalFieldLosesItsSyncButtonTest(ZeroUPlacementMixin, BaseViewTestCase):
    """The guard reads one dict, and a row that has none of those fields must keep its buttons."""

    def _serial_cell(self, **run_kwargs):
        """Return the serial line of the rendered diff table for one run."""
        from netbox_data_import.views import _serialize_rows

        profile, site, rows, result, row = self._run(**run_kwargs)
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
        html = response.content.decode()
        match = re.search(r'<tr id="diff-field-\d+-serial">(.*?)</tr>', html, re.DOTALL)
        self.assertIsNotNone(match, "the preview must list the serial difference")
        return row, match.group(1)

    def test_a_row_with_no_informational_field_keeps_its_sync_button(self):
        """A full-height type leaves the dict out of the row, which must not disarm the action."""
        row, cell = self._serial_cell(u_height=1)
        self.assertNotIn("field_informational", row.extra_data)
        self.assertIn("ndi-sync-btn", cell)


class ZeroUTypeChangeOnAPlacedDeviceTest(BaseViewTestCase):
    """A row that moves a placed device to a zero-U type still writes neither position nor face."""

    def _run(self):
        """Preview a source row that retypes a racked 1U device as zero-U at another position."""
        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Rack, Site

        from netbox_data_import.engine import parse_file, run_import

        site = Site.objects.create(name="ZeroUMoveSite", slug="zero-u-move-site")
        profile = _make_profile("ZeroUMoveProfile")
        eaton = Manufacturer.objects.create(name="Eaton", slug="eaton")
        DeviceType.objects.create(manufacturer=eaton, model="EMAB33", slug="eaton-emab33", u_height=0)
        legacy = Manufacturer.objects.create(name="Legacy", slug="legacy")
        stored_type = DeviceType.objects.create(
            manufacturer=legacy, model="RackServer", slug="legacy-rackserver", u_height=1
        )
        role = DeviceRole.objects.create(name="Server", slug="server")
        rack = Rack.objects.create(name="Rack-ZU", site=site, u_height=42)
        Device.objects.create(
            name="ZU-PDU-A",
            site=site,
            rack=rack,
            position=5,
            face="front",
            device_type=stored_type,
            role=role,
            serial="SN-NETBOX",
            status="active",
        )
        rows = parse_file(_workbook(serial="SN-NETBOX", position="7", side="Rear"), profile)
        result = run_import(rows, profile, {"site": site}, dry_run=True)
        return next(row for row in result.rows if row.object_type == "device")

    def test_the_position_the_row_carries_is_not_a_writable_difference(self):
        """The stored position does not make the source position reachable."""
        row = self._run()
        self.assertIn("u_position", row.extra_data.get("field_informational", {}))

    def test_the_face_the_row_carries_is_not_a_writable_difference(self):
        """Same rule as the position, and the row supplies both."""
        row = self._run()
        self.assertIn("face", row.extra_data.get("field_informational", {}))


class ANoOpRowDoesNotClaimFieldsDifferTest(ZeroUPlacementMixin, BaseViewTestCase):
    """A badge that counts a field the import never writes reports a change that never happens."""

    def _rendered_preview(self, **run_kwargs):
        """Render the preview a browser shows for one run."""
        from netbox_data_import.views import _serialize_rows

        profile, site, rows, result, row = self._run(**run_kwargs)
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
        return row, response.content.decode()

    def test_a_placement_only_row_reports_no_writable_difference(self):
        """The row writes nothing, so counting its dropped placement as a difference is a lie."""
        _row, html = self._rendered_preview(serial="SN-NETBOX")
        self.assertNotIn("field(s) differ", html)

    def test_a_placement_only_row_still_opens_its_detail(self):
        """Dropping the badge must not leave the row without a control that opens the panel."""
        _row, html = self._rendered_preview(serial="SN-NETBOX")
        self.assertIn("field(s) not written", html)

    def test_a_placement_only_row_still_lists_the_dropped_fields(self):
        """The operator has to see which values the import discards."""
        _row, html = self._rendered_preview(serial="SN-NETBOX")
        self.assertRegex(html, r'<tr id="informational-field-\d+-u_position">')

    def test_a_row_that_writes_a_field_still_counts_it(self):
        """The count has to keep every difference the import really applies."""
        _row, html = self._rendered_preview()
        self.assertIn("field(s) differ", html)
        self.assertRegex(html, r'<tr id="diff-field-\d+-serial">')
