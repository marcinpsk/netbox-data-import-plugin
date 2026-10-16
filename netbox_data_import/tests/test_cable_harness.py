# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Plan and write breakout harnesses from leg traces through the real adapter, engine and database."""

import uuid
from io import BytesIO
from unittest import skipUnless
from unittest.mock import patch

from dcim.models import (
    Cable,
    CableTermination,
    Device,
    FrontPort,
    Interface,
    PortMapping,
    RearPort,
    Site,
    VirtualChassis,
)
from django.db import connection, transaction
from django.db.models.signals import pre_save
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from netbox_data_import.cable_disclosure import FINDING_HIDDEN, TERMINATION_HIDDEN, WORDING_SOURCES, present_units
from netbox_data_import.cable_harness import IntentRefused, harness_intent, ordinary_intent
from netbox_data_import.cable_policy import cable_profile_label
from netbox_data_import.cable_target import CableModule, _CableBatch
from netbox_data_import.import_engine import ImportEngine, SelectionError, StalePlan
from netbox_data_import.models import (
    CableClassMapping,
    CableImportSource,
    CableSegmentOverride,
    ImportExecution,
    ImportProfile,
)
from netbox_data_import.plan import Disposition, PlannedChange, Severity, fingerprint_of
from netbox_data_import.review_workspace import (
    _DIAGNOSTIC_MESSAGES,
    BREAKOUT_PROFILE,
    SYNC_COMPANIONS,
    SYNC_TOGETHER,
    ReviewWorkspace,
)
from netbox_data_import.target_runtime import ExecutionContext, PreconditionFailed
from netbox_data_import.views import RETAINED_SEGMENT_REASON, SEGMENT_OUT_OF_VIEW
from netbox_data_import.tests.helpers import (
    cables_on,
    competing_write_during,
    preview_claim,
    run_on_separate_connection,
    trace_endpoint_line,
    trace_segment,
    trace_termination,
    trace_workbook_bytes,
    upload_preview,
    user_with_object_permission,
)
from netbox_data_import.tests.test_cable_module import DEVICE_A, CableTopologyMixin, direct_path, patched_path


def _channel_fields() -> bool:
    return {"channels", "channel_id"} <= {field.name for field in Interface._meta.get_fields()}


CHANNELS = skipUnless(_channel_fields(), "NetBox 4.6 Interface has no channel_id field")
FIBER = "Lane Fiber"
CARDS = "Slot 0 >> 0/0 (10)"
BREAKOUT_4 = "breakout-1c4p-4c1p"
HARNESS_PREFIX = "cable:create:harness:"


def lane(number, *, device="SW-1", cards=CARDS, port=None):
    """Return the source reference of one breakout lane, P01 for channel 1."""
    return trace_termination(device, cards, port or f"P{number:02d}", "Port")


def panel_front(number):
    """Return the source reference of one front port of the harness panel."""
    return trace_termination("PANEL-H", "", f"F{number}", "Position Front")


def panel_rear(number):
    """Return the source reference of the rear port behind one front port of the harness panel."""
    return trace_termination("PANEL-H", "", f"R{number}", "Punch-Down")


def server_port(number):
    """Return the source reference of the one port of one server."""
    return trace_termination(f"SRV-{number}", "", "eth0", "NIC")


def panel_leg(number, start=None, downstream=FIBER):
    """Return the leg trace that leaves one lane on panel front port *number* and reaches its server."""
    start = start or lane(number)
    return (
        trace_endpoint_line(start),
        trace_endpoint_line(server_port(number)),
        (
            trace_segment(start, FIBER, panel_front(number)),
            trace_segment(panel_rear(number), downstream, server_port(number)),
        ),
    )


def direct_leg(number, start=None):
    """Return the leg trace that leaves one lane straight to the port of server *number*."""
    start = start or lane(number)
    return (
        trace_endpoint_line(start),
        trace_endpoint_line(server_port(number)),
        (trace_segment(start, FIBER, server_port(number)),),
    )


def uplink():
    """Return the source reference that names the channelized parent itself, under another cards label."""
    return trace_termination("SW-1", "Uplinks", "0/0", "Port")


def uplink_trace():
    """Return the ordinary trace from the parent itself through panel port 1 to server 1."""
    return (
        trace_endpoint_line(uplink()),
        trace_endpoint_line(server_port(1)),
        (trace_segment(uplink(), FIBER, panel_front(1)), trace_segment(panel_rear(1), FIBER, server_port(1))),
    )


def termination_snapshot(cable):
    """Return every CableTermination row of one Cable as side, connector, positions, model and object id."""
    return sorted(
        (row.cable_end, row.connector, list(row.positions or ()), row.termination_type.model, row.termination_id)
        for row in CableTermination.objects.filter(cable=cable).select_related("termination_type")
    )


# Leg-segment findings that state no harness fact: CableClass policy, and the hidden-parent permission refusal.
NO_HARNESS_WORDING = frozenset({"cable.permission_denied", "cable.cableclass_unmapped", "cable.policy_stale"})


def wording_keys(display) -> set:
    """Return the rows one display's wording sources name."""
    return {(source["kind"], source["pk"]) for source in display.get(WORDING_SOURCES) or ()}


def assert_harness_disclosure(test, plan) -> None:
    """Assert that every finding about a harness carries the whole harness as its wording sources (section 6.6).

    The harness set is the one its `cable.harness_state` carries. A finding is about the harness when its code is a
    harness code, or when a leg unit holds it on the leg's own segment.
    """
    harnesses, legs = {}, {}
    for unit in plan.units:
        for item in unit.diagnostics:
            if item.code == "cable.harness_state":
                harnesses[item.identities[0]] = wording_keys(item.display)
                legs[unit.identity] = (item.display["segment_index"], item.identities[0])
    for unit in plan.units:
        leg = legs.get(unit.identity)
        for item in unit.diagnostics:
            if item.code in NO_HARNESS_WORDING:
                continue
            if item.code.startswith("cable.harness_"):
                parent = item.identities[0]
            elif leg is not None and item.display.get("segment_index") == leg[0]:
                parent = leg[1]
            else:
                continue
            # A harness the planner settled no leg of carries no set to compare with.
            if parent in harnesses:
                test.assertLessEqual(harnesses[parent], wording_keys(item.display), f"{unit.identity}: {item.code}")


class HarnessTopologyMixin(CableTopologyMixin):
    """Add one channelized switch port, a four-port panel and four servers to the cable topology."""

    def plan(self, *blocks, actor=None):
        """Plan through the public seam, and check the harness disclosure invariant on every plan a test builds."""
        plan = super().plan(*blocks, actor=actor)
        assert_harness_disclosure(self, plan)
        return plan

    @classmethod
    def build_harness_topology(cls, channels=4):
        """Create the parent Interface with one child per channel, and the far ends the legs reach."""
        cls.build_topology()
        CableClassMapping.objects.create(
            profile=cls.profile,
            cable_class=FIBER,
            cable_type_resolved=True,
            cable_type="mmf-om4",
            cable_profile_resolved=True,
            cable_profile="single-1c1p",
        )
        cls.switch = cls.make_device("SW-1")
        cls.parent = cls.make_parent(cls.switch, "0/0", channels)
        cls.children = {number: cls.make_child(cls.parent, number) for number in range(1, channels + 1)}
        # A plain subinterface under the parent is part of its child set and is no channel.
        Interface.objects.create(device=cls.switch, name=f"{cls.parent.name}.100", type="virtual", parent=cls.parent)
        panel = cls.make_device("PANEL-H")
        cls.fronts, cls.rears = {}, {}
        for number in range(1, 5):
            front = FrontPort.objects.create(device=panel, name=f"F{number}", type="lc")
            rear = RearPort.objects.create(device=panel, name=f"R{number}", type="lc", positions=1)
            PortMapping.objects.create(front_port=front, rear_port=rear, front_port_position=1, rear_port_position=1)
            cls.fronts[number], cls.rears[number] = front, rear
        cls.servers = {
            number: Interface.objects.create(
                device=cls.make_device(f"SRV-{number}"), name="eth0", type="25gbase-x-sfp28"
            )
            for number in range(1, 5)
        }

    @staticmethod
    def make_parent(device, name, channels):
        """Create one channelized Interface through NetBox validation."""
        parent = Interface(device=device, name=name, type="100gbase-x-qsfp28", channels=channels)
        parent.full_clean()
        parent.save()
        return parent

    @staticmethod
    def make_child(parent, channel):
        """Create the channel subinterface for one channel of *parent* through NetBox validation."""
        child = Interface(
            device=parent.device,
            name=f"{parent.name}:{channel}",
            type="25gbase-x-sfp28",
            parent=parent,
            channel_id=channel,
        )
        child.full_clean()
        child.save()
        return child

    def join_chassis(self, serial="MEMBER-B"):
        """Make SW-1 the first member of a virtual chassis and return its second member, SW-1-B."""
        chassis = VirtualChassis.objects.create(name="VC-1")
        member = Device.objects.create(
            name="SW-1-B", site=self.site, device_type=self.device_type, role=self.role, serial=serial
        )
        Device.objects.filter(pk=self.switch.pk).update(virtual_chassis=chassis, vc_position=1)
        Device.objects.filter(pk=member.pk).update(virtual_chassis=chassis, vc_position=2)
        return Device.objects.get(pk=member.pk)

    def build_chassis_harness(self, serial="MEMBER-B"):
        """Put a two-channel parent on chassis member SW-1-B and its children on SW-1, which NetBox permits."""
        member = self.join_chassis(serial)
        parent = self.make_parent(member, "1/1", 2)
        switch = Device.objects.get(pk=self.switch.pk)
        lanes = []
        for number in (1, 2):
            child = Interface(
                device=switch, name=f"1/1:{number}", type="25gbase-x-sfp28", parent=parent, channel_id=number
            )
            child.full_clean()
            child.save()
            lanes.append(lane(number, cards="Breakout VC", port=f"V{number}"))
            self.save_resolution(lanes[-1], child)
        return member, [direct_leg(number, start=reference) for number, reference in enumerate(lanes, start=1)]

    def resolve_lanes(self, numbers, **reference):
        """Save the operator's choice of the channel child for each given lane."""
        for number in numbers:
            self.save_resolution(lane(number, **reference), self.children[number])

    def build_existing_harness(self, legs=(1, 2, 3, 4)):
        """Cable the parent to the given panel front ports the way NetBox writes a breakout Cable."""
        cable = Cable(
            profile=BREAKOUT_4,
            type="mmf-om4",
            status="connected",
            a_terminations=[self.parent],
            b_terminations=[self.fronts[number] for number in legs],
        )
        cable.full_clean()
        cable.save()
        return cable

    @staticmethod
    def harness_changes(unit):
        """Return the harness creations one unit carries."""
        return [change for change in unit.changes if change.identity.startswith(HARNESS_PREFIX)]

    @staticmethod
    def state(unit):
        """Return the one harness state finding of a leg unit."""
        (state,) = [item for item in unit.diagnostics if item.code == "cable.harness_state"]
        return state

    def assert_every_leg(self, plan, disposition, code):
        """Assert every unit has *disposition*, carries *code*, and plans no change."""
        for unit in plan.units:
            with self.subTest(unit=unit.identity):
                self.assertEqual(unit.disposition, disposition, self.codes(unit))
                self.assertIn(code, self.codes(unit))
                self.assertEqual(unit.changes, ())


