# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The upgrade rekeys every stored casefold identity to the uppercase identity, and never stops on a collision."""

import hashlib
import json
from importlib import import_module

from django.db import connection
from django.db.migrations import RunPython
from django.db.migrations.executor import MigrationExecutor
from django.test import SimpleTestCase, TransactionTestCase

from netbox_data_import.field_keys import parse_termination_field_key
from netbox_data_import.identity import identity_text
from netbox_data_import.models import ImportProfile
from netbox_data_import.netbox_reader import NetBoxReader
from netbox_data_import.tests.helpers import make_dcim_objects, migrate_plugin_to_leaf, unapply_plugin_migrations_to
from netbox_data_import.trace_device_resolution import (
    MANUALLY_RESOLVED,
    DeviceEvidence,
    resolve_trace_devices,
    source_device_key,
)

APP = "netbox_data_import"
BEFORE = "0044_cableimportsource_segment_index_unknown"
REKEY = "0045_rekey_name_identities"
LOGGER = f"{APP}.migrations.{REKEY}"
DOTLESS_I = "\u0131"


def _digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def old_field_key(device, port, cards="", kind="interface", role="termination"):
    """Return a termination field key the way the casefold release stored it."""
    data = {"cards": cards, "device": device, "kind": kind, "port": port, "role": role}
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def old_trace(*endpoints):
    """Return a trace identity the way the casefold release stored it: endpoints sorted by casefold key."""
    return json.dumps(sorted(list(endpoint) for endpoint in endpoints), ensure_ascii=False, separators=(",", ":"))


class RekeyStructureTest(SimpleTestCase):
    """The data migration has no reverse and follows the schema migration it needs."""

    def test_the_rekey_refuses_to_reverse(self):
        migration = import_module(f"{APP}.migrations.{REKEY}").Migration
        (operation,) = migration.operations

        self.assertIsInstance(operation, RunPython)
        self.assertIsNone(operation.reverse_code)
        self.assertEqual(migration.dependencies, [(APP, BEFORE)])


