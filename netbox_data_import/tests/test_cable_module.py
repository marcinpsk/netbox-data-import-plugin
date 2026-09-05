# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Plan and write cable traces through the real adapter, engine, and Cable Target Module."""

import json
import secrets
import uuid
from io import BytesIO

from core.models import ObjectType
from dcim.models import (
    Cable,
    CableTermination,
    ConsolePort,
    Device,
    FrontPort,
    Interface,
    PortMapping,
    RearPort,
    Site,
)
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from extras.models import Tag

from netbox_data_import.cable_target import CableModule, eligible_terminations
from netbox_data_import.field_keys import (
    MAPPED_PEER_ROLE,
    SELECT_TERMINATION_TASK,
    TERMINATION_ROLE,
    claimed_termination_kind,
    parse_termination_field_key,
    termination_field_key,
)
from netbox_data_import.import_engine import ImportEngine
from netbox_data_import.models import (
    CableClassMapping,
    CableImportSource,
    ImportProfile,
    SourceDocument,
    TerminationResolution,
    index_digest,
)
from netbox_data_import.netbox_reader import NetBoxReader
from netbox_data_import.plan import Disposition, PlannedChange
from netbox_data_import.target_runtime import ExecutionContext, PreconditionFailed
from netbox_data_import.tests.helpers import (
    make_dcim_objects,
    competing_write_during,
    run_on_separate_connection,
    trace_endpoint_line,
    trace_segment,
    trace_termination,
    trace_workbook_bytes,
    user_with_object_permission,
)

User = get_user_model()

DEVICE_A = trace_termination("DEV-A", "", "eth0", "Port")
DEVICE_B = trace_termination("DEV-B", "", "eth1", "NIC")
PANEL_1_FRONT = trace_termination("PANEL-1", "", "F1", "Position Front")
PANEL_1_REAR = trace_termination("PANEL-1", "", "R1", "Punch-Down")
PANEL_2_FRONT = trace_termination("PANEL-2", "", "F1", "Position Front")
PANEL_2_REAR = trace_termination("PANEL-2", "", "R1", "Punch-Down")


def patched_path(from_end=DEVICE_A, to_end=DEVICE_B):
    """Return one path block whose trace crosses two panels through their rear-port trunk."""
    return (
        trace_endpoint_line(from_end),
        trace_endpoint_line(to_end),
        (
            trace_segment(from_end, "Patch", PANEL_1_FRONT),
            trace_segment(PANEL_1_REAR, "Trunk", PANEL_2_REAR),
            trace_segment(PANEL_2_FRONT, "Patch", to_end),
        ),
    )


def direct_path(from_end=DEVICE_A, to_end=DEVICE_B):
    """Return one path block whose trace states a single direct segment."""
    return (
        trace_endpoint_line(from_end),
        trace_endpoint_line(to_end),
        (trace_segment(from_end, "Patch", to_end),),
    )


def reversed_patched_path():
    """Return the standard patched path stated from DEV-B back to DEV-A."""
    return (
        trace_endpoint_line(DEVICE_B),
        trace_endpoint_line(DEVICE_A),
        (
            trace_segment(DEVICE_B, "Patch", PANEL_2_FRONT),
            trace_segment(PANEL_2_REAR, "Trunk", PANEL_1_REAR),
            trace_segment(PANEL_1_FRONT, "Patch", DEVICE_A),
        ),
    )


def same_rear_port_path():
    """Return one path block that re-enters the trunk through the rear port it just left."""
    return (
        trace_endpoint_line(DEVICE_A),
        trace_endpoint_line(DEVICE_B),
        (
            trace_segment(DEVICE_A, "Patch", PANEL_1_REAR),
            trace_segment(PANEL_1_REAR, "Trunk", PANEL_2_REAR),
            trace_segment(PANEL_2_FRONT, "Patch", DEVICE_B),
        ),
    )


class CableTopologyMixin:
    """Build the two devices, the two panels, and the Cable policy every trace test needs."""

    @classmethod
    def build_topology(cls):
        """Create the shared NetBox objects and the Import Profile that plans against them."""
        cls.actor = User.objects.create_superuser("cable-operator", "cable@example.com", "testpass")
        cls.site, _manufacturer, cls.device_type, cls.role = make_dcim_objects("Cable")
        cls.profile = ImportProfile.objects.create(
            name="Cable Traces",
            source_adapter="trace_workbook",
            adapter_config={},
        )
        for cable_class in ("Patch", "Trunk"):
            CableClassMapping.objects.create(
                profile=cls.profile,
                cable_class=cable_class,
                cable_type_resolved=True,
                cable_type="cat6",
                cable_profile_resolved=True,
                cable_profile="single-1c1p",
            )
        cls.device_a = cls.make_device("DEV-A")
        cls.device_b = cls.make_device("DEV-B")
        cls.eth0 = Interface.objects.create(device=cls.device_a, name="eth0", type="1000base-t")
        cls.eth1 = Interface.objects.create(device=cls.device_b, name="eth1", type="1000base-t")
        cls.panel_1, cls.panel_1_fronts, cls.panel_1_rear = cls.make_panel("PANEL-1")
        cls.panel_2, cls.panel_2_fronts, cls.panel_2_rear = cls.make_panel("PANEL-2")
        cls.planning_context = {"site_id": cls.site.pk, "location_id": None, "tenant_id": None}

    @classmethod
    def make_device(cls, name):
        """Create one Device at the shared site."""
        return Device.objects.create(name=name, site=cls.site, device_type=cls.device_type, role=cls.role)

    @classmethod
    def rebuild_panel(cls, name, fronts):
        """Replace one panel with a wider one, whose rear port maps to several front ports."""
        Device.objects.filter(name=name, site=cls.site).delete()
        return cls.make_panel(name, fronts=fronts)

    @classmethod
    def make_panel(cls, name, fronts=1):
        """Create one patch panel whose front ports all map to its single rear port."""
        device = cls.make_device(name)
        rear = RearPort.objects.create(device=device, name="R1", type="8p8c", positions=fronts)
        front_ports = []
        for position in range(1, fronts + 1):
            front = FrontPort.objects.create(device=device, name=f"F{position}", type="8p8c")
            PortMapping.objects.create(
                front_port=front,
                rear_port=rear,
                front_port_position=1,
                rear_port_position=position,
            )
            front_ports.append(front)
        return device, front_ports, rear

    @staticmethod
    def connect(first, second, **attributes):
        """Create one real NetBox Cable between two terminations."""
        cable = Cable(
            a_terminations=[first],
            b_terminations=[second],
            status=attributes.pop("status", "connected"),
            type=attributes.pop("type", "cat6"),
            profile=attributes.pop("profile", "single-1c1p"),
            **attributes,
        )
        cable.full_clean()
        cable.save()
        return cable

    def plan(self, *blocks, actor=None):
        """Plan the given path blocks through the public coordinator seam."""
        document = SourceDocument.store(
            profile=self.profile,
            content=trace_workbook_bytes(path_blocks=blocks),
        )
        self.document = document
        return ImportEngine.plan(self.profile, document, actor or self.actor, self.planning_context)

    def unit(self, *blocks, actor=None):
        """Plan one path block and return the single unit it produces."""
        plan = self.plan(*blocks, actor=actor)
        self.assertEqual(len(plan.units), len(blocks), plan.units)
        return plan.units[0]

    def codes(self, unit):
        """Return the diagnostic codes one unit carries, in order."""
        return [diagnostic.code for diagnostic in unit.diagnostics]

    def termination_pairs(self, change):
        """Return the sorted termination pairs one create change writes."""
        return [(item[0], item[1]) for item in change.payload["terminations"]]

    def provenance_positions(self):
        """Return canonical stored provenance keyed by each Cable's physical terminations."""
        positions = {}
        for source in CableImportSource.objects.all():
            terminations = CableTermination.objects.filter(cable=source.cable).select_related("termination_type")
            key = tuple(
                sorted(
                    (f"{row.termination_type.app_label}.{row.termination_type.model}", row.termination_id)
                    for row in terminations
                )
            )
            positions[key] = (source.trace_identity, source.segment_index)
        return positions