@CHANNELS
class HarnessPlanningTest(HarnessTopologyMixin, TestCase):
    """Section 6: leg traces on one channelized parent plan one shared breakout Cable, or block together."""

    @classmethod
    def setUpTestData(cls):
        cls.build_harness_topology()

    def test_four_legs_share_one_harness_change_and_name_each_other(self):
        self.resolve_lanes(range(1, 5))

        plan = self.plan(*(panel_leg(number) for number in range(1, 5)))

        identities = [unit.identity for unit in plan.units]
        self.assertEqual([unit.disposition for unit in plan.units], [Disposition.ACTIONABLE] * 4)
        (change,) = {change for unit in plan.units for change in self.harness_changes(unit)}
        self.assertEqual(change.identity, f"{HARNESS_PREFIX}dcim.interface:{self.parent.pk}")
        harness = change.to_dict()["payload"]["harness"]
        self.assertEqual(harness["profile"], BREAKOUT_4)
        self.assertEqual(harness["parent"], ["dcim.interface", self.parent.pk, self.switch.pk])
        self.assertEqual(
            [child for child, _far in harness["legs"]],
            [["dcim.interface", self.children[n].pk, self.switch.pk] for n in range(1, 5)],
        )
        self.assertEqual(
            [far for _child, far in harness["legs"]],
            [["dcim.frontport", self.fronts[n].pk, self.fronts[n].device_id] for n in range(1, 5)],
        )
        self.assertEqual(change.payload["cable_type"], "mmf-om4")
        self.assertEqual(len(change.payload["sources"]), 4)
        for unit in plan.units:
            with self.subTest(unit=unit.identity):
                self.assertEqual(self.harness_changes(unit), [change])
                self.assertEqual(unit.selected_with, tuple(sorted(set(identities) - {unit.identity})))
                self.assertEqual(
                    self.state(unit).to_dict()["evidence"]["legs"],
                    [[n, f"dcim.device:{self.switch.pk}", f"dcim.frontport:{self.fronts[n].pk}"] for n in range(1, 5)],
                )

    def test_a_single_lane_names_no_companion_and_warns_of_the_unused_channels(self):
        self.resolve_lanes([1])

        (unit,) = self.plan(panel_leg(1)).units

        self.assertEqual(unit.disposition, Disposition.ACTIONABLE)
        self.assertEqual(unit.selected_with, ())
        (partial,) = [item for item in unit.diagnostics if item.code == "cable.harness_partial"]
        self.assertEqual((partial.severity, partial.to_dict()["display"]["missing"]), (Severity.WARNING, [2, 3, 4]))

    def test_one_leg_alone_is_refused_by_the_workspace_which_offers_the_whole_harness(self):
        self.resolve_lanes(range(1, 5))
        plan = self.plan(*(panel_leg(number) for number in range(1, 5)))
        first, *others = (unit.identity for unit in plan.units)

        workspace = ReviewWorkspace(plan, self.actor)

        self.assertTrue(workspace.cannot_sync((first,)))
        self.assertEqual(workspace.sync_selection(first), (first, *others))
        self.assertFalse(workspace.cannot_sync(workspace.sync_selection(first)))
        self.assertEqual(workspace.sync_all.units, (first, *others))

    def test_far_ends_of_two_models_block_every_leg(self):
        self.resolve_lanes(range(1, 5))

        plan = self.plan(*(panel_leg(number) for number in range(1, 4)), direct_leg(4))

        self.assert_every_leg(plan, Disposition.BLOCKED, "cable.harness_mixed_terminations")
        (mixed,) = [item for item in plan.units[0].diagnostics if item.code == "cable.harness_mixed_terminations"]
        self.assertEqual(mixed.to_dict()["display"]["models"], ["dcim.frontport", "dcim.interface"])

    def test_a_channel_gap_blocks_every_leg(self):
        self.resolve_lanes([1, 2, 4])

        plan = self.plan(*(panel_leg(number) for number in (1, 2, 4)))

        self.assert_every_leg(plan, Disposition.BLOCKED, "cable.harness_channel_gap")
        (gap,) = [item for item in plan.units[0].diagnostics if item.code == "cable.harness_channel_gap"]
        self.assertEqual(gap.to_dict()["display"]["missing"], [3])

    def test_two_lanes_on_one_channel_block_every_leg(self):
        self.resolve_lanes([1])
        self.save_resolution(lane(5), self.children[1])

        plan = self.plan(panel_leg(1), panel_leg(2, start=lane(5)))

        self.assert_every_leg(plan, Disposition.BLOCKED, "cable.harness_leg_conflict")

    def test_a_cable_on_the_parent_blocks_every_leg(self):
        self.resolve_lanes(range(1, 5))
        spare = Interface.objects.create(device=self.make_device("OTHER"), name="eth9", type="100gbase-x-qsfp28")
        self.connect(self.parent, spare)

        plan = self.plan(*(panel_leg(number) for number in range(1, 5)))

        self.assert_every_leg(plan, Disposition.BLOCKED, "cable.harness_occupied")

    def test_a_parent_marked_connected_blocks_every_leg(self):
        """NetBox refuses a Cable on a termination marked as connected, and the harness cables the parent."""
        self.resolve_lanes(range(1, 5))
        Interface.objects.filter(pk=self.parent.pk).update(mark_connected=True)

        plan = self.plan(*(panel_leg(number) for number in range(1, 5)))

        self.assert_every_leg(plan, Disposition.BLOCKED, "cable.harness_occupied")
        (occupied,) = [item for item in plan.units[0].diagnostics if item.code == "cable.harness_occupied"]
        self.assertEqual((occupied.display["port"], occupied.display["marked_connected"]), (str(self.parent), True))
        messages = {item["code"]: item["message"] for item in ReviewWorkspace(plan, self.actor).traces[0].findings}
        self.assertIn("as connected without a Cable", messages["cable.harness_occupied"])

    def test_a_far_end_marked_connected_blocks_every_leg(self):
        self.resolve_lanes(range(1, 5))
        FrontPort.objects.filter(pk=self.fronts[3].pk).update(mark_connected=True)

        plan = self.plan(*(panel_leg(number) for number in range(1, 5)))

        self.assert_every_leg(plan, Disposition.BLOCKED, "cable.harness_occupied")
        (occupied,) = [item for item in plan.units[0].diagnostics if item.code == "cable.harness_occupied"]
        self.assertEqual(occupied.display["port"], str(self.fronts[3]))

    def test_a_virtual_far_end_blocks_every_leg_and_reopens_its_picker(self):
        """NetBox refuses a Cable on a virtual Interface (CableTermination.clean)."""
        self.resolve_lanes(range(1, 5))
        Interface.objects.filter(pk=self.servers[3].pk).update(type="virtual")

        plan = self.plan(*(direct_leg(number) for number in range(1, 5)))

        self.assert_every_leg(plan, Disposition.BLOCKED, "cable.termination_not_connectable")
        (refused,) = [item for item in plan.units[0].diagnostics if item.code == "cable.termination_not_connectable"]
        self.assertEqual(
            (refused.display["port"], refused.display["interface_type"], refused.display["parent"]),
            (str(self.servers[3]), "virtual", str(self.parent)),
        )
        records = {
            item["label"]: item["incompatible"]
            for unit in plan.units
            for item in unit.display["trace"]["terminations"]
            if item["label"].startswith("SRV-")
        }
        self.assertEqual(records, {"SRV-1 eth0": False, "SRV-2 eth0": False, "SRV-3 eth0": True, "SRV-4 eth0": False})
        messages = {item["code"]: item["message"] for item in ReviewWorkspace(plan, self.actor).traces[0].findings}
        self.assertIn("Choose another termination", messages["cable.termination_not_connectable"])

    def test_a_virtual_parent_blocks_every_leg(self):
        self.resolve_lanes(range(1, 5))
        Interface.objects.filter(pk=self.parent.pk).update(type="virtual")

        plan = self.plan(*(direct_leg(number) for number in range(1, 5)))

        self.assert_every_leg(plan, Disposition.BLOCKED, "cable.termination_not_connectable")
        (refused,) = [item for item in plan.units[0].diagnostics if item.code == "cable.termination_not_connectable"]
        self.assertEqual(refused.display["port"], str(self.parent))

    def test_a_physical_far_end_type_change_keeps_the_harness_actionable(self):
        self.resolve_lanes(range(1, 5))
        Interface.objects.filter(pk=self.servers[3].pk).update(type="10gbase-x-sfpp")

        plan = self.plan(*(direct_leg(number) for number in range(1, 5)))

        for unit in plan.units:
            self.assertEqual(unit.disposition, Disposition.ACTIONABLE, self.codes(unit))

    def test_an_unresolved_lane_in_the_cohort_blocks_the_resolved_legs(self):
        self.resolve_lanes([1, 2, 3])

        plan = self.plan(*(panel_leg(number) for number in range(1, 5)))

        legs, unresolved = plan.units[:3], plan.units[3]
        self.assertIn("cable.termination_unresolved", self.codes(unresolved))
        for unit in legs:
            with self.subTest(unit=unit.identity):
                self.assertEqual(unit.disposition, Disposition.BLOCKED)
                (member,) = [item for item in unit.diagnostics if item.code == "cable.harness_member_unresolved"]
                self.assertEqual(member.display["trace"], unresolved.display["name"])

    def test_two_cohorts_on_one_parent_plan_one_harness(self):
        self.resolve_lanes([1, 2])
        self.resolve_lanes([3, 4], cards="Breakout B")

        plan = self.plan(panel_leg(1), panel_leg(2), *(panel_leg(n, start=lane(n, cards="Breakout B")) for n in (3, 4)))

        self.assertEqual([unit.disposition for unit in plan.units], [Disposition.ACTIONABLE] * 4)
        self.assertEqual(len({change for unit in plan.units for change in self.harness_changes(unit)}), 1)

    def test_two_parents_in_one_blank_cohort_both_plan(self):
        switch = self.make_device("SW-2")
        parents = [self.make_parent(switch, name, 2) for name in ("1/1", "1/2")]
        children = [self.make_child(parent, channel) for parent in parents for channel in (1, 2)]
        lanes = [lane(n, device="SW-2", cards="", port=f"Q{n}") for n in range(1, 5)]
        for reference, child in zip(lanes, children, strict=True):
            self.save_resolution(reference, child)

        plan = self.plan(*(panel_leg(n, start=reference) for n, reference in enumerate(lanes, start=1)))

        self.assertEqual([unit.disposition for unit in plan.units], [Disposition.ACTIONABLE] * 4)
        changes = {change.identity for unit in plan.units for change in self.harness_changes(unit)}
        self.assertEqual(changes, {f"{HARNESS_PREFIX}dcim.interface:{parent.pk}" for parent in parents})
        self.assertEqual(
            [unit.selected_with for unit in plan.units[:2]], [(plan.units[1].identity,), (plan.units[0].identity,)]
        )

    def test_a_leg_without_a_cable_type_holds_back_the_legs_that_have_one(self):
        """An unmapped or stale Cable Type blocks its own leg, and selected_with keeps the others from syncing."""
        self.resolve_lanes(range(1, 5))
        CableClassMapping.objects.create(profile=self.profile, cable_class="Unmapped")
        CableClassMapping.objects.create(
            profile=self.profile, cable_class="Removed", cable_type_resolved=True, cable_type="mmf-om4"
        )
        CableClassMapping.objects.filter(profile=self.profile, cable_class="Removed").update(cable_type="removed-type")
        lanes = [
            (
                trace_endpoint_line(lane(number)),
                trace_endpoint_line(server_port(number)),
                (trace_segment(lane(number), cable_class, server_port(number)),),
            )
            for number, cable_class in ((1, "Unmapped"), (2, "Removed"), (3, FIBER), (4, FIBER))
        ]

        plan = self.plan(*lanes)

        self.assertIn("cable.cableclass_unmapped", self.codes(plan.units[0]))
        self.assertIn("cable.policy_stale", self.codes(plan.units[1]))
        self.assertEqual(
            [unit.disposition for unit in plan.units], [Disposition.BLOCKED] * 2 + [Disposition.ACTIONABLE] * 2
        )
        (change,) = {change for unit in plan.units for change in self.harness_changes(unit)}
        self.assertEqual(change.payload["cable_type"], "mmf-om4")
        workspace = ReviewWorkspace(plan, self.actor)
        self.assertTrue(workspace.cannot_sync(workspace.sync_selection(plan.units[2].identity)))
        self.assertEqual(workspace.sync_all.units, ())

    def test_legs_whose_cable_types_disagree_block_every_leg(self):
        self.resolve_lanes(range(1, 5))
        identity = self.identity_of(panel_leg(1))
        self.force(
            self.children[1], self.fronts[1], cable_type="smf", cable_profile="single-1c1p", trace_identity=identity
        )

        plan = self.plan(*(panel_leg(number) for number in range(1, 5)))

        self.assert_every_leg(plan, Disposition.BLOCKED, "cable.harness_type_conflict")

    def test_a_channel_count_with_no_breakout_profile_blocks_every_leg(self):
        switch = self.make_device("SW-3")
        parent = self.make_parent(switch, "1/1", 3)
        lanes = [lane(n, device="SW-3") for n in range(1, 4)]
        for number, reference in enumerate(lanes, start=1):
            self.save_resolution(reference, self.make_child(parent, number))

        plan = self.plan(*(panel_leg(n, start=reference) for n, reference in enumerate(lanes, start=1)))

        self.assert_every_leg(plan, Disposition.BLOCKED, "cable.harness_profile_unavailable")

    def test_a_segment_between_two_channel_children_is_unsupported(self):
        self.resolve_lanes(range(1, 5))
        between = (
            trace_endpoint_line(lane(1)),
            trace_endpoint_line(lane(2)),
            (trace_segment(lane(1), FIBER, lane(2)),),
        )

        plan = self.plan(between, panel_leg(3), panel_leg(4))

        self.assertEqual(plan.units[0].disposition, Disposition.BLOCKED)
        self.assertIn("cable.harness_unsupported", self.codes(plan.units[0]))
        self.assertEqual(plan.units[0].changes, ())
        for unit in plan.units[1:]:
            self.assertEqual(unit.disposition, Disposition.BLOCKED)
            self.assertIn("cable.harness_unsupported", self.codes(unit))

    def test_a_segment_between_two_channel_children_alone_blocks_without_a_harness(self):
        self.resolve_lanes([1, 2])
        between = (
            trace_endpoint_line(lane(1)),
            trace_endpoint_line(lane(2)),
            (trace_segment(lane(1), FIBER, lane(2)),),
        )

        (unit,) = self.plan(between).units

        self.assertEqual(unit.disposition, Disposition.BLOCKED)
        self.assertIn("cable.harness_unsupported", self.codes(unit))
        self.assertEqual(unit.changes, ())

    def test_a_lone_lane_without_a_first_hop_blocks_without_a_harness(self):
        self.resolve_lanes([4])
        lost = (
            trace_endpoint_line(lane(4)),
            trace_endpoint_line(server_port(9)),
            (trace_segment(lane(4), FIBER, server_port(9)),),
        )

        (unit,) = self.plan(lost).units

        self.assertEqual(unit.disposition, Disposition.BLOCKED)
        self.assertIn("trace.device_unresolved", self.codes(unit))
        self.assertEqual(unit.changes, ())

    def test_a_leg_that_ends_on_its_own_parent_is_unsupported(self):
        """The source names the parent port under another cards label, so only the resolved objects meet."""
        self.resolve_lanes([1])
        uplink = trace_termination("SW-1", "Uplinks", "0/0", "Port")
        loop = (trace_endpoint_line(lane(1)), trace_endpoint_line(uplink), (trace_segment(lane(1), FIBER, uplink),))

        plan = self.plan(loop)

        (unit,) = plan.units
        self.assertEqual(unit.disposition, Disposition.BLOCKED, self.codes(unit))
        self.assertIn("cable.harness_unsupported", self.codes(unit))
        self.assertEqual(unit.changes, ())
        messages = [item["message"] for item in ReviewWorkspace(plan, self.actor).traces[0].findings]
        self.assertIn("its own parent", " ".join(messages))

    def test_an_ordinary_trace_that_ends_on_a_channelized_parent_blocks_and_reopens_its_picker(self):
        """NetBox accepts that Cable but mirrors no channel through it, so no channel would reach its far end."""
        plan = self.plan(uplink_trace())

        (unit,) = plan.units
        self.assertEqual((unit.disposition, unit.changes), (Disposition.BLOCKED, ()))
        (refused,) = [item for item in unit.diagnostics if item.code == "cable.termination_channelized"]
        self.assertEqual(refused.display["port"], str(self.parent))
        records = {item["label"]: item["incompatible"] for item in unit.display["trace"]["terminations"]}
        self.assertEqual(
            records, {"SW-1 Uplinks 0/0": True, "PANEL-H F1": False, "PANEL-H R1": False, "SRV-1 eth0": False}
        )
        messages = {item["code"]: item["message"] for item in ReviewWorkspace(plan, self.actor).traces[0].findings}
        self.assertIn("channel subinterface", messages["cable.termination_channelized"])

    def test_a_plain_interface_in_the_cohort_warns_and_blocks_nothing(self):
        self.resolve_lanes(range(1, 5))
        Interface.objects.create(device=self.switch, name="P05", type="25gbase-x-sfp28")
        spare = Interface.objects.create(device=self.make_device("SRV-5"), name="eth0", type="25gbase-x-sfp28")

        plan = self.plan(
            *(panel_leg(number) for number in range(1, 5)),
            (
                trace_endpoint_line(lane(5)),
                trace_endpoint_line(server_port(5)),
                (trace_segment(lane(5), FIBER, server_port(5)),),
            ),
        )

        self.assertTrue(spare.pk)
        self.assertEqual([unit.disposition for unit in plan.units], [Disposition.ACTIONABLE] * 5)
        for unit in plan.units[:4]:
            (warning,) = [item for item in unit.diagnostics if item.code == "cable.harness_member_unchannelized"]
            self.assertEqual((warning.severity, warning.display["port"]), (Severity.WARNING, "P05"))

    def test_an_existing_harness_is_reused_only_while_its_termination_snapshot_matches(self):
        self.resolve_lanes(range(1, 5))
        harness = self.build_existing_harness()
        blocks = [panel_leg(number) for number in range(1, 5)]
        for unit in self.plan(*blocks).units:
            self.assertEqual(self.harness_changes(unit), [])
            self.assertIn("cable.segment_reused", self.codes(unit))
            self.assertEqual(unit.selected_with, ())

        CableTermination.objects.filter(cable=harness, cable_end="B", connector=2).update(positions=[2])

        self.assert_every_leg(self.plan(*blocks), Disposition.BLOCKED, "cable.harness_occupied")

    def test_a_child_that_does_not_mirror_the_parent_cable_invalidates_every_leg(self):
        self.resolve_lanes(range(1, 5))
        self.build_existing_harness()
        blocks = [panel_leg(number) for number in range(1, 5)]
        self.assertNotIn(Disposition.INVALID, [unit.disposition for unit in self.plan(*blocks).units])

        Interface.objects.filter(pk=self.children[2].pk).update(cable_positions=[3])

        plan = self.plan(*blocks)
        self.assert_every_leg(plan, Disposition.INVALID, "cable.harness_channel_state_invalid")
        (fault,) = [item for item in plan.units[0].diagnostics if item.code == "cable.harness_channel_state_invalid"]
        self.assertEqual((fault.display["port"], fault.display["fault"]), (str(self.children[2]), "mirror"))

    def test_an_unexported_child_past_the_channel_count_invalidates_every_leg(self):
        self.resolve_lanes([1, 2, 3])
        Interface.objects.filter(pk=self.children[4].pk).update(channel_id=9)

        plan = self.plan(*(panel_leg(number) for number in (1, 2, 3)))

        self.assert_every_leg(plan, Disposition.INVALID, "cable.harness_channel_state_invalid")
        (fault,) = [item for item in plan.units[0].diagnostics if item.code == "cable.harness_channel_state_invalid"]
        self.assertEqual((fault.display["channel"], fault.display["fault"]), (9, "channel"))

    def test_a_child_with_its_own_cable_termination_invalidates_every_leg(self):
        self.resolve_lanes(range(1, 5))
        stray = self.connect(self.rears[1], Interface.objects.create(device=self.switch, name="x", type="1000base-t"))
        CableTermination.objects.filter(cable=stray, cable_end="B").update(termination_id=self.children[3].pk)

        plan = self.plan(*(panel_leg(number) for number in range(1, 5)))

        self.assert_every_leg(plan, Disposition.INVALID, "cable.harness_channel_state_invalid")


