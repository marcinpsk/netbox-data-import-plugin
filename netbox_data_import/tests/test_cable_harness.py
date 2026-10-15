# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Plan and write breakout harnesses from leg traces through the real adapter, engine and database."""

import uuid
from unittest import skipUnless

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
from django.db import transaction
from django.db.models.signals import pre_save
from django.test import TestCase, TransactionTestCase

from netbox_data_import.cable_disclosure import TERMINATION_HIDDEN, present_units
from netbox_data_import.cable_harness import IntentRefused, harness_intent, ordinary_intent
from netbox_data_import.cable_target import CableModule
from netbox_data_import.import_engine import ImportEngine, SelectionError, StalePlan
from netbox_data_import.models import CableClassMapping, CableImportSource, ImportExecution
from netbox_data_import.plan import Disposition, PlannedChange, Severity, fingerprint_of
from netbox_data_import.review_workspace import ReviewWorkspace
from netbox_data_import.target_runtime import ExecutionContext, PreconditionFailed
from netbox_data_import.tests.helpers import (
    cables_on,
    competing_write_during,
    run_on_separate_connection,
    trace_endpoint_line,
    trace_segment,
    trace_termination,
    user_with_object_permission,
)
from netbox_data_import.tests.test_cable_module import CableTopologyMixin, patched_path


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


def termination_snapshot(cable):
    """Return every CableTermination row of one Cable as side, connector, positions, model and object id."""
    return sorted(
        (row.cable_end, row.connector, list(row.positions or ()), row.termination_type.model, row.termination_id)
        for row in CableTermination.objects.filter(cable=cable).select_related("termination_type")
    )


class HarnessTopologyMixin(CableTopologyMixin):
    """Add one channelized switch port, a four-port panel and four servers to the cable topology."""

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

    def test_an_ordinary_write_takes_a_parent_and_refuses_its_child(self):
        self.assertEqual(ordinary_intent(self.parent, self.fronts[1], profile="").a_terminations, (self.parent,))
        with self.assertRaises(IntentRefused):
            ordinary_intent(self.fronts[1], self.children[1], profile="")

    def test_legs_given_out_of_order_take_the_connector_of_their_channel(self):
        intent = harness_intent(self.parent, [(self.children[2], self.fronts[2]), (self.children[1], self.fronts[1])])

        self.assertEqual(intent.b_terminations, (self.fronts[1], self.fronts[2]))
        self.assertEqual((intent.profile, intent.trailing), (BREAKOUT_4, (3, 4)))

    def test_a_far_end_that_is_a_channel_child_is_unsupported(self):
        self.assertEqual(self.refusal([(self.children[1], self.children[2])]), "cable.harness_unsupported")

    def test_one_far_end_on_two_channels_is_a_leg_conflict(self):
        legs = [(self.children[1], self.fronts[1]), (self.children[2], self.fronts[1])]

        self.assertEqual(self.refusal(legs), "cable.harness_leg_conflict")

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

        for unit in plan.units:
            with self.subTest(unit=unit.identity):
                self.assertEqual(unit.disposition, Disposition.BLOCKED)
                self.assertIn("cable.planned_termination_conflict", self.codes(unit))

    def test_a_panel_return_to_the_parent_conflicts_with_the_harness_of_its_own_trace(self):
        """The harness cables the parent to F1, and the segment back from R1 claims the parent as well."""
        self.resolve_lanes([1])
        uplink = trace_termination("SW-1", "Uplinks", "0/0", "Port")
        loop = (
            trace_endpoint_line(lane(1)),
            trace_endpoint_line(uplink),
            (trace_segment(lane(1), FIBER, panel_front(1)), trace_segment(panel_rear(1), FIBER, uplink)),
        )

        plan = self.plan(loop)

        (unit,) = plan.units
        self.assertEqual(unit.disposition, Disposition.BLOCKED, self.codes(unit))
        self.assertEqual(unit.changes, ())
        conflicts = [item.display for item in unit.diagnostics if item.code == "cable.planned_termination_conflict"]
        self.assertEqual(
            sorted((item["segment_index"], item["competing_segment"], item["termination"]) for item in conflicts),
            [(0, 1, str(self.parent)), (1, 0, str(self.parent))],
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

        (unit,) = self.plan(panel_leg(1)).units

        (occupied,) = [item for item in unit.diagnostics if item.code == "cable.harness_occupied"]
        segment = unit.display["trace"]["segments"][occupied.display["segment_index"]]
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

        for unit in present_units(plan.units, viewer):
            displays = {item.code: item.display for item in unit.diagnostics}
            self.assertEqual(
                {key: displays["cable.harness_state"][key] for key in ("channels", "legs", "profile")},
                {"channels": TERMINATION_HIDDEN, "legs": (), "profile": TERMINATION_HIDDEN},
            )
            self.assertEqual(displays["cable.harness_partial"]["missing"], ())
        for trace in ReviewWorkspace(plan, viewer).traces:
            with self.subTest(trace=trace.identity):
                findings = {item["code"]: item["message"] for item in trace.findings}
                self.assertEqual(
                    findings["cable.harness_state"],
                    "This trace is a leg of a breakout on a parent Interface you cannot view.",
                )
                self.assertEqual(
                    findings["cable.harness_partial"],
                    "Some channels of this breakout have no leg in this source, so the breakout Cable leaves them "
                    "unused. A per-module export confirms only the legs it states. A leg added later needs the Cable "
                    "edited in NetBox, because the import never edits a breakout Cable.",
                )

    def test_a_cached_refusal_states_no_channel_count_to_a_viewer_who_cannot_view_the_parent(self):
        parent = self.make_parent(self.make_device("SW-3"), "1/1", 3)
        for number in range(1, 4):
            self.save_resolution(lane(number, device="SW-3"), self.make_child(parent, number))
        viewer = self.viewer(interfaces={"channels__isnull": True})
        cases = {
            "cable.harness_channel_gap": (
                (1, 3),
                ("missing", ()),
                "Some channels between the legs of this breakout have no leg.",
            ),
            "cable.harness_profile_unavailable": (
                (1, 2, 3),
                ("channels", TERMINATION_HIDDEN),
                "This NetBox instance offers no breakout Cable Profile for the channel count of this breakout's parent.",
            ),
        }
        for code, (numbers, (field, hidden), wording) in cases.items():
            with self.subTest(code=code):
                plan = self.plan(*(direct_leg(n, start=lane(n, device="SW-3")) for n in numbers))

                (presented,) = [item for item in present_units(plan.units, viewer)[0].diagnostics if item.code == code]
                findings = {item["code"]: item["message"] for item in ReviewWorkspace(plan, viewer).traces[0].findings}

                self.assertEqual(presented.display[field], hidden)
                self.assertTrue(findings[code].startswith(wording), findings[code])

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