class CablePlanningTest(CableTopologyMixin, TestCase):
    """Section 6 planning: comparison, precedence, substitution, and every blocking condition."""

    @classmethod
    def setUpTestData(cls):
        cls.build_topology()

    def test_a_trace_with_patching_replaces_the_logical_cable(self):
        """The unit deletes the direct cable first, then creates each segment in canonical order."""
        logical = self.connect(self.eth0, self.eth1)

        unit = self.unit(patched_path())

        self.assertEqual(unit.disposition, Disposition.ACTIONABLE)
        self.assertEqual([change.operation for change in unit.changes], ["delete", "create", "create", "create"])
        deletion = unit.changes[0]
        self.assertEqual(deletion.payload["cable_id"], logical.pk)
        for creation in unit.changes[1:]:
            self.assertEqual(creation.dependencies, (deletion.identity,))
        self.assertEqual(
            [self.termination_pairs(change) for change in unit.changes[1:]],
            [
                sorted([("dcim.interface", self.eth0.pk), ("dcim.frontport", self.panel_1_fronts[0].pk)]),
                sorted([("dcim.rearport", self.panel_1_rear.pk), ("dcim.rearport", self.panel_2_rear.pk)]),
                sorted([("dcim.frontport", self.panel_2_fronts[0].pk), ("dcim.interface", self.eth1.pk)]),
            ],
        )

    def test_a_panel_that_names_its_front_and_rear_port_alike_plans_each_kind(self):
        """A shared port label is a real panel shape, and each segment still takes its own kind."""
        panel = self.make_device("PANEL-3")
        rear = RearPort.objects.create(device=panel, name="P1", type="8p8c", positions=1)
        front = FrontPort.objects.create(device=panel, name="P1", type="8p8c")
        PortMapping.objects.create(front_port=front, rear_port=rear, front_port_position=1, rear_port_position=1)
        shared_front = trace_termination("PANEL-3", "", "P1", "Position Front")
        shared_rear = trace_termination("PANEL-3", "", "P1", "Punch-Down")
        block = (
            trace_endpoint_line(DEVICE_A),
            trace_endpoint_line(DEVICE_B),
            (
                trace_segment(DEVICE_A, "Patch", shared_front),
                trace_segment(shared_rear, "Trunk", PANEL_2_REAR),
                trace_segment(PANEL_2_FRONT, "Patch", DEVICE_B),
            ),
        )

        unit = self.unit(block)

        self.assertEqual(unit.disposition, Disposition.ACTIONABLE)
        creations = [change for change in unit.changes if change.operation == "create"]
        self.assertEqual(
            [self.termination_pairs(change) for change in creations],
            [
                sorted([("dcim.interface", self.eth0.pk), ("dcim.frontport", front.pk)]),
                sorted([("dcim.rearport", rear.pk), ("dcim.rearport", self.panel_2_rear.pk)]),
                sorted([("dcim.frontport", self.panel_2_fronts[0].pk), ("dcim.interface", self.eth1.pk)]),
            ],
        )

    def test_logical_cable_deletion_review_carries_description_and_sorted_tags(self):
        """The operator reviews the Logical Cable description and tags before approving deletion."""
        logical = self.connect(self.eth0, self.eth1, description="Temporary logical path")
        later = Tag.objects.create(name="Later", slug="later")
        earlier = Tag.objects.create(name="Earlier", slug="earlier")
        logical.tags.add(later, earlier)

        unit = self.unit(patched_path())

        deletion = unit.changes[0]
        self.assertEqual(deletion.payload["description"], "Temporary logical path")
        self.assertEqual(list(deletion.payload["tags"]), ["Earlier", "Later"])

    def test_a_trace_with_no_direct_cable_is_a_creation_only_replacement(self):
        """With nothing to remove the unit carries creations alone, and they wait on nothing."""
        unit = self.unit(patched_path())

        self.assertEqual(unit.disposition, Disposition.ACTIONABLE)
        self.assertEqual([change.operation for change in unit.changes], ["create", "create", "create"])
        self.assertEqual([change.dependencies for change in unit.changes], [(), (), ()])

    def test_a_single_segment_trace_with_a_matching_cable_is_a_no_op(self):
        """Segment precedence makes the direct cable a proven segment, never the Logical Cable."""
        existing = self.connect(self.eth0, self.eth1)

        unit = self.unit(direct_path())

        self.assertEqual(unit.disposition, Disposition.NO_OP)
        self.assertEqual(unit.changes, ())
        self.assertIn("cable.segment_reused", self.codes(unit))
        self.assertTrue(Cable.objects.filter(pk=existing.pk).exists())

    def test_a_complete_existing_path_is_a_no_op(self):
        """Every segment proven and no Logical Cable left means there is nothing to do."""
        self.connect(self.eth0, self.panel_1_fronts[0])
        self.connect(self.panel_1_rear, self.panel_2_rear)
        self.connect(self.panel_2_fronts[0], self.eth1)

        unit = self.unit(patched_path())

        self.assertEqual(unit.disposition, Disposition.NO_OP)
        self.assertEqual(unit.changes, ())

    def test_a_partial_path_reuses_its_proven_segments(self):
        """A proven trunk is kept, so only the missing segments are created."""
        trunk = self.connect(self.panel_1_rear, self.panel_2_rear)

        unit = self.unit(patched_path())

        self.assertEqual(unit.disposition, Disposition.ACTIONABLE)
        self.assertEqual([change.operation for change in unit.changes], ["create", "create"])
        reuse = next(item for item in unit.diagnostics if item.code == "cable.segment_reused")
        self.assertIn(f"dcim.cable:{trunk.pk}", reuse.identities)

    def test_a_reused_cable_reports_attribute_drift_without_changing_the_disposition(self):
        """Drift on a matched Cable is review information, not a write."""
        self.connect(
            self.eth0,
            self.eth1,
            type="cat5e",
            status="planned",
            profile="single-1c2p",
            label="PATCH-9",
        )

        unit = self.unit(direct_path())

        self.assertEqual(unit.disposition, Disposition.NO_OP)
        self.assertEqual(unit.changes, ())
        drift = next(item for item in unit.diagnostics if item.code == "cable.attribute_drift")
        self.assertEqual(
            {key: drift.display[key] for key in ("type", "status", "profile", "label")},
            {"type": "cat5e", "status": "planned", "profile": "single-1c2p", "label": "PATCH-9"},
        )

    def test_a_unique_mapped_peer_substitutes_and_appears_in_the_plan(self):
        """The second cable end moves to the one front port NetBox maps the rear port to."""
        unit = self.unit(same_rear_port_path())

        self.assertEqual(unit.disposition, Disposition.ACTIONABLE)
        substitution = next(item for item in unit.diagnostics if item.code == "cable.same_port_continuation")
        self.assertEqual(substitution.display["peer"], str(self.panel_1_fronts[0]))
        self.assertEqual(
            self.termination_pairs(unit.changes[1]),
            sorted([("dcim.frontport", self.panel_1_fronts[0].pk), ("dcim.rearport", self.panel_2_rear.pk)]),
        )

    def test_several_mapped_peers_block_the_unit_and_offer_exactly_them(self):
        """A fan-out rear port needs the operator to say which front port continues the path."""
        panel, fronts, _rear = self.rebuild_panel("PANEL-1", fronts=2)
        del panel

        unit = self.unit(same_rear_port_path())

        self.assertEqual(unit.disposition, Disposition.BLOCKED)
        ambiguous = next(item for item in unit.diagnostics if item.code == "cable.ambiguous_mapped_peer")
        self.assertEqual(list(ambiguous.display["peers"]), sorted(str(front) for front in fronts))

    def test_a_stored_mapped_peer_resolution_is_reused_on_a_replan(self):
        """The saved second-role decision resolves the fan-out without another operator visit."""
        _panel, fronts, _rear = self.rebuild_panel("PANEL-1", fronts=2)
        blocked = self.unit(same_rear_port_path())
        self.assertEqual(blocked.disposition, Disposition.BLOCKED)
        field_key = termination_field_key(
            device="PANEL-1",
            cards="",
            port="R1",
            kind="rear_port",
            role=MAPPED_PEER_ROLE,
        )
        reader = NetBoxReader.for_actor(self.actor).for_target(site=self.site)
        result = eligible_terminations(field_key, reader, profile=self.profile)
        self.assertEqual(result.candidates, tuple(fronts))
        self.save_resolution(PANEL_1_REAR, result.candidates[1], role=MAPPED_PEER_ROLE)

        unit = self.unit(same_rear_port_path())

        self.assertEqual(unit.disposition, Disposition.ACTIONABLE)
        self.assertEqual(
            self.termination_pairs(unit.changes[1]),
            sorted([("dcim.frontport", fronts[1].pk), ("dcim.rearport", self.panel_2_rear.pk)]),
        )

    def test_a_stored_termination_resolution_is_reused_on_a_replan(self):
        """A port name the source states differently is resolved once and then stays resolved."""
        renamed = trace_termination("DEV-A", "", "GigabitEthernet0/1", "Port")
        blocked = self.unit(patched_path(from_end=renamed))
        self.assertEqual(blocked.disposition, Disposition.BLOCKED)
        self.assertIn("cable.termination_unresolved", self.codes(blocked))

        self.save_resolution(renamed, self.eth0)
        unit = self.unit(patched_path(from_end=renamed))

        self.assertEqual(unit.disposition, Disposition.ACTIONABLE)
        self.assertEqual(
            self.termination_pairs(unit.changes[0]),
            sorted([("dcim.interface", self.eth0.pk), ("dcim.frontport", self.panel_1_fronts[0].pk)]),
        )

    def test_stored_resolution_query_is_limited_to_batch_reference_keys_and_joins_object_types(self):
        """One workbook does not load unrelated decision history or query ObjectType per saved row."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        renamed = trace_termination("DEV-A", "", "source-port", "Port")
        self.save_resolution(renamed, self.eth0)
        object_type = ObjectType.objects.get_for_model(Interface)
        for index in range(12):
            TerminationResolution.objects.create(
                profile=self.profile,
                task_type=SELECT_TERMINATION_TASK,
                field_key=termination_field_key(
                    device=f"UNUSED-{index}",
                    cards="",
                    port="not-in-workbook",
                    kind="interface",
                ),
                selected_object_type=object_type,
                selected_object_id=self.eth0.pk,
                selected_display_name=str(self.eth0),
            )

        with CaptureQueriesContext(connection) as captured:
            unit = self.unit(direct_path(from_end=renamed))

        table = TerminationResolution._meta.db_table
        table_queries = [query["sql"] for query in captured.captured_queries if f'FROM "{table}"' in query["sql"]]
        # The profile fingerprint reads every policy row by design, so select the batch planner query.
        planner_queries = [query for query in table_queries if f'"{table}"."field_key_digest" IN' in query]
        self.assertEqual(len(planner_queries), 1, table_queries)
        planner_query = planner_queries[0]
        self.assertIn(f'JOIN "{ObjectType._meta.db_table}"', planner_query)
        # The constraint indexes the digest, so each role is asserted through the digest it carries.
        for role in (TERMINATION_ROLE, MAPPED_PEER_ROLE):
            asked = termination_field_key(device="DEV-A", cards="", port="source-port", kind="interface", role=role)
            self.assertIn(index_digest(asked), planner_query)
        for index in range(12):
            unused = termination_field_key(device=f"UNUSED-{index}", cards="", port="not-in-workbook", kind="interface")
            self.assertNotIn(index_digest(unused), planner_query)
        self.assertNotIn("UNUSED-", planner_query)
        self.assertEqual(unit.disposition, Disposition.ACTIONABLE)

    def test_a_stored_resolution_that_left_the_device_asks_the_operator_again(self):
        """A saved selection no longer on the resolved Device cannot answer the question it was asked."""
        elsewhere = Interface.objects.create(device=self.make_device("DEV-C"), name="eth9", type="1000base-t")
        self.save_resolution(DEVICE_A, elsewhere)

        unit = self.unit(patched_path())

        self.assertEqual(unit.disposition, Disposition.BLOCKED)
        unresolved = next(item for item in unit.diagnostics if item.code == "cable.termination_unresolved")
        self.assertEqual(unresolved.display["selected_display_name"], str(elsewhere))

    def test_a_stored_termination_resolution_with_the_wrong_kind_is_blocked(self):
        """A FrontPort selection cannot satisfy a source reference whose PortClass claims a RearPort."""
        self.save_resolution(PANEL_1_REAR, self.panel_1_fronts[0])

        unit = self.unit(direct_path(from_end=PANEL_1_REAR))

        self.assertEqual(unit.disposition, Disposition.BLOCKED)
        self.assertEqual(unit.changes, ())
        mismatch = next(item for item in unit.diagnostics if item.code == "cable.termination_kind_mismatch")
        self.assertEqual(mismatch.display["claimed_kind"], "rear_port")
        self.assertEqual(mismatch.display["selected_kind"], "front_port")

    def test_a_same_port_continuation_through_an_unmapped_port_is_invalid(self):
        """A rear port NetBox maps to nothing cannot continue the path to a second cable."""
        PortMapping.objects.filter(device=self.panel_1).delete()

        unit = self.unit(same_rear_port_path())

        self.assertEqual(unit.disposition, Disposition.INVALID)
        finding = next(item for item in unit.diagnostics if item.code == "cable.pass_through_not_mapped")
        self.assertEqual(list(finding.display["mapped"]), [])

    def test_a_resolved_object_of_an_unsupported_kind_is_invalid(self):
        """No operator decision makes a console port a cable end this module writes."""
        console = ConsolePort.objects.create(device=self.device_a, name="con0", type="rj-45")
        self.save_resolution(DEVICE_A, console)

        unit = self.unit(patched_path())

        self.assertEqual(unit.disposition, Disposition.INVALID)
        self.assertIn("cable.unsupported_termination_kind", self.codes(unit))

    def test_a_contradicted_pass_through_claim_is_invalid(self):
        """A panel NetBox does not map cannot realize the path, and the finding names both ports."""
        PortMapping.objects.filter(device=self.panel_1).delete()

        unit = self.unit(patched_path())

        self.assertEqual(unit.disposition, Disposition.INVALID)
        finding = next(item for item in unit.diagnostics if item.code == "cable.pass_through_not_mapped")
        self.assertEqual(finding.display["entry"], str(self.panel_1_fronts[0]))
        self.assertEqual(finding.display["exit"], str(self.panel_1_rear))
        self.assertEqual(list(finding.display["mapped"]), [])

    def test_an_invisible_sibling_peer_does_not_block_a_visible_pass_through(self):
        """A stated RearPort-to-FrontPort mapping needs no access to a sibling mapped FrontPort."""
        _panel, fronts, _rear = self.rebuild_panel("PANEL-1", fronts=2)
        path = (
            trace_endpoint_line(DEVICE_A),
            trace_endpoint_line(DEVICE_B),
            (
                trace_segment(DEVICE_A, "Patch", PANEL_1_REAR),
                trace_segment(PANEL_1_FRONT, "Patch", DEVICE_B),
            ),
        )
        actor = user_with_object_permission(
            "cable-visible-pass-through",
            [
                (Site, ("view",), {}),
                (Device, ("view",), {}),
                (Interface, ("view",), {}),
                (FrontPort, ("view",), {"pk": fronts[0].pk}),
                (RearPort, ("view",), {}),
                (Cable, ("add",), {}),
            ],
        )

        unit = self.unit(path, actor=actor)

        self.assertEqual(unit.disposition, Disposition.ACTIONABLE)
        self.assertIn("cable.pass_through_verified", self.codes(unit))
        self.assertNotIn("cable.permission_denied", self.codes(unit))

    def test_a_foreign_cable_on_a_desired_termination_blocks_the_unit(self):
        """The plugin never removes a cable it did not plan, so the operator clears it first."""
        other = self.make_device("DEV-C")
        foreign = self.connect(
            self.panel_1_fronts[0], Interface.objects.create(device=other, name="eth9", type="1000base-t")
        )

        unit = self.unit(patched_path())

        self.assertEqual(unit.disposition, Disposition.BLOCKED)
        self.assertEqual(unit.changes, ())
        occupied = next(item for item in unit.diagnostics if item.code == "cable.termination_occupied")
        self.assertIn(f"dcim.cable:{foreign.pk}", occupied.identities)
        self.assertIs(occupied.display["cable_visible"], True)
        self.assertEqual(occupied.display["cable"], str(foreign))
        self.assertTrue(Cable.objects.filter(pk=foreign.pk).exists())

    def test_an_invisible_occupying_cable_blocks_without_disclosing_its_identity(self):
        """Occupancy stays unscoped, but its Cable details stay inside the actor's view scope."""
        other = self.make_device("DEV-C")
        foreign = self.connect(
            self.panel_1_fronts[0],
            Interface.objects.create(device=other, name="eth9", type="1000base-t"),
        )
        actor = user_with_object_permission(
            "cable-hidden-occupant",
            [
                (Site, ("view",), {}),
                (Device, ("view",), {}),
                (Interface, ("view",), {}),
                (FrontPort, ("view",), {}),
                (RearPort, ("view",), {}),
                (Cable, ("add",), {}),
            ],
        )
        self.assertFalse(actor.has_perm("dcim.view_cable", foreign))

        unit = self.unit(patched_path(), actor=actor)

        self.assertEqual(unit.disposition, Disposition.BLOCKED)
        occupied = next(item for item in unit.diagnostics if item.code == "cable.termination_occupied")
        self.assertIs(occupied.display["cable_visible"], False)
        self.assertNotIn("cable", occupied.display)
        self.assertNotIn(f"dcim.cable:{foreign.pk}", occupied.identities)

    def test_an_invisible_reused_cable_does_not_disclose_its_identity(self):
        """A matching Cable stays hidden when the actor can view only its endpoint Interfaces."""
        existing = self.connect(self.eth0, self.eth1)
        actor = user_with_object_permission(
            "cable-hidden-reuse",
            [
                (Site, ("view",), {}),
                (Device, ("view",), {}),
                (Interface, ("view",), {}),
            ],
        )
        self.assertFalse(actor.has_perm("dcim.view_cable", existing))

        unit = self.unit(direct_path(), actor=actor)

        self.assertEqual(unit.disposition, Disposition.NO_OP)
        reused = next(item for item in unit.diagnostics if item.code == "cable.segment_reused")
        self.assertIs(reused.display["cable_visible"], False)
        self.assertNotIn("cable", reused.display)
        self.assertNotIn(f"dcim.cable:{existing.pk}", reused.identities)

    def test_an_invisible_reused_cable_redacts_attribute_drift(self):
        """A hidden reused Cable reports drift without exposing its attributes or identity."""
        existing = self.connect(
            self.eth0,
            self.eth1,
            type="cat5e",
            status="planned",
            profile="single-1c2p",
            label="PATCH-9",
        )
        actor = user_with_object_permission(
            "cable-hidden-drift",
            [
                (Site, ("view",), {}),
                (Device, ("view",), {}),
                (Interface, ("view",), {}),
            ],
        )
        self.assertFalse(actor.has_perm("dcim.view_cable", existing))

        unit = self.unit(direct_path(), actor=actor)

        reused = next(item for item in unit.diagnostics if item.code == "cable.segment_reused")
        self.assertIs(reused.display["cable_visible"], False)
        self.assertNotIn("cable", reused.display)
        self.assertNotIn(f"dcim.cable:{existing.pk}", reused.identities)
        drifts = [item for item in unit.diagnostics if item.code == "cable.attribute_drift"]
        self.assertEqual(len(drifts), 1)
        drift = drifts[0]
        self.assertEqual(dict(drift.display), {"segment_index": 0, "cable_visible": False})
        self.assertNotIn(f"dcim.cable:{existing.pk}", drift.identities)

    def test_a_multi_termination_cable_touching_a_desired_port_blocks_the_unit(self):
        """A cable with two terminations on one side never matches a desired segment."""
        other = self.make_device("DEV-C")
        second = Interface.objects.create(device=other, name="eth9", type="1000base-t")
        third = Interface.objects.create(device=other, name="eth8", type="1000base-t")
        bundle = Cable(a_terminations=[self.eth0], b_terminations=[second, third], status="connected")
        bundle.full_clean()
        bundle.save()

        unit = self.unit(patched_path())

        self.assertEqual(unit.disposition, Disposition.BLOCKED)
        self.assertIn("cable.multi_termination_conflict", self.codes(unit))

    def test_an_unmapped_cable_class_blocks_the_unit(self):
        """A segment cannot be written until its CableClass names a Cable Type and Profile."""
        CableClassMapping.objects.filter(profile=self.profile, cable_class="Trunk").delete()

        unit = self.unit(patched_path())

        self.assertEqual(unit.disposition, Disposition.BLOCKED)
        self.assertIn("cable.cableclass_unmapped", self.codes(unit))

    def test_a_stale_cable_class_mapping_blocks_the_unit(self):
        """A stored value this NetBox no longer offers has its own diagnostic."""
        CableClassMapping.objects.filter(profile=self.profile, cable_class="Trunk").update(cable_type="obsolete-type")

        unit = self.unit(patched_path())

        self.assertEqual(unit.disposition, Disposition.BLOCKED)
        self.assertIn("cable.cableclass_stale_mapping", self.codes(unit))

    def test_an_incompatible_cable_profile_blocks_the_unit(self):
        """A Cable Profile with more than one connector per side cannot carry one termination each."""
        CableClassMapping.objects.filter(profile=self.profile, cable_class="Trunk").update(cable_profile="trunk-2c1p")

        unit = self.unit(patched_path())

        self.assertEqual(unit.disposition, Disposition.BLOCKED)
        self.assertIn("cable.profile_incompatible", self.codes(unit))

    def test_a_device_the_import_cannot_resolve_blocks_the_unit(self):
        """Endpoint Device resolution runs before port resolution and reports its own condition."""
        unit = self.unit(patched_path(to_end=trace_termination("DEV-GONE", "", "eth1", "NIC")))

        self.assertEqual(unit.disposition, Disposition.BLOCKED)
        self.assertIn("trace.device_unresolved", self.codes(unit))

    def test_endpoint_evidence_only_blocks_when_no_direct_cable_exists(self):
        """A Trace List block states endpoints alone, so nothing proves the physical path."""
        content = trace_workbook_bytes(
            include_path=False,
            include_list=True,
            list_blocks=(
                (
                    trace_endpoint_line(DEVICE_A),
                    trace_endpoint_line(DEVICE_B),
                    (("", "", "", "DEV-A", "", "eth0", "Port", "Ignored"),),
                ),
            ),
        )
        document = SourceDocument.store(profile=self.profile, content=content)

        plan = ImportEngine.plan(self.profile, document, self.actor, self.planning_context)

        self.assertEqual(plan.units[0].disposition, Disposition.BLOCKED)
        self.assertIn("trace.endpoint_evidence_only", self.codes(plan.units[0]))

    def test_endpoint_evidence_only_is_a_no_op_when_the_direct_cable_exists(self):
        """The stated endpoints are already joined, so the evidence is satisfied and nothing moves."""
        existing = self.connect(self.eth0, self.eth1)
        content = trace_workbook_bytes(
            include_path=False,
            include_list=True,
            list_blocks=(
                (
                    trace_endpoint_line(DEVICE_A),
                    trace_endpoint_line(DEVICE_B),
                    (("", "", "", "DEV-A", "", "eth0", "Port", "Ignored"),),
                ),
            ),
        )
        document = SourceDocument.store(profile=self.profile, content=content)

        plan = ImportEngine.plan(self.profile, document, self.actor, self.planning_context)

        self.assertEqual(plan.units[0].disposition, Disposition.NO_OP)
        self.assertEqual(plan.units[0].changes, ())
        self.assertTrue(Cable.objects.filter(pk=existing.pk).exists())

    def test_endpoint_evidence_does_not_disclose_an_invisible_reused_cable(self):
        """An Endpoint Summary can prove a hidden Cable exists without naming it."""
        existing = self.connect(self.eth0, self.eth1)
        actor = user_with_object_permission(
            "cable-hidden-endpoint-evidence",
            [
                (Site, ("view",), {}),
                (Device, ("view",), {}),
                (Interface, ("view",), {}),
            ],
        )
        self.assertFalse(actor.has_perm("dcim.view_cable", existing))
        content = trace_workbook_bytes(
            include_path=False,
            include_list=True,
            list_blocks=(
                (
                    trace_endpoint_line(DEVICE_A),
                    trace_endpoint_line(DEVICE_B),
                    (("", "", "", "DEV-A", "", "eth0", "Port", "Ignored"),),
                ),
            ),
        )
        document = SourceDocument.store(profile=self.profile, content=content)

        plan = ImportEngine.plan(self.profile, document, actor, self.planning_context)

        reused = next(item for item in plan.units[0].diagnostics if item.code == "cable.segment_reused")
        self.assertIs(reused.display["cable_visible"], False)
        self.assertNotIn("cable", reused.display)
        self.assertNotIn(f"dcim.cable:{existing.pk}", reused.identities)

    def test_an_operator_without_the_cable_permissions_is_blocked(self):
        """Planning refuses a write the actor could not make, with the permission named."""
        actor = user_with_object_permission(
            "cable-viewer",
            [
                (Site, ("view",), {}),
                (Device, ("view",), {}),
                (Interface, ("view",), {}),
                (FrontPort, ("view",), {}),
                (RearPort, ("view",), {}),
            ],
        )

        unit = self.unit(patched_path(), actor=actor)

        self.assertEqual(unit.disposition, Disposition.BLOCKED)
        denied = next(item for item in unit.diagnostics if item.code == "cable.permission_denied")
        self.assertEqual(denied.display["permission"], "dcim.add_cable")

    def test_an_operator_who_may_not_delete_the_logical_cable_is_blocked(self):
        """The replacement removes one cable, so planning refuses it without the delete right."""
        self.connect(self.eth0, self.eth1)
        actor = user_with_object_permission(
            "cable-adder",
            [
                (Site, ("view",), {}),
                (Device, ("view",), {}),
                (Interface, ("view",), {}),
                (FrontPort, ("view",), {}),
                (RearPort, ("view",), {}),
                (Cable, ("view", "add"), {}),
            ],
        )

        unit = self.unit(patched_path(), actor=actor)

        self.assertEqual(unit.disposition, Disposition.BLOCKED)
        denied = next(item for item in unit.diagnostics if item.code == "cable.permission_denied")
        self.assertEqual(denied.display["permission"], "dcim.delete_cable")

    def test_denied_deletion_does_not_disclose_an_invisible_logical_cable(self):
        """The delete-permission finding does not name a Logical Cable outside the actor's view scope."""
        logical = self.connect(self.eth0, self.eth1)
        actor = user_with_object_permission(
            "cable-hidden-deletion",
            [
                (Site, ("view",), {}),
                (Device, ("view",), {}),
                (Interface, ("view",), {}),
                (FrontPort, ("view",), {}),
                (RearPort, ("view",), {}),
                (Cable, ("add",), {}),
            ],
        )
        self.assertFalse(actor.has_perm("dcim.view_cable", logical))

        unit = self.unit(patched_path(), actor=actor)

        denied = next(item for item in unit.diagnostics if item.code == "cable.permission_denied")
        self.assertEqual(denied.display["permission"], "dcim.delete_cable")
        self.assertIs(denied.display["cable_visible"], False)
        self.assertNotIn("cable", denied.display)
        self.assertNotIn(f"dcim.cable:{logical.pk}", denied.identities)

    def test_an_invisible_mapped_peer_blocks_the_unit(self):
        """Planning cannot substitute a mapped peer outside the actor's FrontPort view scope."""
        actor = user_with_object_permission(
            "cable-hidden-peer",
            [
                (Site, ("view",), {}),
                (Device, ("view",), {}),
                (Interface, ("view",), {}),
                (FrontPort, ("view",), {"device": self.panel_2.pk}),
                (RearPort, ("view",), {}),
                (Cable, ("add",), {}),
            ],
        )

        unit = self.unit(same_rear_port_path(), actor=actor)

        self.assertEqual(unit.disposition, Disposition.BLOCKED)
        self.assertEqual(unit.changes, ())
        denied = next(item for item in unit.diagnostics if item.code == "cable.permission_denied")
        self.assertEqual(denied.display["permission"], "dcim.view_frontport")
        self.assertNotIn("cable.same_port_continuation", self.codes(unit))

    def test_an_invalid_source_trace_does_not_stop_the_batch(self):
        """A refused trace becomes its own invalid unit while the rest of the batch plans."""
        broken = (
            trace_endpoint_line(DEVICE_A),
            trace_endpoint_line(DEVICE_B),
            (trace_segment(DEVICE_A, "Patch", trace_termination("PANEL-1", "", "F1", "Bogus Class")),),
        )
        other_a = trace_termination("DEV-C", "", "eth0", "Port")
        other_b = trace_termination("DEV-D", "", "eth1", "NIC")
        for name, port in (("DEV-C", "eth0"), ("DEV-D", "eth1")):
            Interface.objects.create(device=self.make_device(name), name=port, type="1000base-t")

        plan = self.plan(broken, direct_path(other_a, other_b))

        dispositions = {unit.identity: unit.disposition for unit in plan.units}
        self.assertEqual(sorted(dispositions.values()), [Disposition.ACTIONABLE, Disposition.INVALID])

    def test_resolved_aliases_with_conflicting_cable_policy_invalidate_their_own_traces(self):
        """Two source names for one target segment cannot crash the complete batch preview."""
        alias_a = trace_termination("DEV-A", "", "source-port-a", "Port")
        alias_b = trace_termination("DEV-B", "", "source-port-b", "NIC")
        aliased = (
            trace_endpoint_line(alias_a),
            trace_endpoint_line(alias_b),
            (trace_segment(alias_a, "Patch", alias_b),),
        )
        conflicting = (
            trace_endpoint_line(DEVICE_A),
            trace_endpoint_line(DEVICE_B),
            (trace_segment(DEVICE_A, "Trunk", DEVICE_B),),
        )
        CableClassMapping.objects.filter(profile=self.profile, cable_class="Trunk").update(
            cable_type=None,
            cable_profile=None,
        )
        self.save_resolution(alias_a, self.eth0)
        self.save_resolution(alias_b, self.eth1)

        plan = self.plan(aliased, conflicting)

        self.assertEqual(
            [unit.disposition for unit in plan.units],
            [Disposition.INVALID, Disposition.INVALID],
        )
        for unit in plan.units:
            self.assertEqual(unit.changes, ())
            self.assertIn("cable.resolved_segment_conflict", self.codes(unit))

    def test_two_traces_claiming_one_free_termination_are_blocked_together(self):
        """Target-side occupancy refuses both source traces before either Cable can be created."""
        shared_a = trace_termination("DEV-A", "Card 1", "eth0", "Port")
        shared_b = trace_termination("DEV-A", "Card 2", "eth0", "Port")
        device_c = self.make_device("DEV-C")
        Interface.objects.create(device=device_c, name="eth2", type="1000base-t")
        endpoint_c = trace_termination("DEV-C", "", "eth2", "NIC")

        plan = self.plan(direct_path(shared_a, DEVICE_B), direct_path(shared_b, endpoint_c))

        self.assertEqual(
            [unit.disposition for unit in plan.units],
            [Disposition.BLOCKED, Disposition.BLOCKED],
        )
        expected_competitors = ["DEV-A Card 2 eth0 to DEV-C eth2", "DEV-A Card 1 eth0 to DEV-B eth1"]
        for unit, competing_trace in zip(plan.units, expected_competitors, strict=True):
            conflicts = [item for item in unit.diagnostics if item.code == "cable.planned_termination_conflict"]
            self.assertEqual(len(conflicts), 1)
            self.assertEqual(conflicts[0].display["termination"], str(self.eth0))
            self.assertEqual(conflicts[0].display["competing_trace"], competing_trace)
            self.assertEqual(unit.changes, ())
        self.assertEqual(Cable.objects.count(), 0)

    def test_a_segment_that_ends_on_its_own_termination_is_blocked(self):
        """NetBox refuses a Cable carrying one termination on both ends, so planning must catch it."""
        # Two references the source keeps apart: resolution matches device, name and kind, not Cards.
        same_port_other_cards = trace_termination("DEV-A", "SLOT-1", "eth0", "NIC")

        unit = self.unit(direct_path(DEVICE_A, same_port_other_cards))

        self.assertEqual(unit.disposition, Disposition.BLOCKED)
        self.assertIn("cable.segment_self_connection", self.codes(unit))
        self.assertEqual(unit.changes, ())

    def test_a_source_path_from_a_termination_to_itself_never_reaches_planning(self):
        """The Source Adapter refuses the identical-endpoint form before the Cable module sees it."""
        unit = self.unit(direct_path(DEVICE_A, DEVICE_A))

        self.assertEqual(unit.disposition, Disposition.INVALID)
        self.assertIn("trace.non_linear_path", self.codes(unit))
        self.assertEqual(unit.changes, ())

    def test_a_blocked_trace_still_contributes_its_policy_to_a_shared_segment(self):
        """Leaving it out let the other trace create the shared Cable with the losing policy."""
        alias_front = trace_termination("PANEL-2", "", "source-front", "Position Front")
        alias_b = trace_termination("DEV-B", "", "source-port-b", "NIC")
        # Two source names for one target segment: the adapter's own guard refuses one name twice.
        partner = (
            trace_endpoint_line(alias_front),
            trace_endpoint_line(alias_b),
            (trace_segment(alias_front, "Trunk", alias_b),),
        )
        # This trace blocks on its middle segment and still resolves a policy for the last one.
        blocked = (
            trace_endpoint_line(DEVICE_A),
            trace_endpoint_line(DEVICE_B),
            (
                trace_segment(DEVICE_A, "Patch", PANEL_1_FRONT),
                trace_segment(PANEL_1_REAR, "Bogus Class", PANEL_2_REAR),
                trace_segment(PANEL_2_FRONT, "Patch", DEVICE_B),
            ),
        )
        CableClassMapping.objects.filter(profile=self.profile, cable_class="Trunk").update(
            cable_type=None,
            cable_profile=None,
        )
        self.save_resolution(alias_front, self.panel_2_fronts[0])
        self.save_resolution(alias_b, self.eth1)

        plan = self.plan(partner, blocked)

        for unit in plan.units:
            self.assertIn("cable.resolved_segment_conflict", self.codes(unit))
            self.assertEqual(unit.changes, ())
        self.assertIn("cable.cableclass_unmapped", self.codes(plan.units[1]))

    def test_validation_sees_the_digest_the_constraint_carries(self):
        """`full_clean` checks the unique constraint on `trace_key`, so it cannot read an empty one."""
        cable = self.connect(self.eth0, self.eth1)
        identity = json.dumps([["dev-a", "", "eth0"], ["dev-b", "", "eth1"]], separators=(",", ":"))
        CableImportSource.objects.create(cable=cable, profile=self.profile, trace_identity=identity, segment_index=0)
        duplicate = CableImportSource(cable=cable, profile=self.profile, trace_identity=identity, segment_index=1)

        with self.assertRaises(ValidationError):
            duplicate.full_clean()

        self.assertEqual(duplicate.trace_key, index_digest(identity))

    def test_a_long_trace_identity_still_records_its_provenance(self):
        """The identity is unbounded source text, and a btree entry cannot exceed about 2704 bytes."""
        cable = self.connect(self.eth0, self.eth1)
        # A repeated character compresses away, so the port text has to be incompressible.
        port = secrets.token_hex(2000)
        identity = json.dumps([["dev-a", "", port], ["dev-b", "", "eth1"]], separators=(",", ":"))

        CableImportSource.objects.create(
            cable=cable,
            profile=self.profile,
            trace_identity=identity,
            segment_index=0,
        )

        stored = CableImportSource.objects.get(cable=cable, profile=self.profile)
        self.assertEqual(stored.trace_identity, identity)

    def save_resolution(self, reference, selected, role=TERMINATION_ROLE):
        """Store one manual termination decision for a Termination Reference."""
        device, cards, port, port_class = reference
        kind = claimed_termination_kind(port_class)
        TerminationResolution.objects.create(
            profile=self.profile,
            task_type=SELECT_TERMINATION_TASK,
            field_key=termination_field_key(device=device, cards=cards, port=port, kind=kind, role=role),
            selected_object_type=ObjectType.objects.get_for_model(selected),
            selected_object_id=selected.pk,
            selected_display_name=str(selected),
        )