class OrdinaryUnitBytesTest(CableTopologyMixin, TestCase):
    """Acceptance 5: a unit without companions serializes as it did before co-selection existed."""

    @classmethod
    def setUpTestData(cls):
        cls.build_topology()

    def test_an_ordinary_unit_has_no_selected_with_key(self):
        (unit,) = self.plan(patched_path()).units

        self.assertEqual(unit.disposition, Disposition.ACTIONABLE)
        self.assertNotIn("selected_with", unit.to_dict())


@CHANNELS
class HarnessExecutionTest(HarnessTopologyMixin, TransactionTestCase):
    """Acceptance 1, 2 and 4, and the execution refusals, against the real database."""

    def setUp(self):
        self.build_harness_topology()

    def execute(self, plan, identities=None, key=None, actor=None):
        """Run one selection through the public coordinator seam."""
        return ImportEngine.execute(
            self.profile,
            self.document,
            plan.to_dict(),
            identities or [unit.identity for unit in plan.units if unit.disposition == Disposition.ACTIONABLE],
            key or str(uuid.uuid4()),
            actor or self.actor,
        )

    def test_a_lane_resolved_to_a_channel_child_cables_the_parent_and_never_the_child(self):
        self.resolve_lanes([1])
        plan = self.plan(panel_leg(1))
        (unit,) = plan.units
        self.assertEqual(unit.disposition, Disposition.ACTIONABLE, unit.diagnostics)

        execution = self.execute(plan)

        self.assertEqual(execution.outcome, "succeeded")
        child = self.children[1]
        self.assertFalse(cables_on(child).exists())
        harness = cables_on(self.parent).get()
        self.assertEqual(harness.profile, BREAKOUT_4)
        child.refresh_from_db()
        self.assertEqual(child.connected_endpoints, [self.servers[1]])

    def test_four_legs_write_one_breakout_cable_and_replan_as_no_op(self):
        self.resolve_lanes(range(1, 5))
        blocks = [panel_leg(number) for number in range(1, 5)]
        plan = self.plan(*blocks)

        execution = self.execute(plan)

        self.assertEqual(execution.outcome, "succeeded")
        harness = cables_on(self.parent).get()
        self.assertEqual((harness.profile, harness.type), (BREAKOUT_4, "mmf-om4"))
        self.assertEqual(
            termination_snapshot(harness),
            sorted(
                [("A", 1, [1, 2, 3, 4], "interface", self.parent.pk)]
                + [("B", number, [1], "frontport", self.fronts[number].pk) for number in range(1, 5)]
            ),
        )
        for number, child in self.children.items():
            with self.subTest(channel=number):
                child.refresh_from_db()
                self.assertEqual(child.connected_endpoints, [self.servers[number]])
        self.assertEqual(CableImportSource.objects.filter(cable=harness).count(), 4)
        self.assertEqual(Cable.objects.count(), 5)
        replan = self.plan(*blocks)
        self.assertEqual([unit.disposition for unit in replan.units], [Disposition.NO_OP] * 4)
        for unit in replan.units:
            self.assertEqual(unit.selected_with, ())
            self.assertIs(self.state(unit).evidence["reused"], True)

    def test_one_leg_alone_is_refused_by_the_engine_and_writes_nothing(self):
        self.resolve_lanes(range(1, 5))
        plan = self.plan(*(panel_leg(number) for number in range(1, 5)))
        first, *others = (unit.identity for unit in plan.units)
        quoted = ", ".join(f"'{identity}'" for identity in others)

        with self.assertRaisesMessage(
            SelectionError, f"Synchronization Unit '{first}' must be selected together with {quoted}."
        ):
            self.execute(plan, [first], key="one-leg")

        self.assertFalse(Cable.objects.exists())
        self.assertEqual(ImportExecution.objects.get(idempotency_key="one-leg").failure_detail["reason"], "selection")

    def test_a_trailing_partial_harness_writes_a_shorter_b_side(self):
        self.resolve_lanes([1, 2])
        plan = self.plan(panel_leg(1), panel_leg(2))

        self.execute(plan)

        self.assertEqual(
            termination_snapshot(cables_on(self.parent).get()),
            sorted(
                [("A", 1, [1, 2, 3, 4], "interface", self.parent.pk)]
                + [("B", number, [1], "frontport", self.fronts[number].pk) for number in (1, 2)]
            ),
        )

    def test_a_channel_count_changed_after_acceptance_is_a_stale_plan(self):
        self.resolve_lanes(range(1, 5))
        plan = self.plan(*(panel_leg(number) for number in range(1, 5)))
        parent = Interface.objects.get(pk=self.parent.pk)
        parent.channels = 8
        parent.full_clean()
        parent.save()

        with self.assertRaises(StalePlan):
            self.execute(plan)

        self.assertFalse(Cable.objects.exists())

    def test_a_child_set_changed_after_acceptance_is_a_stale_plan(self):
        self.resolve_lanes([1, 2, 3])
        plan = self.plan(*(panel_leg(number) for number in (1, 2, 3)))
        self.children[4].delete()

        with self.assertRaises(StalePlan):
            self.execute(plan)

        self.assertFalse(Cable.objects.exists())

    def test_the_execution_locks_a_child_no_trace_states(self):
        """A native write that moves an unexported child out of the parent waits for the import (R5-1)."""
        self.resolve_lanes([1, 2, 3])
        plan = self.plan(*(panel_leg(number) for number in (1, 2, 3)))
        unexported = self.children[4].pk

        def move_the_child():
            Interface.objects.filter(pk=unexported).update(description="moved")

        with competing_write_during(pre_save, Cable, move_the_child) as (observed, blocked):
            self.execute(plan)

        self.assertTrue(observed, "the execution reached no Cable write")
        self.assertEqual(blocked, [True])

    def test_a_companion_the_current_plan_adds_makes_the_selection_stale(self):
        spare = Interface.objects.create(device=self.switch, name="spare", type="25gbase-x-sfp28")
        self.resolve_lanes([1])
        self.save_resolution(lane(2), spare)
        accepted = self.plan(panel_leg(1), panel_leg(2))
        first, second = (unit.identity for unit in accepted.units)
        self.assertEqual(accepted.units[0].selected_with, ())
        self.children[2].delete()
        spare.parent, spare.channel_id = self.parent, 2
        spare.full_clean()
        spare.save()

        with self.assertRaisesMessage(
            StalePlan,
            f"Synchronization Unit '{first}' changed after the plan was accepted: it now needs '{second}' selected and actionable.",
        ):
            self.execute(accepted, [first], key="companion-added")

        self.assertFalse(Cable.objects.exists())
        self.assertEqual(
            ImportExecution.objects.get(idempotency_key="companion-added").failure_detail["reason"], "stale_plan"
        )

    def test_a_companion_that_stopped_being_actionable_makes_the_selection_stale(self):
        self.resolve_lanes([1, 2])
        accepted = self.plan(panel_leg(1), panel_leg(2))
        first, second = (unit.identity for unit in accepted.units)
        self.connect(
            self.rears[2],
            Interface.objects.create(device=self.make_device("OTHER"), name="eth9", type="25gbase-x-sfp28"),
        )

        with self.assertRaisesMessage(
            StalePlan,
            f"Synchronization Unit '{first}' changed after the plan was accepted: it now needs '{second}' selected and actionable.",
        ):
            self.execute(accepted, [first, second], key="companion-blocked")

        self.assertFalse(cables_on(self.parent).exists())
        self.assertEqual(
            ImportExecution.objects.get(idempotency_key="companion-blocked").failure_detail["reason"], "stale_plan"
        )

    def execute_after_a_deletion_before_the_lock(self, plan, row_id):
        """Execute, deleting one Interface on a second connection after the locked replan read it, before its lock."""
        lock_harness_parents = _CableBatch._lock_harness_parents
        deleted = []

        def delete_then_lock(batch):
            if batch.lock_plan_references and not deleted:
                deleted.append(row_id)

                def delete():
                    # A raw delete takes no Device row lock, which the replan already holds.
                    with connection.cursor() as cursor:
                        cursor.execute("DELETE FROM dcim_interface WHERE id = %s", [row_id])

                with run_on_separate_connection(delete):
                    pass
            lock_harness_parents(batch)

        with patch.object(_CableBatch, "_lock_harness_parents", autospec=True, side_effect=delete_then_lock):
            try:
                return self.execute(plan, key="deleted-before-lock")
            finally:
                self.assertEqual(deleted, [row_id], "the locked replan never ran")

    def test_a_far_end_deleted_before_the_replan_locks_it_refuses_the_harness(self):
        self.resolve_lanes([1, 2])
        plan = self.plan(direct_leg(1), direct_leg(2))
        far = self.servers[2]

        with self.assertRaisesMessage(
            PreconditionFailed, f"dcim.interface {far.pk} is gone, so the breakout Cable cannot be created."
        ):
            self.execute_after_a_deletion_before_the_lock(plan, far.pk)

        self.assertFalse(Cable.objects.exists())
        self.assertEqual(
            ImportExecution.objects.get(idempotency_key="deleted-before-lock").failure_detail["reason"],
            "precondition",
        )

    def test_a_channel_child_deleted_before_the_replan_locks_it_makes_the_plan_stale(self):
        """Without its child the trace is no leg, so the replan no longer carries the harness."""
        self.resolve_lanes([1, 2])
        plan = self.plan(direct_leg(1), direct_leg(2))

        with self.assertRaises(StalePlan):
            self.execute_after_a_deletion_before_the_lock(plan, self.children[2].pk)

        self.assertFalse(Cable.objects.exists())
        self.assertEqual(
            ImportExecution.objects.get(idempotency_key="deleted-before-lock").failure_detail["reason"], "stale_plan"
        )

    def test_the_execution_locks_the_parent_before_any_write(self):
        """SRV sorts before SW, so the leg is the last segment and the first write is an ordinary Cable.

        Only the replan holds the parent at that write.
        """
        self.resolve_lanes(range(1, 5))
        plan = self.plan(*(panel_leg(number) for number in range(1, 5)))
        self.assertEqual([unit.disposition for unit in plan.units], [Disposition.ACTIONABLE] * 4)
        self.assertFalse(plan.units[0].changes[0].identity.startswith(HARNESS_PREFIX))
        parent = self.parent.pk

        def touch_the_parent():
            Interface.objects.filter(pk=parent).update(description="touched")

        with competing_write_during(pre_save, Cable, touch_the_parent) as (observed, blocked):
            self.execute(plan)

        self.assertTrue(observed, "the execution reached no Cable write")
        self.assertEqual(blocked, [True])
        self.assertEqual(cables_on(self.parent).get().profile, BREAKOUT_4)

    def test_a_harness_write_refuses_channels_that_moved_after_planning(self):
        """The write rebuilds the intent from locked rows, so a profile the parent no longer has never reaches NetBox."""
        self.resolve_lanes(range(1, 5))
        plan = self.plan(*(panel_leg(number) for number in range(1, 5)))
        (change,) = self.harness_changes(plan.units[0])
        parent = Interface.objects.get(pk=self.parent.pk)
        parent.channels = 8
        parent.full_clean()
        parent.save()
        context = ExecutionContext(actor=self.actor, reader=self.reader(), profile=self.profile)

        with (
            self.assertRaisesMessage(
                PreconditionFailed, "The channels of this harness changed after the plan was made."
            ),
            transaction.atomic(),
        ):
            CableModule().apply(change, context)

        self.assertFalse(Cable.objects.exists())

    def test_a_parent_device_hidden_after_acceptance_is_a_stale_plan(self):
        """The locked replan rechecks the parent's Device, so the harness is never written on a hidden member."""
        member, legs = self.build_chassis_harness(serial="")
        actor = user_with_object_permission(
            "harness-chassis-executor",
            [
                (Site, ("view",), {}),
                (Device, ("view",), {"serial": ""}),
                (Interface, ("view",), {}),
                (FrontPort, ("view",), {}),
                (RearPort, ("view",), {}),
                (Cable, ("add",), {}),
            ],
        )
        accepted = self.plan(*legs, actor=actor)
        self.assertEqual([unit.disposition for unit in accepted.units], [Disposition.ACTIONABLE] * 2)
        Device.objects.filter(pk=member.pk).update(serial="MEMBER-B")

        with self.assertRaises(StalePlan):
            self.execute(accepted, actor=actor)

        self.assertFalse(Cable.objects.exists())

    def test_a_child_moved_to_another_chassis_member_before_its_lock_is_a_stale_plan(self):
        """The child stays a channel of its parent, but it no longer answers the source's Device reference."""
        from django.db import connection

        member = self.join_chassis()
        self.resolve_lanes([1])
        accepted = self.plan(panel_leg(1))
        child = self.children[1].pk
        moved = []
        table = Interface._meta.db_table

        def move_before_the_first_interface_lock(execute, sql, params, many, context):
            if moved or f'FROM "{table}"' not in sql or "FOR UPDATE" not in sql.upper():
                return execute(sql, params, many, context)
            moved.append(True)
            with run_on_separate_connection(lambda: Interface.objects.filter(pk=child).update(device=member)):
                pass
            return execute(sql, params, many, context)

        with connection.execute_wrapper(move_before_the_first_interface_lock), self.assertRaises(StalePlan):
            self.execute(accepted)

        self.assertEqual((moved, Interface.objects.get(pk=child).device_id), ([True], member.pk))
        self.assertFalse(Cable.objects.exists())

    def test_a_harness_write_refuses_a_child_that_moved_to_another_device(self):
        member = self.join_chassis()
        self.resolve_lanes([1])
        (change,) = self.harness_changes(self.plan(panel_leg(1)).units[0])
        Interface.objects.filter(pk=self.children[1].pk).update(device=member)
        context = ExecutionContext(actor=self.actor, reader=self.reader(), profile=self.profile)

        with (
            self.assertRaisesMessage(PreconditionFailed, "moved to another Device after the plan was made."),
            transaction.atomic(),
        ):
            CableModule().apply(change, context)

        self.assertFalse(Cable.objects.exists())

    def test_a_harness_write_refuses_a_parent_marked_connected_after_planning(self):
        self.resolve_lanes([1])
        (change,) = self.harness_changes(self.plan(panel_leg(1)).units[0])
        Interface.objects.filter(pk=self.parent.pk).update(mark_connected=True)
        context = ExecutionContext(actor=self.actor, reader=self.reader(), profile=self.profile)

        with self.assertRaises(IntentRefused) as refused, transaction.atomic():
            CableModule().apply(change, context)

        self.assertEqual(refused.exception.code, "cable.harness_occupied")
        self.assertFalse(Cable.objects.exists())

    def test_an_ordinary_write_refuses_a_channel_child_before_netbox_does(self):
        context = ExecutionContext(actor=self.actor, reader=self.reader(), profile=self.profile)
        child, front = self.children[1], self.fronts[1]
        change = PlannedChange(
            identity="cable:create:forged",
            target_module=CableModule.key,
            operation="create",
            payload={
                "terminations": [
                    ["dcim.interface", child.pk, child.device_id],
                    ["dcim.frontport", front.pk, front.device_id],
                ],
                "status": "connected",
                "sources": [],
                "cable_type": "mmf-om4",
                "cable_profile": "single-1c1p",
            },
        )

        with self.assertRaises(IntentRefused) as refused, transaction.atomic():
            CableModule().apply(change, context)

        self.assertEqual(refused.exception.code, "cable.harness_unsupported")
        self.assertFalse(Cable.objects.exists())

    def test_an_ordinary_trace_never_replaces_a_one_leg_breakout(self):
        """One leg gives the breakout one end per side, so it joins the two endpoints of an ordinary trace."""
        self.resolve_lanes([1])
        self.assertEqual(self.execute(self.plan(direct_leg(1))).outcome, "succeeded")
        harness = cables_on(self.parent).get()
        before = termination_snapshot(harness)

        plan = self.plan(uplink_trace())
        (unit,) = plan.units
        with self.assertRaisesMessage(SelectionError, "was not actionable when selected"):
            self.execute(plan, [unit.identity])

        self.assertEqual(termination_snapshot(Cable.objects.get(pk=harness.pk)), before)
        self.assertEqual(Cable.objects.count(), 1)
        self.assertEqual((unit.disposition, unit.changes), (Disposition.BLOCKED, ()))
        occupied = sorted(
            item.display["port"] for item in unit.diagnostics if item.code == "cable.termination_occupied"
        )
        self.assertEqual(occupied, sorted([str(self.parent), str(self.servers[1])]))

    def test_a_logical_cable_that_became_a_breakout_refuses_its_deletion(self):
        """The deletion decides from the locked Cable and its ends, so a stale or forged change cannot remove a breakout."""
        context = ExecutionContext(actor=self.actor, reader=self.reader(), profile=self.profile)
        cases = {
            "breakout profile": lambda cable, end: Cable.objects.filter(pk=cable.pk).update(profile=BREAKOUT_4),
            "channelized end": lambda cable, end: Interface.objects.filter(pk=end.pk).update(channels=4),
        }
        for number, (case, make_breakout) in enumerate(cases.items(), start=1):
            with self.subTest(case=case):
                other = trace_termination(f"OTHER-{number}", "", "eth9", "NIC")
                far = Interface.objects.create(device=self.make_device(f"OTHER-{number}"), name="eth9", type="lc")
                logical = self.connect(self.servers[number], far)
                trace = (
                    trace_endpoint_line(server_port(number)),
                    trace_endpoint_line(other),
                    (
                        trace_segment(server_port(number), FIBER, panel_front(number)),
                        trace_segment(panel_rear(number), FIBER, other),
                    ),
                )
                (delete, *_creations) = self.plan(trace).units[0].changes
                self.assertEqual(delete.identity, f"cable:delete:{logical.pk}")
                make_breakout(logical, self.servers[number])

                with (
                    self.assertRaisesMessage(PreconditionFailed, "the import never deletes it"),
                    transaction.atomic(),
                ):
                    CableModule().apply(delete, context)

                self.assertTrue(Cable.objects.filter(pk=logical.pk).exists())


