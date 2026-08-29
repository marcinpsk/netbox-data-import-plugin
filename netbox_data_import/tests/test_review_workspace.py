# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Review Workspace presentation and command integration tests."""

from django.contrib.auth import get_user_model
from django.test import TestCase

from netbox_data_import.models import ClassRoleMapping, DeviceExistingMatch, ImportProfile
from netbox_data_import.plan import Diagnostic, Disposition, ImportPlan, PlannedChange, Severity, SynchronizationUnit
from netbox_data_import.review_workspace import AutoMatchSummary, ReviewWorkspace
from netbox_data_import.tests.helpers import make_dcim_objects, user_with_object_permission


def _unit(identity, disposition, *, row=None, operation=None, rack_name=""):
    """Return one compact presentation unit."""
    row = row or {}
    changes = (
        (
            PlannedChange(
                identity=f"{identity}:{operation}",
                target_module=identity.partition(":")[0],
                operation=operation,
                payload={},
            ),
        )
        if operation is not None
        else ()
    )
    return SynchronizationUnit(
        identity=identity,
        disposition=disposition,
        changes=changes,
        display={
            "row_number": row.get("_row_number"),
            "source_id": row.get("source_id", ""),
            "name": row.get("device_name", row.get("rack_name", "")),
            "rack_name": rack_name or row.get("rack_name", ""),
            "source_row": row,
            "extra_data": {"u_position": row.get("u_position")},
        },
    )


def _workspace(*units):
    """Return a Review Workspace over the supplied units."""
    return ReviewWorkspace(
        ImportPlan(
            units=units,
            source_fingerprint="0" * 64,
            profile_fingerprint="1" * 64,
            actor="1",
            planning_context={"site_id": 1, "location_id": None, "tenant_id": None},
        )
    )


class ReviewWorkspacePresentationTest(TestCase):
    """The workspace maps plan vocabulary to stable preview vocabulary."""

    def test_counts_cover_every_disposition_and_ignore_unknown_operations(self):
        """Counts distinguish no-op, exclusion, error, create, update, and non-preview operations."""
        diagnostic = Diagnostic(code="device.example", severity=Severity.ERROR)
        invalid = SynchronizationUnit(
            identity="device:invalid",
            disposition=Disposition.INVALID,
            diagnostics=(diagnostic,),
        )
        workspace = _workspace(
            _unit("device:create", Disposition.ACTIONABLE, operation="create"),
            _unit("device:update", Disposition.ACTIONABLE),
            _unit("device:skip", Disposition.NO_OP),
            _unit("device:ignore", Disposition.EXCLUDED),
            invalid,
            _unit("device:delete", Disposition.ACTIONABLE, operation="delete"),
        )

        self.assertEqual(
            dict(workspace.counts),
            {"devices_created": 1, "devices_updated": 1, "skipped": 1, "ignored": 1, "errors": 1},
        )
        self.assertTrue(workspace.has_errors)
        self.assertEqual(workspace.units[3].action, "ignore")
        self.assertEqual(workspace.units[4].detail, "device.example")

    def test_rack_groups_sort_devices_and_source_rows_are_deduplicated(self):
        """One source row can yield dependencies, but it appears once and devices sort by position."""
        shared = {"_row_number": 2, "source_id": "D-1", "device_name": "device-a", "rack_name": "rack-a"}
        workspace = _workspace(
            _unit("rack:empty", Disposition.NO_OP, row={"_row_number": 1, "rack_name": ""}),
            _unit(
                "rack:rack-a",
                Disposition.ACTIONABLE,
                row={"_row_number": 3, "rack_name": "rack-a"},
                operation="create",
            ),
            _unit("device:later", Disposition.NO_OP, row={**shared, "u_position": None}),
            _unit("device:first", Disposition.NO_OP, row={**shared, "u_position": 2}),
            _unit("device:unracked", Disposition.NO_OP, row={"_row_number": 4, "device_name": "loose"}),
        )

        self.assertEqual(
            [unit.identity for unit in workspace.rack_groups["rack-a"]["devices"]], ["device:first", "device:later"]
        )
        self.assertIn("(No rack)", workspace.rack_groups)
        self.assertEqual([row["_row_number"] for row in workspace.source_rows], [1, 2, 3, 4])

        replaced = workspace.replace_unit(workspace.units[0], detail="replacement")
        copied = workspace.with_units((replaced,))
        self.assertEqual(copied.units[0].detail, "replacement")
        self.assertIs(copied.plan, workspace.plan)

    def test_auto_match_summary_reports_each_command_outcome(self):
        """The summary tells the operator about every materially different result."""
        message = AutoMatchSummary(
            matched=1,
            probable=2,
            ambiguous=3,
            placement_conflicts=4,
            already=5,
            skipped=6,
        ).message()

        for text in ("auto-matched", "probable", "ambiguous", "placement conflict", "already", "skipped"):
            self.assertIn(text, message)
        self.assertEqual(AutoMatchSummary().message(), "Auto-match: nothing found.")