class EligibleTerminationTest(CableTopologyMixin, TestCase):
    """The picker and a proposal request share one candidate query."""

    @classmethod
    def setUpTestData(cls):
        cls.build_topology()

    def test_candidates_are_the_claimed_kind_on_the_resolved_device(self):
        """A front-port question never offers an interface, and never leaves the device."""
        Interface.objects.create(device=self.panel_1, name="mgmt0", type="1000base-t")
        field_key = termination_field_key(device="PANEL-1", cards="", port="F1", kind="front_port")
        reader = NetBoxReader.for_actor(self.actor).for_target(site=self.site)

        result = eligible_terminations(field_key, reader, profile=self.profile)

        self.assertEqual(result.candidates, tuple(FrontPort.objects.filter(device=self.panel_1).order_by("name", "pk")))
        self.assertEqual(result.total, 1)

    def test_candidates_are_searched_counted_and_capped(self):
        """The result reports all search matches while returning only the requested first page."""
        for name in ("Eligible 3", "Eligible 1", "Eligible 2", "Excluded"):
            FrontPort.objects.create(device=self.panel_1, name=name, type="8p8c")
        field_key = termination_field_key(device="PANEL-1", cards="", port="F1", kind="front_port")
        reader = NetBoxReader.for_actor(self.actor).for_target(site=self.site)

        result = eligible_terminations(field_key, reader, profile=self.profile, search="ELIGIBLE", limit=2)

        self.assertEqual([candidate.name for candidate in result.candidates], ["Eligible 1", "Eligible 2"])
        self.assertEqual(result.total, 3)

    def test_mapped_peer_candidates_follow_port_mappings_in_both_directions(self):
        """A mapped-peer question offers the opposite ports instead of the source port's kind."""
        _panel, fronts, rear = self.rebuild_panel("PANEL-1", fronts=2)
        reader = NetBoxReader.for_actor(self.actor).for_target(site=self.site)
        cases = (
            (PANEL_1_REAR, "rear_port", tuple(fronts)),
            (PANEL_1_FRONT, "front_port", (rear,)),
        )

        for reference, kind, expected in cases:
            with self.subTest(kind=kind):
                device, cards, port, _port_class = reference
                field_key = termination_field_key(
                    device=device,
                    cards=cards,
                    port=port,
                    kind=kind,
                    role=MAPPED_PEER_ROLE,
                )

                result = eligible_terminations(field_key, reader, profile=self.profile)

                self.assertEqual(result.candidates, expected)
                self.assertEqual(result.total, len(expected))

    def test_mapped_peer_candidates_follow_a_manual_base_resolution(self):
        """A mapped-peer question starts from the visible RearPort selected for the base role."""
        _panel, fronts, rear = self.rebuild_panel("PANEL-1", fronts=2)
        source = trace_termination("PANEL-1", "Card A", "source-rear", "Punch-Down")
        TerminationResolution.objects.create(
            profile=self.profile,
            task_type=SELECT_TERMINATION_TASK,
            field_key=termination_field_key(
                device=source[0],
                cards=source[1],
                port=source[2],
                kind="rear_port",
                role=TERMINATION_ROLE,
            ),
            selected_object_type=ObjectType.objects.get_for_model(rear),
            selected_object_id=rear.pk,
            selected_display_name=str(rear),
        )
        field_key = termination_field_key(
            device=source[0],
            cards=source[1],
            port=source[2],
            kind="rear_port",
            role=MAPPED_PEER_ROLE,
        )
        actor = user_with_object_permission(
            "manual-base-mapped-peers",
            [
                (Device, ("view",), {}),
                (RearPort, ("view",), {}),
                (FrontPort, ("view",), {}),
            ],
        )
        reader = NetBoxReader.for_actor(actor).for_target(site=self.site)

        try:
            result = eligible_terminations(field_key, reader, profile=self.profile)
        except TypeError:
            self.fail("The eligible-termination interface has no Import Profile decision context.")

        self.assertEqual(result.candidates, tuple(fronts))
        self.assertEqual(result.total, 2)

    def test_candidates_stay_inside_the_actor_view_scope(self):
        """An actor who may not view the front ports is offered none of them."""
        actor = user_with_object_permission("cable-partial", [(Device, ("view",), {})])
        field_key = termination_field_key(device="PANEL-1", cards="", port="F1", kind="front_port")
        reader = NetBoxReader.for_actor(actor).for_target(site=self.site)

        result = eligible_terminations(field_key, reader, profile=self.profile)

        self.assertEqual(result.candidates, ())
        self.assertEqual(result.total, 0)

    def test_mapped_peer_candidates_stay_inside_the_actor_view_scope(self):
        """A mapped-peer question offers only the opposite ports the actor may view."""
        _panel, fronts, _rear = self.rebuild_panel("PANEL-1", fronts=2)
        actor = user_with_object_permission(
            "cable-mapped-peer-partial",
            [
                (Device, ("view",), {}),
                (FrontPort, ("view",), {"pk": fronts[1].pk}),
                (RearPort, ("view",), {}),
            ],
        )
        field_key = termination_field_key(
            device="PANEL-1",
            cards="",
            port="R1",
            kind="rear_port",
            role=MAPPED_PEER_ROLE,
        )
        reader = NetBoxReader.for_actor(actor).for_target(site=self.site)

        result = eligible_terminations(field_key, reader, profile=self.profile)

        self.assertEqual(result.candidates, (fronts[1],))
        self.assertEqual(result.total, 1)

    def test_an_unresolved_device_offers_no_candidate(self):
        """With no single Device there is no scope to list candidates from."""
        field_key = termination_field_key(device="DEV-GONE", cards="", port="F1", kind="front_port")
        reader = NetBoxReader.for_actor(self.actor).for_target(site=self.site)

        result = eligible_terminations(field_key, reader, profile=self.profile)

        self.assertEqual(result.candidates, ())
        self.assertEqual(result.total, 0)

    def test_a_key_that_is_not_a_termination_field_key_is_refused(self):
        """The seam validates its input rather than returning an empty list for a typo."""
        reader = NetBoxReader.for_actor(self.actor).for_target(site=self.site)

        with self.assertRaises(ValueError):
            eligible_terminations("device:source:7", reader, profile=self.profile)

    def test_a_noncanonical_json_key_is_refused(self):
        """A JSON object must contain every canonical termination field-key member."""
        reader = NetBoxReader.for_actor(self.actor).for_target(site=self.site)
        key = json.dumps({"device": "PANEL-1", "kind": "front_port"}, sort_keys=True, separators=(",", ":"))

        with self.assertRaises(ValueError):
            eligible_terminations(key, reader, profile=self.profile)

    def test_the_canonical_parser_refuses_an_unknown_termination_kind(self):
        """A five-member key is noncanonical when no candidate query exists for its kind."""
        key = json.dumps(
            {"cards": "", "device": "PANEL-1", "kind": "console_port", "port": "F1", "role": "termination"},
            sort_keys=True,
            separators=(",", ":"),
        )

        with self.assertRaises(ValueError):
            parse_termination_field_key(key)