@CHANNELS
class CableIntentTest(HarnessTopologyMixin, TestCase):
    """The one Cable write constructor, with selected bad cases beside the nearest valid ones."""

    @classmethod
    def setUpTestData(cls):
        cls.build_harness_topology()

    def refusal(self, legs):
        """Return the code the constructor refuses one harness with."""
        with self.assertRaises(IntentRefused) as refused:
            harness_intent(self.parent, legs)
        return refused.exception.code

    def test_an_ordinary_write_refuses_a_channelized_parent_and_its_child(self):
        """NetBox mirrors no channel through an ordinary Cable on the parent, so only a harness cables it."""
        self.assertEqual(
            ordinary_intent(self.servers[1], self.fronts[1], profile="").a_terminations, (self.servers[1],)
        )
        for first, second, code in (
            (self.fronts[1], self.parent, "cable.termination_channelized"),
            (self.parent, self.fronts[1], "cable.termination_channelized"),
            (self.fronts[1], self.children[1], "cable.harness_unsupported"),
        ):
            with self.subTest(first=str(first), second=str(second)):
                with self.assertRaises(IntentRefused) as refused:
                    ordinary_intent(first, second, profile="")
                self.assertEqual(refused.exception.code, code)

    def test_legs_given_out_of_order_take_the_connector_of_their_channel(self):
        intent = harness_intent(self.parent, [(self.children[2], self.fronts[2]), (self.children[1], self.fronts[1])])

        self.assertEqual(intent.b_terminations, (self.fronts[1], self.fronts[2]))
        self.assertEqual((intent.profile, intent.trailing), (BREAKOUT_4, (3, 4)))

    def test_a_far_end_that_is_a_channel_child_is_unsupported(self):
        self.assertEqual(self.refusal([(self.children[1], self.children[2])]), "cable.harness_unsupported")

    def test_one_far_end_on_two_channels_is_a_leg_conflict(self):
        legs = [(self.children[1], self.fronts[1]), (self.children[2], self.fronts[1])]

        self.assertEqual(self.refusal(legs), "cable.harness_leg_conflict")

    def test_a_far_end_that_is_a_channelized_interface_is_unsupported(self):
        other = self.make_parent(self.switch, "0/1", 4)

        self.assertEqual(self.refusal([(self.children[1], other)]), "cable.harness_unsupported")

    def test_a_far_end_that_is_the_parent_is_unsupported(self):
        self.assertEqual(self.refusal([(self.children[1], self.parent)]), "cable.harness_unsupported")

    def test_a_harness_without_legs_is_a_programming_error(self):
        with self.assertRaisesMessage(ValueError, "A harness intent needs at least one leg."):
            harness_intent(self.parent, [])

    def test_a_channel_past_the_parent_count_is_invalid_state(self):
        Interface.objects.filter(pk=self.children[4].pk).update(channel_id=9)
        stray = Interface.objects.get(pk=self.children[4].pk)

        self.assertEqual(self.refusal([(stray, self.fronts[1])]), "cable.harness_channel_state_invalid")