class ReviewWorkspaceAutoMatchTest(TestCase):
    """Auto-match uses real Device identities, target scope, and permission-scoped writes."""

    def setUp(self):
        """Create one target and an eligible profile."""
        self.site, self.manufacturer, self.device_type, self.role = make_dcim_objects("Workspace")
        self.profile = ImportProfile.objects.create(name="Workspace Profile", adapter_config={})
        ClassRoleMapping.objects.create(profile=self.profile, source_class="Server", role_slug=self.role.slug)
        self.actor = get_user_model().objects.create_superuser(
            username="workspace-operator",
            email="workspace@example.invalid",
            password="testpass",
        )
        self.target = {"site": self.site, "location": None, "tenant": None}

    def _workspace(self, *rows):
        """Return a workspace carrying the given source rows."""
        return _workspace(
            *(
                _unit(f"device:{index}", Disposition.NO_OP, row={"_row_number": index, **row})
                for index, row in enumerate(rows, start=1)
            )
        )

    def _row(self, source_id, name, **values):
        """Return one eligible device source row."""
        row = {
            "source_id": source_id,
            "device_name": name,
            "device_class": "Server",
            "serial": "",
            "asset_tag": "",
            "rack_name": "",
            "u_position": "",
            "face": "",
        }
        row.update(values)
        return row

    def test_exact_serial_match_is_saved_and_a_second_run_is_already_matched(self):
        """A safe strong identity saves one binding and becomes idempotent."""
        from dcim.models import Device

        device = Device.objects.create(
            name="serial-target",
            serial="WORKSPACE-SERIAL",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
        )
        workspace = self._workspace(self._row("SOURCE-1", "source-name", serial=device.serial))

        first = workspace.auto_match_devices(self.profile, self.actor, self.target)
        second = workspace.auto_match_devices(self.profile, self.actor, self.target)

        self.assertEqual(first.matched, 1)
        self.assertEqual(second.already, 1)
        self.assertTrue(DeviceExistingMatch.objects.filter(profile=self.profile, netbox_device_id=device.pk).exists())

    def test_ambiguous_duplicate_and_cross_target_identities_are_never_saved(self):
        """Duplicate source IDs, conflicting strong IDs, and another site all report ambiguity."""
        from dcim.models import Device, Site

        other_site = Site.objects.create(name="Workspace Other", slug="workspace-other")
        first = Device.objects.create(
            name="first-target",
            serial="SERIAL-FIRST",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
        )
        second = Device.objects.create(
            name="second-target",
            asset_tag="ASSET-SECOND",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
        )
        Device.objects.create(
            name="other-target",
            serial="SERIAL-OTHER",
            site=other_site,
            device_type=self.device_type,
            role=self.role,
        )
        workspace = self._workspace(
            self._row("DUPLICATE", "one"),
            self._row("DUPLICATE", "two"),
            self._row("CONFLICT", "conflict", serial=first.serial, asset_tag=second.asset_tag),
            self._row("OTHER", "other", serial="SERIAL-OTHER"),
        )

        summary = workspace.auto_match_devices(self.profile, self.actor, self.target)

        self.assertEqual(summary.ambiguous, 4)
        self.assertFalse(DeviceExistingMatch.objects.filter(profile=self.profile).exists())

    def test_name_placement_conflict_probable_name_and_permission_skip_are_distinct(self):
        """Auto-match separates unsafe placement, fuzzy proposals, and denied binding writes."""
        from dcim.models import Device, Rack

        rack = Rack.objects.create(name="workspace-rack", site=self.site, u_height=42)
        Device.objects.create(
            name="placed-device",
            site=self.site,
            rack=rack,
            position=5,
            face="front",
            device_type=self.device_type,
            role=self.role,
        )
        Device.objects.create(
            name="prefix probable-device suffix",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
        )
        exact = Device.objects.create(
            name="permission-device",
            serial="PERMISSION-SERIAL",
            site=self.site,
            device_type=self.device_type,
            role=self.role,
        )
        workspace = self._workspace(
            self._row("PLACED", "placed-device"),
            self._row("PROBABLE", "probable-device"),
            self._row("DENIED", "permission-device", serial=exact.serial),
        )
        limited = user_with_object_permission(
            "workspace-limited",
            [(Device, ("view",), {})],
        )

        summary = workspace.auto_match_devices(self.profile, limited, self.target)

        self.assertEqual(summary.placement_conflicts, 1)
        self.assertEqual(summary.probable, 1)
        self.assertEqual(summary.skipped, 1)