class CablePreconditionRecheckTest(CableTopologyMixin, TestCase):
    """Execution rechecks its preconditions inside the transaction, against the real rows."""

    @classmethod
    def setUpTestData(cls):
        cls.build_topology()

    def context(self):
        """Return the execution context the coordinator would open the transaction with."""
        return ExecutionContext(
            actor=self.actor,
            reader=NetBoxReader.for_actor(self.actor).for_target(site=self.site),
            profile=self.profile,
        )

    def test_a_termination_cabled_after_planning_refuses_its_creation(self):
        """The write cannot land a second cable on a port, so it stops before NetBox validates."""
        unit = self.unit(patched_path())
        other = self.make_device("DEV-C")
        self.connect(self.panel_1_fronts[0], Interface.objects.create(device=other, name="eth9", type="1000base-t"))

        with self.assertRaises(PreconditionFailed):
            CableModule().apply(unit.changes[0], self.context())

        self.assertEqual(CableImportSource.objects.count(), 0)

    def test_a_re_terminated_logical_cable_refuses_its_deletion(self):
        """The one cable this import removes must still hold the terminations the plan recorded."""
        logical = self.connect(self.eth0, self.eth1)
        unit = self.unit(patched_path())
        moved = Interface.objects.create(device=self.make_device("DEV-C"), name="eth9", type="1000base-t")
        CableTermination.objects.filter(cable=logical, cable_end="B").update(termination_id=moved.pk)

        with self.assertRaises(PreconditionFailed):
            CableModule().apply(unit.changes[0], self.context())

        self.assertTrue(Cable.objects.filter(pk=logical.pk).exists())

    def test_a_logical_cable_that_is_gone_refuses_its_deletion(self):
        """A cable someone else already removed is a stale plan, not a silent success."""
        logical = self.connect(self.eth0, self.eth1)
        unit = self.unit(patched_path())
        logical.delete()

        with self.assertRaises(PreconditionFailed):
            CableModule().apply(unit.changes[0], self.context())

    def test_an_unknown_operation_is_refused(self):
        """A Planned Change this module did not write never reaches a NetBox row."""
        change = PlannedChange(
            identity="cable:update:1",
            target_module="cable",
            operation="update",
            payload={},
        )

        with self.assertRaises(PreconditionFailed):
            CableModule().apply(change, self.context())