@CHANNELS
class HarnessReviewTest(HarnessTopologyMixin, TestCase):
    """Claims, permissions, disclosure and media around one harness."""

    @classmethod
    def setUpTestData(cls):
        cls.build_harness_topology()

    def test_a_lane_whose_far_end_is_unresolved_blocks_the_resolved_legs(self):
        """A stated lane with no first hop must not leave a shorter harness that no later import can extend."""
        self.resolve_lanes(range(1, 5))
        lost = (
            trace_endpoint_line(lane(4)),
            trace_endpoint_line(server_port(9)),
            (trace_segment(lane(4), FIBER, server_port(9)),),
        )

        plan = self.plan(*(panel_leg(number) for number in range(1, 4)), lost)

        self.assertIn("trace.device_unresolved", self.codes(plan.units[3]))
        for unit in plan.units[:3]:
            with self.subTest(unit=unit.identity):
                self.assertEqual(unit.disposition, Disposition.BLOCKED)
                self.assertIn("cable.harness_member_unresolved", self.codes(unit))

    def test_an_ordinary_trace_on_the_parent_blocks_it_and_every_leg(self):
        """The source names the parent port under another cards label, so only the resolved objects collide."""
        self.resolve_lanes(range(1, 5))
        uplink, other = (
            trace_termination("SW-1", "Uplinks", "0/0", "Port"),
            trace_termination("OTHER", "", "eth9", "NIC"),
        )
        Interface.objects.create(device=self.make_device("OTHER"), name="eth9", type="100gbase-x-qsfp28")
        rival = (trace_endpoint_line(uplink), trace_endpoint_line(other), (trace_segment(uplink, FIBER, other),))

        plan = self.plan(*(panel_leg(number) for number in range(1, 5)), rival)

        *legs, ordinary = plan.units
        self.assertEqual(ordinary.disposition, Disposition.BLOCKED)
        self.assertIn("cable.termination_channelized", self.codes(ordinary))
        for unit in legs:
            with self.subTest(unit=unit.identity):
                self.assertEqual((unit.disposition, unit.changes), (Disposition.BLOCKED, ()))
                (unsupported,) = [item for item in unit.diagnostics if item.code == "cable.harness_unsupported"]
                self.assertEqual(unsupported.display["trace"], "SW-1 Uplinks 0/0 to OTHER eth9")

    def test_a_panel_return_to_the_parent_is_unsupported_in_its_own_trace(self):
        """The harness cables the parent to F1, and the segment back from R1 names the parent as an ordinary port."""
        self.resolve_lanes([1])
        uplink = trace_termination("SW-1", "Uplinks", "0/0", "Port")
        loop = (
            trace_endpoint_line(lane(1)),
            trace_endpoint_line(uplink),
            (trace_segment(lane(1), FIBER, panel_front(1)), trace_segment(panel_rear(1), FIBER, uplink)),
        )

        plan = self.plan(loop)

        (unit,) = plan.units
        self.assertEqual((unit.disposition, unit.changes), (Disposition.BLOCKED, ()))
        self.assertIn("cable.harness_unsupported", self.codes(unit))
        messages = {item["code"]: item["message"] for item in ReviewWorkspace(plan, self.actor).traces[0].findings}
        self.assertIn("names the parent as an ordinary port", messages["cable.harness_unsupported"])

    def test_a_panel_return_to_the_far_end_conflicts_with_the_harness_of_its_own_trace(self):
        """The harness cables the parent to F1, and the segment back from R1 claims F1 under another source name."""
        self.resolve_lanes([1])
        alias = trace_termination("PANEL-H", "", "F1-ALIAS", "Position Front")
        self.save_resolution(alias, self.fronts[1])
        loop = (
            trace_endpoint_line(lane(1)),
            trace_endpoint_line(alias),
            (trace_segment(lane(1), FIBER, panel_front(1)), trace_segment(panel_rear(1), FIBER, alias)),
        )

        plan = self.plan(loop)

        (unit,) = plan.units
        self.assertEqual((unit.disposition, unit.changes), (Disposition.BLOCKED, ()), self.codes(unit))
        conflicts = [item.display for item in unit.diagnostics if item.code == "cable.planned_termination_conflict"]
        self.assertEqual(
            sorted((item["segment_index"], item["competing_segment"], item["termination"]) for item in conflicts),
            [(0, 1, str(self.fronts[1])), (1, 0, str(self.fronts[1]))],
        )
        messages = {item["code"]: item["message"] for item in ReviewWorkspace(plan, self.actor).traces[0].findings}
        self.assertTrue(
            messages["cable.planned_termination_conflict"].startswith("Another segment of this trace"),
            messages["cable.planned_termination_conflict"],
        )

    def test_a_leg_never_joins_a_media_run(self):
        """A breakout Cable fans out, so a copper hop behind a fiber lane states no media contradiction."""
        self.resolve_lanes([1])

        (unit,) = self.plan(panel_leg(1, downstream="Patch")).units

        self.assertEqual(unit.disposition, Disposition.ACTIONABLE)
        self.assertNotIn("cable.media_family_mismatch", self.codes(unit))

    def test_an_occupied_end_marks_the_leg_as_a_conflict(self):
        self.resolve_lanes([1])
        self.connect(self.fronts[1], Interface.objects.create(device=self.make_device("OTHER"), name="eth9", type="lc"))

        plan = self.plan(panel_leg(1))
        (unit,) = plan.units

        (occupied,) = [item for item in unit.diagnostics if item.code == "cable.harness_occupied"]
        segment = ReviewWorkspace(plan, self.actor).traces[0].segments[occupied.display["segment_index"]]
        self.assertEqual((segment["right"], segment["status"]), ("SW-1 0/0:1", "conflict"))

    def viewer(self, *, add_cable=True, interfaces=None, devices=None):
        """Return an operator who views the topology, with the given Interface and Device scopes."""
        grants = [
            (Site, ("view",), {}),
            (Device, ("view",), devices or {}),
            (Interface, ("view",), interfaces or {}),
            (FrontPort, ("view",), {}),
            (RearPort, ("view",), {}),
        ]
        if add_cable:
            grants.append((Cable, ("add",), {}))
        return user_with_object_permission(f"harness-{uuid.uuid4().hex[:8]}", grants)

    def test_an_operator_who_cannot_add_cables_is_blocked_on_a_harness_alone(self):
        self.resolve_lanes([1, 2])
        actor = self.viewer(add_cable=False)

        plan = self.plan(direct_leg(1), direct_leg(2), actor=actor)

        for unit in plan.units:
            self.assertEqual(unit.disposition, Disposition.BLOCKED)
            self.assertIn("cable.permission_denied", self.codes(unit))

    def test_a_parent_the_operator_cannot_view_blocks_every_leg(self):
        self.resolve_lanes([1, 2])
        actor = self.viewer(interfaces={"channels__isnull": True})

        plan = self.plan(direct_leg(1), direct_leg(2), actor=actor)

        for unit in plan.units:
            self.assertEqual(unit.disposition, Disposition.BLOCKED)
            denied = [item.display for item in unit.diagnostics if item.code == "cable.permission_denied"]
            self.assertEqual(denied, [{"permission": "dcim.view_interface"}])
        for trace in ReviewWorkspace(plan, actor).traces:
            with self.subTest(trace=trace.identity):
                self.assertEqual([item["code"] for item in trace.findings], ["cable.permission_denied"])
                self.assertNotIn("channel", " ".join(item["message"] for item in trace.findings).lower())

    def test_a_parent_on_a_chassis_member_the_operator_cannot_view_blocks_every_leg(self):
        """Spec 6.6: the actor views every involved Device, and the parent's Device is one of them."""
        _member, legs = self.build_chassis_harness()
        visible = self.plan(*legs, actor=self.viewer())
        self.assertEqual([unit.disposition for unit in visible.units], [Disposition.ACTIONABLE] * 2)
        actor = self.viewer(devices={"serial": ""})

        plan = self.plan(*legs, actor=actor)

        for unit in plan.units:
            with self.subTest(unit=unit.identity):
                self.assertEqual(unit.disposition, Disposition.BLOCKED)
                self.assertEqual(unit.changes, ())
                denied = [item.display for item in unit.diagnostics if item.code == "cable.permission_denied"]
                self.assertEqual(denied, [{"permission": "dcim.view_device"}])
                self.assertNotIn("cable.harness_state", self.codes(unit))

    def test_a_hidden_parent_still_moves_the_fingerprint_when_its_channels_change(self):
        self.resolve_lanes([1, 2])
        actor = self.viewer(interfaces={"channels__isnull": True})
        before = self.plan(direct_leg(1), direct_leg(2), actor=actor).units[0]
        Interface.objects.filter(pk=self.parent.pk).update(channels=8)

        after = self.plan(direct_leg(1), direct_leg(2), actor=actor).units[0]

        self.assertEqual(after.disposition, Disposition.BLOCKED)
        self.assertNotEqual(fingerprint_of(before.fingerprint_data), fingerprint_of(after.fingerprint_data))

    def test_a_cached_plan_states_no_channel_facts_to_a_viewer_who_cannot_view_the_parent(self):
        """The planner could view the parent; the viewer lost that right before the preview renders."""
        self.resolve_lanes([1, 2])
        plan = self.plan(direct_leg(1), direct_leg(2))
        self.assertIn(
            "of 4", " ".join(item["message"] for item in ReviewWorkspace(plan, self.actor).traces[0].findings)
        )
        viewer = self.viewer(interfaces={"channels__isnull": True})

        self.assert_every_harness_finding_hidden(plan, viewer)

    def assert_every_harness_finding_hidden(self, plan, viewer):
        """Assert that a viewer who cannot see one port of the harness reads no harness finding (section 6.6)."""
        for unit in present_units(plan.units, viewer):
            codes = [item.code for item in unit.diagnostics]
            self.assertIn(FINDING_HIDDEN, codes)
            self.assertFalse([code for code in codes if code.startswith("cable.harness_")], codes)
        for trace in ReviewWorkspace(plan, viewer).traces:
            with self.subTest(trace=trace.identity):
                wording = " ".join(item["message"] for item in trace.findings).lower()
                self.assertNotIn("breakout", wording)
                self.assertNotIn("channel", wording)

    def test_a_leg_policy_row_names_the_breakout_profile_unless_the_parent_is_hidden(self):
        self.resolve_lanes([1, 2])
        plan = self.plan(direct_leg(1), direct_leg(2))
        viewer = self.viewer(interfaces={"channels__isnull": True})

        for actor, expected in (
            (self.actor, f"{BREAKOUT_PROFILE}: {cable_profile_label(BREAKOUT_4)}"),
            (viewer, BREAKOUT_PROFILE),
        ):
            with self.subTest(actor=actor.username):
                policies = ReviewWorkspace(plan, actor).traces[0].cable_policies
                self.assertEqual(
                    [(item["cable_class"], item["breakout_profile"]) for item in policies], [(FIBER, expected)]
                )

    def test_a_leg_policy_row_names_no_profile_when_the_channel_count_has_none(self):
        parent = self.make_parent(self.make_device("SW-3"), "1/1", 3)
        lanes = [lane(number, device="SW-3") for number in range(1, 4)]
        for number, reference in enumerate(lanes, start=1):
            self.save_resolution(reference, self.make_child(parent, number))

        plan = self.plan(*(direct_leg(number, start=reference) for number, reference in enumerate(lanes, start=1)))

        self.assertIn("cable.harness_profile_unavailable", self.codes(plan.units[0]))
        (policy,) = ReviewWorkspace(plan, self.actor).traces[0].cable_policies
        self.assertEqual(policy["breakout_profile"], BREAKOUT_PROFILE)

    def test_a_cached_plan_states_no_channel_facts_to_a_viewer_who_cannot_view_the_parent_device(self):
        """The viewer keeps Interface view and loses only the chassis member that holds the parent."""
        _member, legs = self.build_chassis_harness()
        plan = self.plan(*legs)
        self.assertEqual([unit.disposition for unit in plan.units], [Disposition.ACTIONABLE] * 2)
        viewer = self.viewer(devices={"serial": ""})

        self.assert_every_harness_finding_hidden(plan, viewer)

    def test_a_cached_refusal_states_no_channel_count_to_a_viewer_who_cannot_view_the_parent(self):
        parent = self.make_parent(self.make_device("SW-3"), "1/1", 3)
        for number in range(1, 4):
            self.save_resolution(lane(number, device="SW-3"), self.make_child(parent, number))
        viewer = self.viewer(interfaces={"channels__isnull": True})
        for code, numbers in (("cable.harness_channel_gap", (1, 3)), ("cable.harness_profile_unavailable", (1, 2, 3))):
            with self.subTest(code=code):
                plan = self.plan(*(direct_leg(n, start=lane(n, device="SW-3")) for n in numbers))
                self.assertIn(code, self.codes(plan.units[0]))

                self.assert_every_harness_finding_hidden(plan, viewer)

    def test_a_hidden_parent_does_not_disclose_that_its_cable_is_the_harness(self):
        """A visible leg reads no companion once its harness exists, so a hidden one must always name them."""
        self.resolve_lanes(range(1, 5))
        self.build_existing_harness()
        actor = self.viewer(interfaces={"channels__isnull": True})

        plan = self.plan(*(panel_leg(number) for number in range(1, 5)), actor=actor)

        identities = {unit.identity for unit in plan.units}
        for unit in plan.units:
            with self.subTest(unit=unit.identity):
                self.assertEqual(unit.disposition, Disposition.BLOCKED)
                self.assertNotIn("cable.segment_reused", self.codes(unit))
                self.assertEqual(set(unit.selected_with), identities - {unit.identity})

    def test_a_cable_the_operator_cannot_view_is_not_disclosed(self):
        self.resolve_lanes([1, 2])
        hidden = self.connect(
            self.parent,
            Interface.objects.create(device=self.make_device("OTHER"), name="eth9", type="100gbase-x-qsfp28"),
        )
        actor = self.viewer()

        plan = self.plan(direct_leg(1), direct_leg(2), actor=actor)

        (occupied,) = [item for item in plan.units[0].diagnostics if item.code == "cable.harness_occupied"]
        self.assertIs(occupied.display["cable_visible"], False)
        self.assertNotIn("cable", occupied.display)
        self.assertNotIn(f"dcim.cable:{hidden.pk}", occupied.identities)

    def test_a_lane_that_leads_the_canonical_order_cables_its_far_end(self):
        """AA sorts before SRV, so the channel child is the left end of the first segment."""
        switch = self.make_device("AA-1")
        parent = self.make_parent(switch, "1/1", 2)
        for number in (1, 2):
            self.save_resolution(lane(number, device="AA-1"), self.make_child(parent, number))

        plan = self.plan(*(direct_leg(number, start=lane(number, device="AA-1")) for number in (1, 2)))

        self.assertEqual([unit.disposition for unit in plan.units], [Disposition.ACTIONABLE] * 2)
        (change,) = {change for unit in plan.units for change in self.harness_changes(unit)}
        fars = [far for _child, far in change.to_dict()["payload"]["harness"]["legs"]]
        self.assertEqual(fars, [["dcim.interface", self.servers[n].pk, self.servers[n].device_id] for n in (1, 2)])