class RekeyNameIdentitiesMigrationTest(TransactionTestCase):
    """Real rows of every affected model, written under the casefold identity, then migrated."""

    def setUp(self):
        super().setUp()
        self.addCleanup(migrate_plugin_to_leaf)
        unapply_plugin_migrations_to(BEFORE)
        self.apps = MigrationExecutor(connection).loader.project_state([(APP, BEFORE)]).apps
        from dcim.models import Device

        # DCIM is at its leaf, so the current model writes its rows and fills NetBox's own defaults.
        site, _manufacturer, device_type, role = make_dcim_objects("Rekey")
        self.devices = [
            Device.objects.create(name=f"rekey-{number}", site=site, device_type=device_type, role=role)
            for number in range(2)
        ]
        self.profile = self.model("ImportProfile").objects.create(
            name="Rekey Profile", source_adapter="trace_workbook", adapter_config={}
        )
        ObjectType = self.apps.get_model("core", "ObjectType")
        self.interface_type = ObjectType.objects.get(app_label="dcim", model="interface")
        self.device_type = ObjectType.objects.get(app_label="dcim", model="device")

    def model(self, name):
        return self.apps.get_model(APP, name)

    def termination(self, key, object_id):
        return self.model("TerminationResolution").objects.create(
            profile=self.profile,
            task_type="select_termination",
            field_key=key,
            field_key_digest=_digest(key),
            selected_object_type=self.interface_type,
            selected_object_id=object_id,
            selected_display_name=f"port {object_id}",
        )

    def proposal(self, key, status, **fields):
        return self.model("ResolutionProposal").objects.create(
            profile=self.profile,
            task_type="select_termination",
            field_key=key,
            field_key_digest=_digest(key),
            status=status,
            source_evidence={},
            resolved_device_type=self.device_type,
            resolved_device_id=self.devices[0].pk,
            prompt_version=2,
            response_schema_version=2,
            candidate_snapshot={},
            **fields,
        )

    def migrate(self):
        with self.assertLogs(LOGGER, level="WARNING") as logs:
            MigrationExecutor(connection).migrate([(APP, REKEY)])
        return "\n".join(logs.output)

    def test_termination_decisions_rekey_merge_one_target_and_drop_two(self):
        Model = self.model("TerminationResolution")
        plain = self.termination(old_field_key("dev-a", "eth0"), 11)
        sharp = self.termination(old_field_key("strasse", "eth0"), 12)
        kept = self.termination(old_field_key(f"sw-{DOTLESS_I}", "eth1"), 21)
        merged = self.termination(old_field_key("sw-i", "eth1"), 21)
        first_drop = self.termination(old_field_key(f"rack-{DOTLESS_I}", "eth2"), 31)
        second_drop = self.termination(old_field_key("rack-i", "eth2"), 32)
        decided = {"outcome": "candidate", "selected_candidate_id": "x", "selected_object_type": self.interface_type}
        decided |= {"selected_object_id": 21, "decision": "accepted", "decided_at": "2026-01-01T00:00:00Z"}
        to_merged = self.proposal(old_field_key("sw-i", "eth1"), "completed", written_resolution=merged, **decided)
        to_dropped = self.proposal(
            old_field_key("rack-i", "eth2"), "completed", written_resolution=second_drop, **decided
        )

        output = self.migrate()

        rows = {row.pk: row for row in Model.objects.all()}
        self.assertEqual(set(rows), {plain.pk, sharp.pk, kept.pk})
        expected = {
            plain.pk: old_field_key("DEV-A", "ETH0"),
            sharp.pk: old_field_key("STRASSE", "ETH0"),
            kept.pk: old_field_key("SW-I", "ETH1"),
        }
        self.assertEqual({pk: row.field_key for pk, row in rows.items()}, expected)
        self.assertEqual(
            {pk: row.field_key_digest for pk, row in rows.items()}, {pk: _digest(key) for pk, key in expected.items()}
        )
        for row in rows.values():
            parsed = parse_termination_field_key(row.field_key)
            self.assertEqual(parsed["device"], identity_text(parsed["device"]))
        Proposal = self.model("ResolutionProposal")
        self.assertEqual(Proposal.objects.get(pk=to_merged.pk).written_resolution_id, kept.pk)
        self.assertIsNone(Proposal.objects.get(pk=to_dropped.pk).written_resolution_id)
        self.assertIn(f"Merged TerminationResolution rows [{merged.pk}] into row {kept.pk}", output)
        self.assertIn(f"Dropped TerminationResolution rows [{first_drop.pk}, {second_drop.pk}]", output)

    def test_device_and_location_decisions_rekey_merge_and_drop(self):
        Device = self.model("TraceDeviceResolution")
        Location = self.model("TraceLocationResolution")
        rows = {}
        for name, key, device in (
            ("alias", "srv alias", 0),
            ("kept", f"core-{DOTLESS_I}", 0),
            ("merged", "core-i", 0),
            ("dropped-a", f"edge-{DOTLESS_I}", 0),
            ("dropped-b", "edge-i", 1),
        ):
            rows[name] = Device.objects.create(
                profile=self.profile,
                source_device_key=key,
                source_device_key_digest=_digest(key),
                selected_device_id=self.devices[device].pk,
                selected_display_name="device",
            )
        hall = Location.objects.create(
            profile=self.profile,
            source_location_key="campus >> dh4",
            source_location_key_digest=_digest("campus >> dh4"),
            selected_location_id=7,
            selected_display_name="DH4",
        )
        for key, location in ((f"row {DOTLESS_I}", 8), ("row i", 9)):
            Location.objects.create(
                profile=self.profile,
                source_location_key=key,
                source_location_key_digest=_digest(key),
                selected_location_id=location,
                selected_display_name="row",
            )

        output = self.migrate()

        self.assertEqual(
            dict(Device.objects.values_list("pk", "source_device_key")),
            {rows["alias"].pk: "SRV ALIAS", rows["kept"].pk: "CORE-I"},
        )
        self.assertEqual(
            dict(Device.objects.values_list("source_device_key", "source_device_key_digest")),
            {"SRV ALIAS": _digest("SRV ALIAS"), "CORE-I": _digest("CORE-I")},
        )
        self.assertEqual(
            list(Location.objects.values_list("pk", "source_location_key", "source_location_key_digest")),
            [(hall.pk, "CAMPUS >> DH4", _digest("CAMPUS >> DH4"))],
        )
        # The rekeyed choice answers the label it was made for, under the identity the planner now uses.
        evidence = DeviceEvidence(
            key=source_device_key("Srv  Alias"), labels=("Srv  Alias",), locations=(), racks=(), u_positions=()
        )
        reader = NetBoxReader.unrestricted().for_target(site=self.devices[0].site)
        resolved = resolve_trace_devices(
            profile=ImportProfile.objects.get(pk=self.profile.pk), reader=reader, evidence={evidence.key: evidence}
        )
        self.assertEqual(
            (resolved[evidence.key].state, resolved[evidence.key].device), (MANUALLY_RESOLVED, self.devices[0])
        )
        self.assertIn("Merged TraceDeviceResolution", output)
        self.assertIn("Dropped TraceDeviceResolution", output)
        self.assertIn("Dropped TraceLocationResolution", output)

    def test_a_permanent_table_named_like_a_staging_table_keeps_its_rows(self):
        """The staging tables are temporary, so the migration must neither drop nor write a permanent namesake."""
        migration = import_module(f"{APP}.migrations.{REKEY}")
        names = [table.rpartition(".")[2] for table in (migration.STAGE, migration.GROUPS)]
        with connection.cursor() as cursor:
            for name in names:
                cursor.execute(f"CREATE TABLE public.{name} (note text)")
                cursor.execute(f"INSERT INTO public.{name} VALUES ('kept')")  # noqa: S608 - fixed names
                self.addCleanup(self.drop_permanent, name)
        self.model("TraceDeviceResolution").objects.create(
            profile=self.profile,
            source_device_key="alias",
            source_device_key_digest=_digest("alias"),
            selected_device_id=self.devices[0].pk,
            selected_display_name="device",
        )

        MigrationExecutor(connection).migrate([(APP, REKEY)])

        with connection.cursor() as cursor:
            for name in names:
                cursor.execute(f"SELECT note FROM public.{name}")  # noqa: S608 - fixed names
                self.assertEqual(cursor.fetchall(), [("kept",)], name)
        self.assertEqual(self.model("TraceDeviceResolution").objects.get().source_device_key, "ALIAS")

    @staticmethod
    def drop_permanent(name):
        with connection.cursor() as cursor:
            cursor.execute(f"DROP TABLE IF EXISTS public.{name}")

    def test_a_large_table_is_read_staged_and_written_in_bounded_pages(self):
        """No read or write of the decision table holds more than one page, and every temporary digest goes first."""
        from django.test.utils import CaptureQueriesContext

        page = 500
        Device = self.model("TraceDeviceResolution")
        keys = [f"core-{DOTLESS_I}", *(f"dev-{number:04}" for number in range(2 * page)), "core-i"]
        Device.objects.bulk_create(
            Device(
                profile=self.profile,
                source_device_key=key,
                source_device_key_digest=_digest(key),
                selected_device_id=self.devices[0].pk,
                selected_display_name="device",
            )
            for key in keys
        )
        first, last = Device.objects.order_by("pk").values_list("pk", flat=True)[:: len(keys) - 1]
        table = connection.ops.quote_name(Device._meta.db_table)

        with CaptureQueriesContext(connection) as captured, self.assertLogs(LOGGER, level="WARNING") as logs:
            MigrationExecutor(connection).migrate([(APP, REKEY)])

        reads = [
            query["sql"] for query in captured if query["sql"].startswith("SELECT") and f"FROM {table}" in query["sql"]
        ]
        writes = [query["sql"] for query in captured if query["sql"].startswith(f"UPDATE {table}")]
        stage = import_module(f"{APP}.migrations.{REKEY}").STAGE
        stages = [query["sql"] for query in captured if query["sql"].startswith(f"INSERT INTO {stage}")]
        self.assertEqual(len(reads), 3)
        self.assertTrue(all(sql.endswith(f"LIMIT {page}") for sql in reads), reads)
        self.assertEqual(len(stages), 3)
        self.assertEqual(
            [("rekey-" in sql) for sql in writes], [True] * 3 + [False] * 3, "every temporary digest goes first"
        )
        self.assertEqual(Device.objects.count(), len(keys) - 1)
        self.assertEqual(Device.objects.get(pk=first).source_device_key, "CORE-I")
        self.assertFalse(Device.objects.filter(pk=last).exists())
        self.assertEqual(Device.objects.get(source_device_key="DEV-0999").source_device_key_digest, _digest("DEV-0999"))
        self.assertIn(f"Merged TraceDeviceResolution rows [{last}] into row {first}", "\n".join(logs.output))

    def test_a_collision_group_larger_than_a_page_is_settled_in_bounded_pages(self):
        """1200 casefold keys of one uppercase key merge, and no statement carries or returns the whole group."""
        import itertools

        page = 500
        Device = self.model("TraceDeviceResolution")
        spellings = itertools.islice(itertools.product("i" + DOTLESS_I, repeat=11), 1200)
        keys = ["dev-" + "".join(letters) for letters in spellings]
        Device.objects.bulk_create(
            Device(
                profile=self.profile,
                source_device_key=key,
                source_device_key_digest=_digest(key),
                selected_device_id=self.devices[0].pk,
                selected_display_name="device",
            )
            for key in keys
        )
        first = Device.objects.order_by("pk").values_list("pk", flat=True).first()
        statements = []

        def record(execute, sql, params, many, context):
            statements.append((sql, params or ()))
            return execute(sql, params, many, context)

        with connection.execute_wrapper(record), self.assertLogs(LOGGER, level="WARNING"):
            MigrationExecutor(connection).migrate([(APP, REKEY)])

        arrays = [len(value) for _sql, params in statements for value in params if isinstance(value, list)]
        self.assertLessEqual(max(arrays), page)
        self.assertEqual([sql for sql, _params in statements if "array_agg" in sql], [])
        self.assertEqual(list(Device.objects.values_list("pk", "source_device_key")), [(first, "DEV-" + "I" * 11)])

    def test_requests_in_flight_retire_and_every_request_keeps_its_casefold_key(self):
        queued = self.proposal(old_field_key("dev-a", "eth0"), "queued")
        running = self.proposal(old_field_key("dev-a", "eth1"), "running")
        cancelled = self.proposal(old_field_key("dev-a", "eth2"), "cancelled")

        with self.assertNoLogs(LOGGER, level="WARNING"):
            MigrationExecutor(connection).migrate([(APP, REKEY)])

        Proposal = self.model("ResolutionProposal")
        self.assertEqual(
            sorted(Proposal.objects.values_list("pk", "status", "failure_reason", "field_key", "field_key_digest")),
            sorted(
                (row.pk, status, reason, old_field_key("dev-a", port), _digest(old_field_key("dev-a", port)))
                for row, status, reason, port in (
                    (queued, "failed", "superseded_request", "eth0"),
                    (running, "failed", "superseded_request", "eth1"),
                    (cancelled, "cancelled", "", "eth2"),
                )
            ),
        )

    def merged_question(self):
        """Return a Device whose two casefold Device keys merge, its new field key, and its inventory."""
        from dcim.models import Device, Interface
        from django.contrib.auth import get_user_model

        from netbox_data_import.field_keys import termination_field_key
        from netbox_data_import.inference_backend import proposal_eligible_set_limit
        from netbox_data_import.termination_proposal import SelectTerminationTask

        device = Device.objects.create(
            name="ALIAS-I",
            site=self.devices[0].site,
            device_type=self.devices[0].device_type,
            role=self.devices[0].role,
        )
        Interface.objects.bulk_create(
            Interface(device=device, name=f"p{number}", type="1000base-t") for number in range(5)
        )
        operator = get_user_model().objects.create_superuser("rekey-operator", "rekey@example.invalid", "x")
        reader = NetBoxReader.for_actor(operator).for_target(site=device.site)
        key = termination_field_key(device="alias-i", cards="", port="absent", kind="interface")

        def inventory():
            return SelectTerminationTask().inventory(
                profile=ImportProfile.objects.get(pk=self.profile.pk),
                field_key=key,
                netbox_reader=reader,
                limit=proposal_eligible_set_limit(),
            )

        return device, key, operator, reader, inventory

    def answered(self, old_key, device, snapshot, *, outcome):
        """Store one completed attempt under a casefold key, through the proposal lifecycle."""
        from dcim.models import Device, Interface
        from core.models import ObjectType

        from netbox_data_import.field_keys import SELECT_TERMINATION_TASK
        from netbox_data_import.resolution_proposals import claim_proposal, complete_proposal, request_proposal

        proposal = request_proposal(
            profile=ImportProfile.objects.get(pk=self.profile.pk),
            task_type=SELECT_TERMINATION_TASK,
            field_key=old_key,
            source_evidence={},
            resolved_device_type=ObjectType.objects.get_for_model(Device),
            resolved_device_id=device.pk,
            prompt_version=2,
            response_schema_version=2,
            candidate_snapshot=snapshot,
        )
        claim_proposal(proposal.pk)
        entry = snapshot.page[0]
        selection = (
            {
                "selected_candidate_id": entry.candidate_id,
                "selected_object_type": ObjectType.objects.get_for_model(Interface),
                "selected_object_id": entry.object_id,
            }
            if outcome == "candidate"
            else {}
        )
        complete_proposal(proposal.pk, outcome=outcome, explanation="answered", **selection)
        return proposal

    def test_an_exhausted_page_of_a_casefold_question_does_not_move_the_merged_question(self):
        from netbox_data_import.field_keys import SELECT_TERMINATION_TASK
        from netbox_data_import.resolution_proposals import next_page_offset

        device, key, _operator, _reader, inventory = self.merged_question()
        page = inventory().candidate_snapshot.with_page(offset=0, size=2)
        self.answered(old_field_key(f"alias-{DOTLESS_I}", "absent"), device, page, outcome="no_match")
        self.assertTrue(page.has_next_page)

        MigrationExecutor(connection).migrate([(APP, REKEY)])

        offset = next_page_offset(
            profile=ImportProfile.objects.get(pk=self.profile.pk),
            task_type=SELECT_TERMINATION_TASK,
            field_key=key,
            inventory=inventory(),
        )
        self.assertEqual(offset, 0)

    def test_a_completed_candidate_of_a_casefold_question_cannot_be_accepted(self):
        from django.core.exceptions import ValidationError

        from netbox_data_import.models import TerminationResolution
        from netbox_data_import.proposal_decisions import accept_proposal

        device, _key, operator, reader, inventory = self.merged_question()
        page = inventory().candidate_snapshot.with_page(offset=0, size=2)
        proposal = self.answered(old_field_key(f"alias-{DOTLESS_I}", "absent"), device, page, outcome="candidate")

        MigrationExecutor(connection).migrate([(APP, REKEY)])

        with self.assertRaises(ValidationError):
            accept_proposal(
                proposal.pk,
                source={"device": "alias-i", "cards": "", "port": "absent"},
                operator=operator,
                netbox_reader=reader,
                reviewed_fingerprint=ImportProfile.objects.get(pk=self.profile.pk).planning_fingerprint,
            )
        self.assertFalse(TerminationResolution.objects.exists())

    def test_provenance_reorders_its_endpoints_and_overrides_follow(self):
        from dcim.models import Cable, Interface

        first = Interface.objects.create(device_id=self.devices[0].pk, name="eth0", type="1000base-t")
        second = Interface.objects.create(device_id=self.devices[1].pk, name="eth0", type="1000base-t")
        cable = Cable(a_terminations=[first], b_terminations=[second])
        cable.save()
        Source = self.model("CableImportSource")
        # '_' sorts before 'b' and after 'B', so uppercase reverses the canonical endpoint order.
        underscore = old_trace(("a_x", "", "p1", "interface"), ("ab", "", "p2", "interface"))
        steady = old_trace(("dev-a", "", "eth0", "interface"), ("dev-b", "", "eth1", "interface"))
        merged_pair = (
            old_trace((f"dev-{DOTLESS_I}", "", "eth0", "interface"), ("dev-0", "", "eth1", "interface")),
            old_trace(("dev-i", "", "eth0", "interface"), ("dev-0", "", "eth1", "interface")),
        )
        rows = {}
        for name, identity, index, direction in (
            ("reversed", underscore, 2, "canonical"),
            ("steady", steady, 1, "reversed"),
            ("kept", merged_pair[0], 0, "canonical"),
            ("merged", merged_pair[1], 0, "canonical"),
        ):
            rows[name] = Source.objects.create(
                cable_id=cable.pk,
                profile=self.profile,
                trace_identity=identity,
                trace_key=_digest(identity),
                segment_index=index,
                direction=direction,
            )
        override = self.model("CableSegmentOverride").objects.create(
            profile=self.profile,
            segment_key="dcim.interface:1|dcim.interface:2",
            source_trace_identity=underscore,
            segment_index=2,
        )

        output = self.migrate()

        reordered = json.dumps([["AB", "", "P2", "interface"], ["A_X", "", "P1", "interface"]], separators=(",", ":"))
        expected = {
            rows["reversed"].pk: (reordered, None, "reversed"),
            rows["steady"].pk: (
                old_trace(("DEV-A", "", "ETH0", "interface"), ("DEV-B", "", "ETH1", "interface")),
                1,
                "reversed",
            ),
            rows["kept"].pk: (
                old_trace(("DEV-I", "", "ETH0", "interface"), ("DEV-0", "", "ETH1", "interface")),
                0,
                "canonical",
            ),
        }
        self.assertEqual(
            {row.pk: (row.trace_identity, row.segment_index, row.direction) for row in Source.objects.all()}, expected
        )
        self.assertEqual(
            {row.pk: row.trace_key for row in Source.objects.all()},
            {pk: _digest(identity) for pk, (identity, _index, _direction) in expected.items()},
        )
        override = self.model("CableSegmentOverride").objects.get(pk=override.pk)
        self.assertEqual((override.source_trace_identity, override.segment_index), (reordered, 2))
        self.assertIn(f"CableImportSource rows [{rows['reversed'].pk}]: the canonical endpoint order", output)
        self.assertIn(f"Merged CableImportSource rows [{rows['merged'].pk}]", output)