class TraceWorkspaceDisplayTest(CableTopologyMixin, TestCase):
    """The workspace panels read the planner's own verdict; nothing recomputes the plan."""

    @classmethod
    def setUpTestData(cls):
        cls.build_topology()

    def workspace(self, unit):
        """Return the trace workspace payload one unit carries."""
        return unit.display["trace"]

    def test_every_proposed_segment_states_create_when_nothing_exists(self):
        """A path over empty terminations proposes one creation per stated segment."""
        workspace = self.workspace(self.unit(patched_path()))

        self.assertEqual(
            [(segment["index"], segment["status"]) for segment in workspace["segments"]],
            [(0, "create"), (1, "create"), (2, "create")],
        )

    def test_a_segment_an_existing_cable_proves_states_reuse_existing(self):
        """A Cable that already proves a segment is reused, so the panel must not say create."""
        self.connect(self.panel_1_rear, self.panel_2_rear)

        workspace = self.workspace(self.unit(patched_path()))

        statuses = {segment["index"]: segment["status"] for segment in workspace["segments"]}
        self.assertEqual(statuses[1], "reuse existing")
        self.assertEqual(statuses[0], "create")

    def test_a_segment_another_cable_holds_states_conflict(self):
        """A termination an unrelated Cable holds is a conflict, not a creation.

        The direct Cable between the two endpoints is this trace's Logical Cable, which the
        replacement deletes, so only a third party's Cable can conflict.
        """
        outsider = Interface.objects.create(device=self.make_device("DEV-C"), name="eth9", type="1000base-t")
        self.connect(self.panel_1_fronts[0], outsider)

        workspace = self.workspace(self.unit(patched_path()))

        statuses = {segment["index"]: segment["status"] for segment in workspace["segments"]}
        self.assertEqual(statuses[0], "conflict")

    def test_the_logical_cable_a_replacement_deletes_is_named(self):
        """The proposed panel states the one deletion a Patched Path Replacement performs."""
        self.connect(self.eth0, self.eth1)
        self.connect(self.panel_1_rear, self.panel_2_rear)
        CableClassMapping.objects.filter(profile=self.profile).update(cable_type="cat6")

        workspace = self.workspace(self.unit(patched_path()))

        self.assertIsNotNone(workspace["logical_cable"])
        self.assertTrue(workspace["logical_cable"]["visible"])

    def test_an_exact_name_match_states_automatically_resolved(self):
        """The operator has to see which terminations the exact-name rule matched without help."""
        workspace = self.workspace(self.unit(direct_path()))

        states = {item["label"]: item["state"] for item in workspace["terminations"]}
        self.assertEqual(states["DEV-A eth0"], "automatically resolved")

    def test_a_saved_decision_states_manually_resolved(self):
        """A termination an operator chose is distinct from one the exact-name rule matched."""
        renamed = trace_termination("DEV-A", "", "source-port", "Port")
        TerminationResolution.objects.create(
            profile=self.profile,
            task_type=SELECT_TERMINATION_TASK,
            field_key=termination_field_key(device="DEV-A", cards="", port="source-port", kind="interface"),
            selected_object_type=ObjectType.objects.get_for_model(Interface),
            selected_object_id=self.eth0.pk,
            selected_display_name=str(self.eth0),
        )

        workspace = self.workspace(self.unit(direct_path(from_end=renamed)))

        states = {item["label"]: item["state"] for item in workspace["terminations"]}
        self.assertEqual(states["DEV-A source-port"], "manually resolved")

    def test_an_unresolved_termination_carries_the_field_key_its_picker_needs(self):
        """The picker asks for candidates by canonical field key, so it must not rebuild one."""
        missing = trace_termination("DEV-A", "", "absent-port", "Port")

        workspace = self.workspace(self.unit(direct_path(from_end=missing)))

        unresolved = [item for item in workspace["terminations"] if item["state"] == "unresolved"]
        self.assertEqual([item["label"] for item in unresolved], ["DEV-A absent-port"])
        self.assertEqual(
            unresolved[0]["field_key"],
            termination_field_key(device="DEV-A", cards="", port="absent-port", kind="interface"),
        )
        self.assertEqual(unresolved[0]["kind"], "interface")

    def test_a_segment_two_traces_claim_with_different_policy_states_conflict(self):
        """A refused shared segment is a conflict the panel names, not an unplanned blank."""
        alias_a = trace_termination("DEV-A", "", "source-port-a", "Port")
        alias_b = trace_termination("DEV-B", "", "source-port-b", "NIC")
        aliased = (
            trace_endpoint_line(alias_a),
            trace_endpoint_line(alias_b),
            (trace_segment(alias_a, "Patch", alias_b),),
        )
        conflicting = (
            trace_endpoint_line(DEVICE_A),
            trace_endpoint_line(DEVICE_B),
            (trace_segment(DEVICE_A, "Trunk", DEVICE_B),),
        )
        CableClassMapping.objects.filter(profile=self.profile, cable_class="Trunk").update(
            cable_type=None, cable_profile=None
        )
        for reference, selected in ((alias_a, self.eth0), (alias_b, self.eth1)):
            device, cards, port, port_class = reference
            TerminationResolution.objects.create(
                profile=self.profile,
                task_type=SELECT_TERMINATION_TASK,
                field_key=termination_field_key(
                    device=device, cards=cards, port=port, kind=claimed_termination_kind(port_class)
                ),
                selected_object_type=ObjectType.objects.get_for_model(selected),
                selected_object_id=selected.pk,
                selected_display_name=str(selected),
            )

        plan = self.plan(aliased, conflicting)

        for unit in plan.units:
            statuses = [segment["status"] for segment in unit.display["trace"]["segments"]]
            self.assertEqual(statuses, ["conflict"], unit.identity)

    def test_a_trace_that_never_reached_classification_does_not_claim_an_absent_cable(self):
        """Planning that stopped before it read the topology reports unknown, not absence."""
        self.connect(self.eth0, self.eth1)
        blocked = (
            trace_endpoint_line(DEVICE_A),
            trace_endpoint_line(DEVICE_B),
            (
                trace_segment(DEVICE_A, "Patch", trace_termination("PANEL-1", "", "absent", "Position Front")),
                trace_segment(PANEL_1_REAR, "Trunk", PANEL_2_REAR),
                trace_segment(PANEL_2_FRONT, "Patch", DEVICE_B),
            ),
        )

        workspace = self.workspace(self.unit(blocked))

        self.assertFalse(workspace["topology_known"])
        self.assertIsNone(workspace["logical_cable"])

    def test_a_planned_trace_states_that_it_read_the_topology(self):
        """A trace that reached classification did look, so its panel may report what it found."""
        workspace = self.workspace(self.unit(patched_path()))

        self.assertTrue(workspace["topology_known"])

    def test_the_logical_cable_a_replacement_deletes_carries_what_a_reviewer_needs(self):
        """Section 6.3: the operator reviews the description and tags before the deletion runs."""
        from extras.models import Tag

        tag = Tag.objects.create(name="Trace Review Tag", slug="trace-review-tag")
        cable = self.connect(self.eth0, self.eth1, description="Temporary logical path")
        cable.tags.add(tag)
        self.connect(self.panel_1_rear, self.panel_2_rear)

        workspace = self.workspace(self.unit(patched_path()))

        self.assertEqual(workspace["logical_cable"]["description"], "Temporary logical path")
        self.assertEqual(list(workspace["logical_cable"]["tags"]), ["Trace Review Tag"])

    def test_an_ambiguous_mapped_peer_offers_its_own_picker(self):
        """A pass-through NetBox maps two ways is an operator decision, so the workspace asks it."""
        self.rebuild_panel("PANEL-1", fronts=2)

        unit = self.unit(same_rear_port_path())
        workspace = self.workspace(unit)

        self.assertIn("cable.ambiguous_mapped_peer", self.codes(unit))
        peer_key = termination_field_key(device="PANEL-1", cards="", port="R1", kind="rear_port", role=MAPPED_PEER_ROLE)
        peer = next(item for item in workspace["terminations"] if item["field_key"] == peer_key)
        self.assertEqual(peer["state"], "unresolved")
        self.assertTrue(peer["selectable"])
        # The picker offers the ports on the far side of the panel, which are front ports.
        self.assertEqual(peer["kind"], "front_port")

    def test_a_chosen_mapped_peer_states_that_an_operator_chose_it(self):
        """The badge separates a decision from a match here too."""
        _panel, fronts, rear = self.rebuild_panel("PANEL-1", fronts=2)
        TerminationResolution.objects.create(
            profile=self.profile,
            task_type=SELECT_TERMINATION_TASK,
            field_key=termination_field_key(
                device="PANEL-1", cards="", port="R1", kind="rear_port", role=MAPPED_PEER_ROLE
            ),
            selected_object_type=ObjectType.objects.get_for_model(FrontPort),
            selected_object_id=fronts[0].pk,
            selected_display_name=str(fronts[0]),
        )

        workspace = self.workspace(self.unit(same_rear_port_path()))

        peer_key = termination_field_key(device="PANEL-1", cards="", port="R1", kind="rear_port", role=MAPPED_PEER_ROLE)
        peer = next(item for item in workspace["terminations"] if item["field_key"] == peer_key)
        self.assertEqual(peer["state"], "manually resolved")
        self.assertEqual(peer["selected"], str(fronts[0]))
        self.assertEqual(rear.name, "R1")

    def test_the_source_evidence_states_both_endpoints_and_every_segment_in_order(self):
        """The source panel restates what the workbook said, in the order it said it."""
        workspace = self.workspace(self.unit(patched_path()))

        self.assertEqual(workspace["endpoints"]["from"], "DEV-A eth0")
        self.assertEqual(workspace["endpoints"]["to"], "DEV-B eth1")
        self.assertEqual(
            [
                (segment["source_left"], segment["cable_class"], segment["source_right"])
                for segment in workspace["segments"]
            ],
            [
                ("DEV-A eth0", "Patch", "PANEL-1 F1"),
                ("PANEL-1 R1", "Trunk", "PANEL-2 R1"),
                ("PANEL-2 F1", "Patch", "DEV-B eth1"),
            ],
        )

    def test_the_proposed_panel_names_the_netbox_objects_the_segments_join(self):
        """The proposed panel shows NetBox's own names, which the source words need not match."""
        workspace = self.workspace(self.unit(patched_path()))

        self.assertEqual(
            [(segment["left"], segment["right"]) for segment in workspace["segments"]],
            [("eth0", "F1"), ("R1", "R1"), ("F1", "eth1")],
        )
        self.assertEqual([segment["substituted"] for segment in workspace["segments"]], [False, False, False])

    def test_every_segment_that_re_enters_a_panel_states_its_implied_claim(self):
        """Section 10.2: the source panel shows the ordered evidence with its Pass-Through Claims."""
        workspace = self.workspace(self.unit(patched_path()))

        self.assertEqual(
            [segment["pass_through"] for segment in workspace["segments"]],
            [False, True, True],
        )

    def test_a_pass_through_claim_names_the_port_planning_entered_through(self):
        """A claim planning had to substitute names the port, because the source never stated it."""
        workspace = self.workspace(self.unit(same_rear_port_path()))

        claims = [segment for segment in workspace["segments"] if segment["pass_through"]]
        self.assertEqual([segment["index"] for segment in claims], [1, 2])
        substituted = claims[0]
        self.assertEqual(substituted["source_left"], "PANEL-1 R1")
        # The source re-entered the rear port it left, so planning claimed its mapped front port.
        self.assertEqual(substituted["left"], "F1")
        self.assertTrue(substituted["substituted"])
        self.assertFalse(claims[1]["substituted"])