# A trace runs from its lower endpoint, SRV before SW, so the leg from the lane is the last segment.
DOWNSTREAM, LEG = 0, 1
# Interface view scopes: the parent alone holds `channels`, and only its children hold `channel_id`.
PARENT_HIDDEN = {"channels__isnull": True}
PARENT_AND_CHILDREN_HIDDEN = {"channels__isnull": True, "channel_id__isnull": True}


def segment_item(page, index):
    """Return the Proposed physical topology entry of one segment on a rendered trace card."""
    return page.content.decode().split(f'data-trace-segment="{index}">', 1)[1].split("</li>", 1)[0]


def segment_policy(page, index):
    """Return the policy line one segment entry shows above its override form."""
    return segment_item(page, index).split("data-segment-policy>", 1)[1].split("</div>", 1)[0]


def policy_row(page, cable_class):
    """Return the Cable policy table row of one CableClass on a rendered trace card."""
    return page.content.decode().split(f'<tr data-cable-class="{cable_class}">', 1)[1].split("</tr>", 1)[0]


@CHANNELS
class HarnessPolicyRowTest(HarnessTopologyMixin, TestCase):
    """The breakout sets a leg's Cable Profile, so neither the leg's policy row nor its override offers one."""

    @classmethod
    def setUpTestData(cls):
        cls.build_harness_topology()

    def setUp(self):
        self.resolve_lanes(range(1, 5))
        self.client.force_login(self.actor)

    def cards(self, *blocks):
        """Upload the blocks and return each trace card, keyed by the trace's first endpoint."""
        upload = BytesIO(trace_workbook_bytes(path_blocks=blocks))
        upload.name = "traces.xlsx"
        upload_preview(self.client, {"profile": self.profile.pk, "site": self.site.pk, "excel_file": upload})
        url = reverse("plugins:netbox_data_import:trace_workspace")
        traces = self.client.get(url).context["traces"]
        return {trace.endpoints["from"]: self.client.get(url, {"trace": trace.identity}) for trace in traces}

    def test_a_leg_row_shows_the_breakout_profile_and_offers_only_the_cable_type(self):
        cards = self.cards(*(panel_leg(number, downstream="Patch") for number in range(1, 5)), direct_path())

        leg = policy_row(cards["SW-1 Slot 0 >> 0/0 (10) P01"], FIBER)
        self.assertIn(f"{BREAKOUT_PROFILE}: {cable_profile_label(BREAKOUT_4)}", leg)
        self.assertIn('<select name="cable_type"', leg)
        self.assertNotIn('<select name="cable_profile"', leg)
        # A save keeps the stored profile, which an ordinary segment of another trace can still use.
        self.assertIn('<input type="hidden" name="cable_profile" value="single-1c1p"', leg)
        downstream = policy_row(cards["SW-1 Slot 0 >> 0/0 (10) P01"], "Patch")
        self.assertIn('<select name="cable_profile"', downstream)
        self.assertNotIn(BREAKOUT_PROFILE, downstream)
        ordinary = cards["DEV-A eth0"]
        self.assertNotContains(ordinary, BREAKOUT_PROFILE)
        self.assertIn('<select name="cable_profile"', policy_row(ordinary, "Patch"))

    def test_a_class_that_also_serves_an_ordinary_segment_keeps_its_profile_select(self):
        cards = self.cards(*(panel_leg(number) for number in range(1, 5)))

        card = cards["SW-1 Slot 0 >> 0/0 (10) P01"]
        self.assertNotIn(BREAKOUT_PROFILE, policy_row(card, FIBER))
        self.assertIn('<select name="cable_profile"', policy_row(card, FIBER))

    def test_a_leg_segment_shows_the_breakout_profile_and_offers_only_a_cable_type_override(self):
        cards = self.cards(*(panel_leg(number, downstream="Patch") for number in range(1, 5)), direct_path())

        card = cards["SW-1 Slot 0 >> 0/0 (10) P01"]
        self.assertIn(f"{BREAKOUT_PROFILE}: {cable_profile_label(BREAKOUT_4)}", segment_policy(card, LEG))
        self.assertNotIn("1C1P", segment_policy(card, LEG))
        self.assertIn('<select name="cable_type"', segment_item(card, LEG))
        self.assertNotIn('name="cable_profile"', segment_item(card, LEG))
        self.assertNotIn("Cable Profile for segment", segment_item(card, LEG))
        self.assertNotIn("cable_profile", card.context["segment_policy_forms"][LEG]["form"].fields)
        self.assertIn('<select name="cable_profile"', segment_item(card, DOWNSTREAM))
        self.assertNotIn(BREAKOUT_PROFILE, segment_item(card, DOWNSTREAM))
        self.assertNotContains(cards["DEV-A eth0"], BREAKOUT_PROFILE)

    def force_type(self, page, cable_type, **extra):
        """Post the leg segment's override form as the card renders it: a Cable Type and no profile."""
        return self.client.post(
            reverse("plugins:netbox_data_import:trace_segment_policy"),
            {
                **preview_claim(self.client),
                "trace": page.context["selected_trace"].identity,
                "segment": str(LEG),
                "cable_type": cable_type,
                **extra,
            },
        )

    def store_leg_override(self, blocks, lane_port="P01"):
        """Store an override with a Cable Profile on one lane's leg segment, and return its segment key."""
        legs = {
            unit.display["trace"]["endpoints"]["from"]: unit.display["trace"]["segments"][LEG]
            for unit in self.plan(*blocks).units
        }
        segment_key = legs[f"SW-1 Slot 0 >> 0/0 (10) {lane_port}"]["segment_key"]
        CableSegmentOverride.objects.create(
            profile=self.profile,
            segment_key=segment_key,
            cable_type="mmf-om4",
            cable_profile="single-1c1p",
            source_trace_identity="stored",
            segment_index=LEG,
        )
        return segment_key

    @staticmethod
    def operator(interfaces):
        """Return an operator who views and edits the profile's policies, and the Interfaces *interfaces* admits."""
        return user_with_object_permission(
            "trace-operator",
            [
                (ImportProfile, ("view", "change"), {}),
                (Site, ("view",), {}),
                (Device, ("view",), {}),
                (Interface, ("view",), interfaces),
                (FrontPort, ("view",), {}),
                (RearPort, ("view",), {}),
                (Cable, ("view", "add", "delete"), {}),
                (CableClassMapping, ("view", "add", "change", "delete"), {}),
                (CableSegmentOverride, ("view", "add", "change", "delete"), {}),
            ],
        )

    def test_a_leg_planned_by_an_operator_who_cannot_view_the_parent_stays_a_leg(self):
        """Being a leg is a fact of the visible channel child, so the page keeps it and states no parent fact."""
        blocks = [panel_leg(number, downstream="Patch") for number in range(1, 5)]
        segment_key = self.store_leg_override(blocks)
        self.client.force_login(self.operator(PARENT_HIDDEN))

        card = self.cards(*blocks)["SW-1 Slot 0 >> 0/0 (10) P01"]

        self.assertIn("cable.permission_denied", [item["code"] for item in card.context["selected_trace"].findings])
        self.assertIn(BREAKOUT_PROFILE, segment_policy(card, LEG))
        self.assertNotIn(f"{BREAKOUT_PROFILE}:", segment_policy(card, LEG))
        self.assertNotIn("1C1P", segment_policy(card, LEG))
        self.assertNotIn('name="cable_profile"', segment_item(card, LEG))
        self.assertIn(BREAKOUT_PROFILE, policy_row(card, FIBER))
        self.assertNotIn('<select name="cable_profile"', policy_row(card, FIBER))
        response = self.force_type(card, "mmf-om3", cable_profile="__explicit_none__")
        self.assertEqual(response.status_code, 302, response.content[:300])
        row = CableSegmentOverride.objects.get(profile=self.profile, segment_key=segment_key)
        self.assertEqual((row.cable_type, row.cable_profile), ("mmf-om3", "single-1c1p"))

    @staticmethod
    def restrict_interfaces(operator, constraints, model=Interface):
        """Replace the operator's view scope of one model, as an administrator does after the preview exists."""
        from users.models import ObjectPermission

        from netbox_data_import.object_permissions import clear_user_permission_caches

        permission = ObjectPermission.objects.get(name__startswith=f"trace-operator {model.__name__} ")
        permission.constraints = constraints
        permission.save(update_fields=("constraints",))
        clear_user_permission_caches(operator)

    @staticmethod
    def all_but(model, hidden):
        """Return a view scope of every row of *model* except *hidden*."""
        return {"id__in": list(model.objects.exclude(pk=hidden.pk).values_list("pk", flat=True))}

    def reload(self, page):
        """Render the trace of *page* again from the cached preview, as the viewer's next request does."""
        identity = page.context["selected_trace"].identity
        return self.client.get(reverse("plugins:netbox_data_import:trace_workspace"), {"trace": identity})

    @staticmethod
    def messages(page):
        """Return the finding wording the selected trace renders."""
        return [item["message"] for item in page.context["selected_trace"].findings]

    def test_a_finding_about_a_sibling_child_hides_with_that_child(self):
        """Lane 1's trace reports that child 2 does not mirror the parent; a viewer of child 1 alone sees no such fact."""
        self.build_existing_harness()
        Interface.objects.filter(pk=self.children[2].pk).update(cable_positions=[3])
        operator = self.operator({})
        self.client.force_login(operator)
        card = self.cards(*(panel_leg(number) for number in range(1, 5)))["SW-1 Slot 0 >> 0/0 (10) P01"]
        self.assertTrue(any("does not mirror" in message for message in self.messages(card)))

        self.restrict_interfaces(operator, self.all_but(Interface, self.children[2]))
        card = self.reload(card)

        self.assertFalse(any("does not mirror" in message for message in self.messages(card)))
        self.assertIn(_DIAGNOSTIC_MESSAGES[FINDING_HIDDEN], self.messages(card))
        self.assertNotContains(card, "does not mirror")

    def virtual_far_end_card(self):
        """Return lane 1's card in a preview where lane 3 reaches a virtual Interface, which NetBox cannot cable."""
        Interface.objects.filter(pk=self.servers[3].pk).update(type="virtual")
        operator = self.operator({})
        self.client.force_login(operator)
        card = self.cards(*(direct_leg(number) for number in range(1, 5)))["SW-1 Slot 0 >> 0/0 (10) P01"]
        self.assertIn(_DIAGNOSTIC_MESSAGES["cable.termination_not_connectable"], self.messages(card))
        return operator, card

    def test_a_harness_refusal_hides_with_any_port_of_the_harness(self):
        """The refused far end and lane 1's own child are both ports of the harness, so either one hides it."""
        operator, card = self.virtual_far_end_card()

        for hidden in (self.servers[3], self.children[1]):
            with self.subTest(hidden=str(hidden)):
                self.restrict_interfaces(operator, self.all_but(Interface, hidden))

                messages = self.messages(self.reload(card))

                self.assertNotIn(_DIAGNOSTIC_MESSAGES["cable.termination_not_connectable"], messages)
                self.assertIn(_DIAGNOSTIC_MESSAGES[FINDING_HIDDEN], messages)

    def test_a_trace_between_two_channel_children_hides_its_finding_with_either_child(self):
        """The unsupported trace names child 2 as well as child 1, and its finding reaches the other legs too."""
        between = (
            trace_endpoint_line(lane(1)),
            trace_endpoint_line(lane(2)),
            (trace_segment(lane(1), FIBER, lane(2)),),
        )
        operator = self.operator({})
        self.client.force_login(operator)
        cards = self.cards(between, panel_leg(3), panel_leg(4))
        unsupported, other_leg = cards["SW-1 Slot 0 >> 0/0 (10) P01"], cards["SW-1 Slot 0 >> 0/0 (10) P03"]
        for page in (unsupported, other_leg):
            self.assertTrue(any("joins two channel subinterfaces" in message for message in self.messages(page)))

        for page, hidden in ((unsupported, [self.children[2]]), (other_leg, [self.children[1], self.children[2]])):
            with self.subTest(trace=page.context["selected_trace"].name, hidden=[str(item) for item in hidden]):
                visible = Interface.objects.exclude(pk__in=[item.pk for item in hidden])
                self.restrict_interfaces(operator, {"id__in": list(visible.values_list("pk", flat=True))})

                messages = self.messages(self.reload(page))

                self.assertFalse(any("channel" in message.lower() for message in messages), messages)
                self.assertIn(_DIAGNOSTIC_MESSAGES[FINDING_HIDDEN], messages)

    def test_one_hidden_far_end_hides_the_harness_on_every_leg(self):
        """A healthy harness is one disclosure unit: losing one far end hides its findings and its breakout heading."""
        operator = self.operator({})
        self.client.force_login(operator)
        cards = self.cards(*(direct_leg(number) for number in range(1, 5)))
        for page in cards.values():
            self.assertContains(page, "breaks out as one Cable")
            self.assertContains(page, 'data-sync-member="companion"', count=3)

        self.restrict_interfaces(operator, self.all_but(Interface, self.servers[4]))

        for name, page in cards.items():
            with self.subTest(trace=name):
                card = self.reload(page)
                self.assertNotContains(card, "breaks out as one Cable")
                self.assertNotContains(card, SYNC_COMPANIONS)
                self.assertContains(card, SYNC_TOGETHER)
                self.assertContains(card, 'data-sync-member="together"', count=3)

    def test_a_blocking_trace_hides_the_harness_with_its_own_ports(self):
        """A trace that names the parent as an ordinary port joins the harness, so its far end hides the harness too."""
        uplink, other = (
            trace_termination("SW-1", "Uplinks", "0/0", "Port"),
            trace_termination("OTHER", "", "eth9", "NIC"),
        )
        eth9 = Interface.objects.create(device=self.make_device("OTHER"), name="eth9", type="100gbase-x-qsfp28")
        rival = (trace_endpoint_line(uplink), trace_endpoint_line(other), (trace_segment(uplink, FIBER, other),))
        operator = self.operator({})
        self.client.force_login(operator)
        card = self.cards(*(panel_leg(number) for number in range(1, 5)), rival)["SW-1 Slot 0 >> 0/0 (10) P01"]
        self.assertTrue(any("names the parent as an ordinary port" in message for message in self.messages(card)))

        self.restrict_interfaces(operator, self.all_but(Interface, eth9))

        messages = self.messages(self.reload(card))
        self.assertFalse(any("ordinary port" in message for message in messages), messages)
        self.assertIn(_DIAGNOSTIC_MESSAGES[FINDING_HIDDEN], messages)

    def test_a_conflict_that_only_the_harness_claims_hides_with_any_port_of_the_harness(self):
        """Lane 1's trace never names SRV-2, but its harness claims SRV-2, which an ordinary trace also plans."""
        alias = trace_termination("SRV-2", "", "eth0-alias", "NIC")
        self.save_resolution(alias, self.servers[2])
        operator = self.operator({})
        self.client.force_login(operator)
        cards = self.cards(direct_leg(1), direct_leg(2), direct_path(DEVICE_A, alias))
        leg, ordinary = cards["SW-1 Slot 0 >> 0/0 (10) P01"], cards["DEV-A eth0"]
        conflict = "Another Source Trace plans a Cable on this termination"
        for page in (leg, ordinary):
            self.assertTrue(any(conflict in message for message in self.messages(page)), self.messages(page))

        for model, hidden in (
            (Interface, self.parent),
            (Interface, self.children[2]),
            (Interface, self.servers[2]),
            (Device, self.servers[2].device),
        ):
            with self.subTest(hidden=f"{model.__name__} {hidden}"):
                self.restrict_interfaces(operator, {}, model=Interface)
                self.restrict_interfaces(operator, {}, model=Device)
                self.restrict_interfaces(operator, self.all_but(model, hidden), model=model)

                for page in (leg, ordinary):
                    messages = self.messages(self.reload(page))
                    self.assertFalse(any(conflict in message for message in messages), messages)
                    self.assertIn(_DIAGNOSTIC_MESSAGES[FINDING_HIDDEN], messages)

    def test_an_occupied_parent_hides_its_conflict_badge_with_the_parent(self):
        """The badge repeats the hidden cable.harness_occupied finding, so it hides with it."""
        Interface.objects.filter(pk=self.parent.pk).update(mark_connected=True)
        operator = self.operator({})
        self.client.force_login(operator)
        card = self.cards(*(direct_leg(number) for number in range(1, 5)))["SW-1 Slot 0 >> 0/0 (10) P01"]
        badge = '<span class="badge ndi-trace-status-conflict">conflict</span>'
        self.assertIn(badge, segment_item(card, 0))

        for model, hidden in ((Interface, self.parent), (Device, self.parent.device)):
            with self.subTest(hidden=f"{model.__name__} {hidden}"):
                self.restrict_interfaces(operator, {}, model=Interface)
                self.restrict_interfaces(operator, {}, model=Device)
                self.restrict_interfaces(operator, self.all_but(model, hidden), model=model)

                card = self.reload(card)

                self.assertNotIn(badge, segment_item(card, 0))
                self.assertIn(_DIAGNOSTIC_MESSAGES[FINDING_HIDDEN], self.messages(card))

    def test_a_refused_far_end_leaves_attention_when_its_harness_finding_hides(self):
        """The far end needs attention only because of the harness refusal, so it settles once that refusal hides."""
        operator = self.operator({})
        self.client.force_login(operator)
        Interface.objects.filter(pk=self.servers[3].pk).update(type="virtual")
        card = self.cards(*(direct_leg(number) for number in range(1, 5)))["SW-1 Slot 0 >> 0/0 (10) P03"]
        self.assertIn("SRV-3 eth0", [item["label"] for item in card.context["attention_terminations"]])

        self.restrict_interfaces(operator, self.all_but(Interface, self.parent))

        card = self.reload(card)
        self.assertNotIn("SRV-3 eth0", [item["label"] for item in card.context["attention_terminations"]])

    def test_a_reused_harness_states_no_reuse_once_one_port_of_it_hides(self):
        """The leg's reuse is a fact of the harness, so its note, badge, count and retained lock hide with any port."""
        self.build_existing_harness()
        operator = self.operator({})
        self.client.force_login(operator)
        card = self.cards(*(panel_leg(number) for number in range(1, 5)))["SW-1 Slot 0 >> 0/0 (10) P01"]
        reused = _DIAGNOSTIC_MESSAGES["cable.segment_reused"]
        badge = '<span class="badge ndi-trace-status-reuse">reuse existing</span>'
        self.assertIn(reused, self.messages(card))
        self.assertIn(badge, segment_item(card, LEG))
        self.assertIn("1 reuse existing", card.context["topology_summary"])

        for model, hidden in ((Interface, self.parent), (Device, self.parent.device), (Interface, self.children[2])):
            with self.subTest(hidden=f"{model.__name__} {hidden}"):
                self.restrict_interfaces(operator, {}, model=Interface)
                self.restrict_interfaces(operator, {}, model=Device)
                self.restrict_interfaces(operator, self.all_but(model, hidden), model=model)

                card = self.reload(card)

                self.assertNotIn(reused, self.messages(card))
                self.assertNotIn(badge, segment_item(card, LEG))
                self.assertNotIn("reuse", card.context["topology_summary"])
                self.assertNotIn(RETAINED_SEGMENT_REASON, segment_item(card, LEG))
                # The plan still keeps the Cable, so a save is refused without saying why.
                refused = self.client.get(self.force_type(card, "mmf-om3")["Location"])
                self.assertContains(refused, SEGMENT_OUT_OF_VIEW)
                self.assertNotContains(refused, RETAINED_SEGMENT_REASON)
                self.assertFalse(CableSegmentOverride.objects.exists())

    def test_one_far_end_device_hides_the_harness(self):
        operator = self.operator({})
        self.client.force_login(operator)
        card = self.cards(*(direct_leg(number) for number in range(1, 5)))["SW-1 Slot 0 >> 0/0 (10) P01"]

        self.restrict_interfaces(operator, self.all_but(Device, self.servers[4].device), model=Device)

        card = self.reload(card)
        self.assertNotContains(card, "breaks out as one Cable")
        self.assertIn(_DIAGNOSTIC_MESSAGES[FINDING_HIDDEN], self.messages(card))

    def test_a_refusal_of_a_far_end_hides_with_its_device_alone(self):
        operator, card = self.virtual_far_end_card()

        self.restrict_interfaces(operator, self.all_but(Device, self.servers[3].device), model=Device)

        messages = self.messages(self.reload(card))
        self.assertNotIn(_DIAGNOSTIC_MESSAGES["cable.termination_not_connectable"], messages)
        self.assertIn(_DIAGNOSTIC_MESSAGES[FINDING_HIDDEN], messages)

    def virtual_downstream_card(self):
        """Return lane 1's card in a three-lane preview whose ordinary downstream segment reaches a virtual Interface."""
        Interface.objects.filter(pk=self.servers[1].pk).update(type="virtual")
        operator = self.operator({})
        self.client.force_login(operator)
        return operator, self.cards(*(panel_leg(number) for number in range(1, 4)))["SW-1 Slot 0 >> 0/0 (10) P01"]

    def test_an_ordinary_segment_refusal_stays_when_only_the_channel_child_is_hidden(self):
        """The downstream refusal says nothing of the breakout, so hiding the leg's child leaves it readable."""
        operator, card = self.virtual_downstream_card()
        self.restrict_interfaces(operator, PARENT_AND_CHILDREN_HIDDEN)

        messages = self.messages(self.reload(card))

        self.assertIn(_DIAGNOSTIC_MESSAGES["cable.termination_not_connectable"], messages)
        self.assertIn(_DIAGNOSTIC_MESSAGES[FINDING_HIDDEN], messages)

    def test_hidden_findings_keep_the_highest_severity_they_replace(self):
        """A warning, an info and an error that all become one hidden-finding line keep the error."""
        operator, card = self.virtual_downstream_card()
        codes = [item["code"] for item in card.context["selected_trace"].findings]
        self.assertLess(codes.index("cable.harness_partial"), codes.index("cable.termination_not_connectable"))
        self.restrict_interfaces(
            operator, {"channels__isnull": True, "channel_id__isnull": True, "type": "25gbase-x-sfp28"}
        )

        card = self.reload(card)

        hidden = [item for item in card.context["selected_trace"].findings if item["code"] == FINDING_HIDDEN]
        self.assertEqual([item["severity"] for item in hidden], [Severity.ERROR])
        findings = card.content.decode().split("data-trace-findings>", 1)[1].split("</div>", 1)[0]
        self.assertNotIn("data-finding-severity", findings)

    def test_a_cached_leg_mark_is_hidden_with_its_channel_child_and_still_keeps_the_stored_profile(self):
        """The viewer loses the child after planning; the page drops the mark, and the save still ignores a profile."""
        blocks = [panel_leg(number, downstream="Patch") for number in range(1, 5)]
        segment_key = self.store_leg_override(blocks)
        operator = self.operator(PARENT_HIDDEN)
        self.client.force_login(operator)
        trace = self.cards(*blocks)["SW-1 Slot 0 >> 0/0 (10) P01"].context["selected_trace"].identity
        self.restrict_interfaces(operator, PARENT_AND_CHILDREN_HIDDEN)

        card = self.client.get(reverse("plugins:netbox_data_import:trace_workspace"), {"trace": trace})

        self.assertEqual(card.status_code, 200)
        self.assertIn(TERMINATION_HIDDEN, segment_item(card, LEG))
        self.assertNotContains(card, BREAKOUT_PROFILE)
        self.assertNotContains(card, "data-breakout-profile")
        self.assertEqual(card.context["segment_policy_forms"][LEG]["breakout_leg"], "")
        response = self.force_type(card, "mmf-om3", cable_profile="__explicit_none__")
        self.assertEqual(response.status_code, 302, response.content[:300])
        row = CableSegmentOverride.objects.get(profile=self.profile, segment_key=segment_key)
        self.assertEqual((row.cable_type, row.cable_profile), ("mmf-om3", "single-1c1p"))

    def test_a_cached_preview_states_no_leg_fact_once_the_parent_and_its_children_are_hidden(self):
        """Every output that says a trace is a leg names the leg's channel child, so it hides with that child."""
        blocks = [panel_leg(number, downstream="Patch") for number in range(1, 5)]
        segment_key = self.store_leg_override(blocks)
        operator = self.operator({})
        self.client.force_login(operator)
        visible = self.cards(*blocks)["SW-1 Slot 0 >> 0/0 (10) P01"]
        self.assertContains(visible, SYNC_COMPANIONS)
        self.assertContains(visible, "breaks out as one Cable")
        self.restrict_interfaces(operator, PARENT_AND_CHILDREN_HIDDEN)

        card = self.client.get(
            reverse("plugins:netbox_data_import:trace_workspace"), {"trace": visible.context["selected_trace"].identity}
        )

        self.assertEqual(card.status_code, 200)
        page = card.content.decode()
        self.assertNotIn("breakout", page.lower())
        self.assertNotIn("channel", page.lower())
        self.assertNotIn('data-sync-member="companion"', page)
        self.assertEqual(page.count(_DIAGNOSTIC_MESSAGES[FINDING_HIDDEN]), 1)
        self.assertContains(card, SYNC_TOGETHER)
        self.assertContains(card, 'data-sync-member="together"', count=3)
        self.assertEqual(page.split('data-trace-action="sync"')[0].count('name="selection"'), 4)
        response = self.force_type(card, "mmf-om3", cable_profile="__explicit_none__")
        self.assertEqual(response.status_code, 302, response.content[:300])
        row = CableSegmentOverride.objects.get(profile=self.profile, segment_key=segment_key)
        self.assertEqual((row.cable_type, row.cable_profile), ("mmf-om3", "single-1c1p"))

    def test_a_leg_type_override_keeps_a_stored_profile_and_never_shows_it(self):
        blocks = [panel_leg(number, downstream="Patch") for number in range(1, 5)]
        legs = {
            unit.display["trace"]["endpoints"]["from"]: unit.display["trace"]["segments"][LEG]
            for unit in self.plan(*blocks).units
        }
        first, second = legs["SW-1 Slot 0 >> 0/0 (10) P01"], legs["SW-1 Slot 0 >> 0/0 (10) P02"]
        CableSegmentOverride.objects.create(
            profile=self.profile,
            segment_key=first["segment_key"],
            cable_type="mmf-om4",
            cable_profile="single-1c1p",
            source_trace_identity="stored",
            segment_index=LEG,
        )
        (change,) = {change for unit in self.plan(*blocks).units for change in self.harness_changes(unit)}
        self.assertEqual(change.to_dict()["payload"]["harness"]["profile"], BREAKOUT_4)
        cards = self.cards(*blocks)
        stored, fresh = cards["SW-1 Slot 0 >> 0/0 (10) P01"], cards["SW-1 Slot 0 >> 0/0 (10) P02"]
        self.assertIn("forced", segment_policy(stored, LEG))
        self.assertIn(BREAKOUT_PROFILE, segment_policy(stored, LEG))
        self.assertNotIn("1C1P", segment_policy(stored, LEG))

        for page, segment_key in ((stored, first["segment_key"]), (fresh, second["segment_key"])):
            with self.subTest(segment_key=segment_key):
                response = self.force_type(page, "mmf-om3")

                self.assertEqual(response.status_code, 302, response.content[:300])
                row = CableSegmentOverride.objects.get(profile=self.profile, segment_key=segment_key)
                self.assertEqual(row.cable_type, "mmf-om3")
        # Planning ignores a leg's profile, so a save keeps a stored one and a new override decides none.
        self.assertEqual(
            dict(CableSegmentOverride.objects.values_list("segment_key", "cable_profile")),
            {first["segment_key"]: "single-1c1p", second["segment_key"]: None},
        )