class TraceWizardRenderTest(CableTopologyMixin, TestCase):
    """A trace profile is selectable now, so the existing wizard has to survive one."""

    @classmethod
    def setUpTestData(cls):
        cls.build_topology()

    def test_the_wizard_uploads_a_trace_workbook_and_renders_its_preview(self):
        """The review workspace is generic until T6, and it must render a Cable unit today."""
        response = self._upload()

        self.assertContains(response, "DEV-A eth0 to DEV-B eth1")

    def test_a_trace_profile_falls_back_to_the_row_view(self):
        """Only the flat adapter declares a stored view mode, and reading it answered 500."""
        response = self._upload()

        self.assertEqual(response.context["view_mode"], "rows")

    def test_the_view_query_parameter_overrides_the_fallback(self):
        """The Rack view link has to keep working for a profile that declares no mode."""
        self._upload()

        response = self.client.get(
            reverse("plugins:netbox_data_import:import_preview"),
            {"view": "racks"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["view_mode"], "racks")

    def _upload(self):
        """Drive the wizard to a rendered preview for the trace profile."""
        self.client.force_login(self.actor)
        upload = BytesIO(trace_workbook_bytes(path_blocks=(patched_path(),)))
        upload.name = "traces.xlsx"
        response = self.client.post(
            reverse("plugins:netbox_data_import:import_setup"),
            {"profile": self.profile.pk, "site": self.site.pk, "excel_file": upload},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        return response


class CableExecutionTest(CableTopologyMixin, TransactionTestCase):
    """Patched Path Replacement end to end, asserted against the real database."""

    def setUp(self):
        self.build_topology()

    def execute(self, plan, identities=None):
        """Run one selection through the public coordinator seam."""
        return ImportEngine.execute(
            self.profile,
            self.document,
            plan.to_dict(),
            identities or [unit.identity for unit in plan.units if unit.disposition == Disposition.ACTIONABLE],
            str(uuid.uuid4()),
            self.actor,
        )

    def assert_competing_write_is_blocked(self, plan, competing_write, signal):
        """Assert that one write cannot move a row after the execution replan reads it."""
        with competing_write_during(signal, Cable, competing_write) as (observed, blocked):
            self.execute(plan)

        self.assertTrue(observed, "the execution reached no Cable write")
        self.assertEqual(blocked, [True], "the competing write did not wait for the execution")

    def test_one_execution_replaces_the_path_and_writes_provenance(self):
        """The logical cable goes, every segment arrives, and each created Cable earns one row."""
        logical = self.connect(self.eth0, self.eth1)
        plan = self.plan(patched_path())

        execution = self.execute(plan)

        self.assertEqual(execution.outcome, "succeeded")
        self.assertFalse(Cable.objects.filter(pk=logical.pk).exists())
        self.assertEqual(Cable.objects.count(), 3)
        self.assertEqual(CableImportSource.objects.count(), 3)
        self.assertEqual(
            sorted(CableImportSource.objects.values_list("segment_index", flat=True)),
            [0, 1, 2],
        )
        self.assertEqual({cable.status for cable in Cable.objects.all()}, {"connected"})
        self.assertEqual({cable.type for cable in Cable.objects.all()}, {"cat6"})

    def test_reverse_import_keeps_creation_and_provenance_positions_canonical(self):
        """Opposite workbook direction preserves each physical segment's plan and stored index."""
        forward = self.plan(patched_path())
        forward_order = [
            self.termination_pairs(change) for change in forward.units[0].changes if change.operation == "create"
        ]
        self.execute(forward)
        forward_positions = self.provenance_positions()
        self.assertEqual({source.direction for source in CableImportSource.objects.all()}, {"canonical"})
        forward_rows = list(CableImportSource.objects.order_by("pk").values())

        existing_reverse = self.plan(reversed_patched_path())
        self.assertEqual(existing_reverse.units[0].disposition, Disposition.NO_OP)
        self.assertEqual(list(CableImportSource.objects.order_by("pk").values()), forward_rows)
        Cable.objects.all().delete()

        reverse = self.plan(reversed_patched_path())
        reverse_order = [
            self.termination_pairs(change) for change in reverse.units[0].changes if change.operation == "create"
        ]
        self.assertEqual(reverse_order, forward_order)
        self.execute(reverse)

        self.assertEqual(self.provenance_positions(), forward_positions)
        self.assertEqual({source.direction for source in CableImportSource.objects.all()}, {"reversed"})

    def test_long_endpoint_evidence_is_stored_intact(self):
        """A valid From line is stored without truncation or a database failure."""
        from django.db import DataError

        long_endpoint = trace_termination("DEV-A", f"CARD-{'X' * 600}", "eth0", "Port")
        from_text = trace_endpoint_line(long_endpoint)
        plan = self.plan(direct_path(from_end=long_endpoint))

        try:
            execution = self.execute(plan)
        except DataError:
            self.fail("The public execution seam raised DataError for valid endpoint evidence.")

        source = CableImportSource.objects.get()
        self.assertEqual(execution.outcome, "succeeded")
        self.assertGreater(len(from_text), 500)
        self.assertEqual(source.from_text, from_text)

    def test_the_removed_logical_cable_is_recorded_only_in_the_execution_snapshot(self):
        """Provenance rows never record a deletion, so the audit row is where it lives."""
        logical = self.connect(self.eth0, self.eth1)
        plan = self.plan(patched_path())

        execution = self.execute(plan)

        deleted = execution.applied_changes["deleted"]
        self.assertEqual([item["object_id"] for item in deleted], [logical.pk])
        self.assertEqual(deleted[0]["object_type"], "dcim.cable")
        self.assertFalse(CableImportSource.objects.filter(cable_id=logical.pk).exists())

    def test_two_traces_sharing_one_segment_create_one_cable_and_two_rows(self):
        """ADR 0001 identity sharing writes the trunk once and credits both Source Traces."""
        _panel_1, _fronts_1, panel_1_rear = self.rebuild_panel("PANEL-1", fronts=2)
        self.rebuild_panel("PANEL-2", fronts=2)
        Interface.objects.create(device=self.make_device("DEV-C"), name="eth0", type="1000base-t")
        Interface.objects.create(device=self.make_device("DEV-D"), name="eth1", type="1000base-t")
        device_c = trace_termination("DEV-C", "", "eth0", "Port")
        device_d = trace_termination("DEV-D", "", "eth1", "NIC")
        second_in = trace_termination("PANEL-1", "", "F2", "Position Front")
        second_out = trace_termination("PANEL-2", "", "F2", "Position Front")
        first = patched_path()
        second = (
            trace_endpoint_line(device_c),
            trace_endpoint_line(device_d),
            (
                trace_segment(device_c, "Patch", second_in),
                trace_segment(PANEL_1_REAR, "Trunk", PANEL_2_REAR),
                trace_segment(second_out, "Patch", device_d),
            ),
        )
        plan = self.plan(first, second)
        self.assertEqual(
            [unit.disposition for unit in plan.units],
            [Disposition.ACTIONABLE, Disposition.ACTIONABLE],
            [unit.diagnostics for unit in plan.units],
        )

        self.execute(plan)

        trunk = Cable.objects.get(
            terminations__termination_id=panel_1_rear.pk,
            terminations__termination_type=ObjectType.objects.get_for_model(RearPort),
        )
        rows = CableImportSource.objects.filter(cable=trunk)
        self.assertEqual(rows.count(), 2)
        self.assertEqual(len({row.trace_identity for row in rows}), 2)

    def test_a_blocked_trace_contributes_provenance_to_a_shared_segment(self):
        """A later policy decision cannot strand a trace without its shared Cable provenance."""
        _panel_1, _fronts_1, panel_1_rear = self.rebuild_panel("PANEL-1", fronts=2)
        self.rebuild_panel("PANEL-2", fronts=2)
        Interface.objects.create(device=self.make_device("DEV-C"), name="eth0", type="1000base-t")
        Interface.objects.create(device=self.make_device("DEV-D"), name="eth1", type="1000base-t")
        device_c = trace_termination("DEV-C", "", "eth0", "Port")
        device_d = trace_termination("DEV-D", "", "eth1", "NIC")
        second_in = trace_termination("PANEL-1", "", "F2", "Position Front")
        second_out = trace_termination("PANEL-2", "", "F2", "Position Front")
        first = patched_path()
        blocked = (
            trace_endpoint_line(device_c),
            trace_endpoint_line(device_d),
            (
                trace_segment(device_c, "Unmapped", second_in),
                trace_segment(PANEL_1_REAR, "Trunk", PANEL_2_REAR),
                trace_segment(second_out, "Patch", device_d),
            ),
        )
        plan = self.plan(first, blocked)
        self.assertEqual(
            [unit.disposition for unit in plan.units],
            [Disposition.ACTIONABLE, Disposition.BLOCKED],
        )

        self.execute(plan, [plan.units[0].identity])
        CableClassMapping.objects.create(
            profile=self.profile,
            cable_class="Unmapped",
            cable_type_resolved=True,
            cable_type="cat6",
            cable_profile_resolved=True,
            cable_profile="single-1c1p",
        )
        repaired = ImportEngine.plan(
            self.profile,
            self.document,
            self.actor,
            self.planning_context,
        )
        self.assertEqual(repaired.units[1].disposition, Disposition.ACTIONABLE)
        self.execute(repaired, [repaired.units[1].identity])

        trunk = Cable.objects.get(
            terminations__termination_id=panel_1_rear.pk,
            terminations__termination_type=ObjectType.objects.get_for_model(RearPort),
        )
        rows = CableImportSource.objects.filter(cable=trunk)
        self.assertEqual(rows.count(), 2)
        self.assertEqual(len({row.trace_identity for row in rows}), 2)

    def test_target_state_that_moved_after_preview_rolls_back_the_complete_selection(self):
        """The replan runs inside the transaction, so a stale unit takes the whole write with it."""
        from netbox_data_import.import_engine import StalePlan

        plan = self.plan(patched_path())
        other = self.make_device("DEV-C")
        self.connect(self.panel_2_fronts[0], Interface.objects.create(device=other, name="eth9", type="1000base-t"))
        before = Cable.objects.count()

        with self.assertRaises((PreconditionFailed, StalePlan)):
            self.execute(plan)

        self.assertEqual(Cable.objects.count(), before)
        self.assertEqual(CableImportSource.objects.count(), 0)

    def test_a_termination_moved_after_replan_refuses_the_accepted_execution(self):
        """The locked write recheck rejects an Interface that left its resolved Device."""
        from django.db import connection

        accepted = self.plan(patched_path())
        other = self.make_device("DEV-C")
        moved = []
        interface_table = Interface._meta.db_table

        def move_before_termination_lock(execute, sql, params, many, context):
            if moved or f'FROM "{interface_table}"' not in sql or "FOR UPDATE" not in sql.upper():
                return execute(sql, params, many, context)
            moved.append(True)

            with run_on_separate_connection(lambda: Interface.objects.filter(pk=self.eth0.pk).update(device=other)):
                pass
            return execute(sql, params, many, context)

        with connection.execute_wrapper(move_before_termination_lock):
            with self.assertRaises(PreconditionFailed):
                self.execute(accepted)

        self.eth0.refresh_from_db()
        self.assertEqual(self.eth0.device_id, other.pk)
        self.assertEqual(Cable.objects.count(), 0)
        self.assertEqual(CableImportSource.objects.count(), 0)

    def test_concurrent_multi_change_imports_do_not_deadlock_on_termination_locks(self):
        """Concurrent imports acquire their shared termination rows in one global order."""
        from queue import Queue
        from threading import Barrier, BrokenBarrierError, Lock, Thread, get_ident

        from django.db import connections
        from django.db.models.signals import pre_save

        from netbox_data_import.import_engine import StalePlan

        device_c = self.make_device("DEV-C")
        device_d = self.make_device("DEV-D")
        Interface.objects.create(device=device_c, name="eth0", type="1000base-t")
        Interface.objects.create(device=device_d, name="eth1", type="1000base-t")
        device_c_end = trace_termination("DEV-C", "", "eth0", "Port")
        device_d_end = trace_termination("DEV-D", "", "eth1", "NIC")
        first_segment = direct_path()
        second_segment = direct_path(device_c_end, device_d_end)
        other_profile = ImportProfile.objects.create(
            name="Concurrent Cable Traces",
            source_adapter="trace_workbook",
            adapter_config={},
        )
        CableClassMapping.objects.create(
            profile=other_profile,
            cable_class="Patch",
            cable_type_resolved=True,
            cable_type="cat6",
            cable_profile_resolved=True,
            cable_profile="single-1c1p",
        )
        first_document = SourceDocument.store(
            profile=self.profile,
            content=trace_workbook_bytes(path_blocks=(first_segment, second_segment)),
        )
        second_document = SourceDocument.store(
            profile=other_profile,
            content=trace_workbook_bytes(path_blocks=(second_segment, first_segment)),
        )
        first_plan = ImportEngine.plan(self.profile, first_document, self.actor, self.planning_context)
        second_plan = ImportEngine.plan(other_profile, second_document, self.actor, self.planning_context)
        start = Barrier(3)
        first_writes = Barrier(2)
        observed_threads = set()
        observed_lock = Lock()
        outcomes = Queue()

        def synchronize_first_writes(sender, instance, **kwargs):
            thread_id = get_ident()
            with observed_lock:
                if thread_id in observed_threads:
                    return
                observed_threads.add(thread_id)
            try:
                first_writes.wait(timeout=2)
            except BrokenBarrierError:
                pass

        def execute_plan(profile, document, plan):
            connections["default"].close()
            try:
                start.wait(timeout=10)
                execution = ImportEngine.execute(
                    profile,
                    document,
                    plan.to_dict(),
                    [unit.identity for unit in plan.units],
                    str(uuid.uuid4()),
                    self.actor,
                )
            except BaseException as exc:
                outcomes.put(exc)
            else:
                outcomes.put(execution)
            finally:
                connections["default"].close()

        pre_save.connect(synchronize_first_writes, sender=Cable, weak=False)
        workers = (
            Thread(target=execute_plan, args=(self.profile, first_document, first_plan), daemon=True),
            Thread(target=execute_plan, args=(other_profile, second_document, second_plan), daemon=True),
        )
        try:
            for worker in workers:
                worker.start()
            start.wait(timeout=10)
            for worker in workers:
                worker.join(timeout=20)
        finally:
            pre_save.disconnect(synchronize_first_writes, sender=Cable)

        self.assertTrue(all(not worker.is_alive() for worker in workers), "a concurrent import did not finish")
        results = [outcomes.get_nowait() for _worker in workers]
        errors = [result for result in results if isinstance(result, BaseException)]
        self.assertTrue(all(isinstance(error, (StalePlan, PreconditionFailed)) for error in errors), errors)
        self.assertEqual(len(errors), 1)
        self.assertEqual(Cable.objects.count(), 2)

    def test_hidden_reused_cable_drift_makes_the_accepted_unit_stale(self):
        """A redacted drift diagnostic still invalidates an accepted actionable unit."""
        from netbox_data_import.import_engine import StalePlan

        trunk = self.connect(self.panel_1_rear, self.panel_2_rear)
        actor = user_with_object_permission(
            "cable-hidden-stale-drift",
            [
                (Site, ("view",), {}),
                (Device, ("view",), {}),
                (Interface, ("view",), {}),
                (FrontPort, ("view",), {}),
                (RearPort, ("view",), {}),
                (Cable, ("add",), {}),
            ],
        )
        self.assertFalse(actor.has_perm("dcim.view_cable", trunk))
        accepted = self.plan(patched_path(), actor=actor)
        Cable.objects.filter(pk=trunk.pk).update(type="cat5e")

        with self.assertRaises(StalePlan):
            ImportEngine.execute(
                self.profile,
                self.document,
                accepted.to_dict(),
                [accepted.units[0].identity],
                str(uuid.uuid4()),
                actor,
            )

        self.assertEqual(Cable.objects.count(), 1)
        self.assertEqual(CableImportSource.objects.count(), 0)

    def test_execution_holds_a_reused_cable_through_the_remaining_writes(self):
        """A trunk proven during replan cannot disappear before both patch segments are written."""
        from django.db.models.signals import pre_save

        trunk = self.connect(self.panel_1_rear, self.panel_2_rear)
        plan = self.plan(patched_path())

        self.assert_competing_write_is_blocked(
            plan,
            lambda: Cable.objects.filter(pk=trunk.pk).delete(),
            pre_save,
        )

        self.assertTrue(Cable.objects.filter(pk=trunk.pk).exists())
        self.assertEqual(Cable.objects.count(), 3)

    def test_execution_holds_the_termination_rows_that_prove_a_reused_cable(self):
        """The replan proves the trunk from its termination rows, so those cannot move under it."""
        from django.db.models.signals import pre_save

        trunk = self.connect(self.panel_1_rear, self.panel_2_rear)
        termination = CableTermination.objects.get(cable=trunk, cable_end="B")
        _panel_3, _fronts, spare_rear = self.make_panel("PANEL-3")
        plan = self.plan(patched_path())

        self.assert_competing_write_is_blocked(
            plan,
            lambda: CableTermination.objects.filter(pk=termination.pk).update(termination_id=spare_rear.pk),
            pre_save,
        )

        termination.refresh_from_db()
        self.assertEqual(termination.termination_id, self.panel_2_rear.pk)

    def test_execution_holds_a_relied_on_port_mapping_through_the_cable_writes(self):
        """A panel mapping proven during replan cannot disappear before its path is written."""
        from django.db.models.signals import pre_save

        mapping = PortMapping.objects.get(
            front_port=self.panel_1_fronts[0],
            rear_port=self.panel_1_rear,
        )
        plan = self.plan(same_rear_port_path())

        self.assert_competing_write_is_blocked(
            plan,
            lambda: PortMapping.objects.filter(pk=mapping.pk).delete(),
            pre_save,
        )

        self.assertTrue(PortMapping.objects.filter(pk=mapping.pk).exists())
        self.assertEqual(Cable.objects.count(), 3)

    def test_the_deleted_object_snapshot_records_what_the_cable_carried(self):
        """The audit row is the only record left of a Logical Cable, so it keeps its metadata."""
        logical = self.connect(self.eth0, self.eth1, description="Temporary logical path")
        later = Tag.objects.create(name="Later", slug="later")
        earlier = Tag.objects.create(name="Earlier", slug="earlier")
        logical.tags.add(later, earlier)

        execution = self.execute(self.plan(patched_path()))

        deleted = execution.applied_changes["deleted"]
        self.assertEqual(len(deleted), 1, deleted)
        self.assertEqual(deleted[0]["detail"]["description"], "Temporary logical path")
        self.assertEqual(deleted[0]["detail"]["tags"], ["Earlier", "Later"])

    def test_deletion_holds_its_termination_rows_through_the_snapshot(self):
        """A Logical Cable termination cannot move after deletion records its reviewed state."""
        from django.db.models.signals import pre_delete

        logical = self.connect(self.eth0, self.eth1)
        plan = self.plan(patched_path())
        termination = CableTermination.objects.get(cable=logical, cable_end="B")
        moved = Interface.objects.create(device=self.make_device("DEV-C"), name="eth9", type="1000base-t")

        self.assert_competing_write_is_blocked(
            plan,
            lambda: CableTermination.objects.filter(pk=termination.pk).update(termination_id=moved.pk),
            pre_delete,
        )

        self.assertFalse(Cable.objects.filter(pk=logical.pk).exists())
