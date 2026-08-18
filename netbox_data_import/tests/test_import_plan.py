# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The target-neutral Import Plan: structure, fingerprints, and the dependency graph."""

from django.test import SimpleTestCase

from netbox_data_import.plan import (
    SCHEMA_VERSION,
    Diagnostic,
    Disposition,
    ImportPlan,
    PlanInvalid,
    PlanSchemaMismatch,
    PlannedChange,
    Severity,
    SynchronizationUnit,
    merge_changes,
)


def _change(identity="device:1", *, payload=None, dependencies=(), preconditions=None):
    """Return one Planned Change with defaulted target-neutral content."""
    return PlannedChange(
        identity=identity,
        target_module="device",
        operation="create",
        payload=payload if payload is not None else {"name": "sw-1"},
        dependencies=dependencies,
        preconditions=preconditions if preconditions is not None else {"device_absent": True},
    )


def _unit(identity="unit:1", *, disposition=Disposition.ACTIONABLE, changes=None, diagnostics=(), display=None):
    """Return one Synchronization Unit around *changes*."""
    return SynchronizationUnit(
        identity=identity,
        disposition=disposition,
        changes=changes if changes is not None else (_change(),),
        diagnostics=diagnostics,
        display=display if display is not None else {"label": "Row 2"},
    )


def _plan(units=None, **overrides):
    """Return one Import Plan with defaulted fingerprint inputs."""
    fields = {
        "units": units if units is not None else (_unit(),),
        "diagnostics": (),
        "source_fingerprint": "src-aaa",
        "profile_fingerprint": "prof-bbb",
        "actor": "operator-1",
        "planning_context": {"site_id": 3},
        "revision": 1,
    }
    fields.update(overrides)
    return ImportPlan(**fields)


class PlanStructureTest(SimpleTestCase):
    """Section 4.1: the plan is a serializable derived artifact."""

    def test_a_plan_carries_the_current_schema_version(self):
        """A serialized plan states the version that produced it."""
        self.assertEqual(_plan().schema_version, SCHEMA_VERSION)

    def test_a_payload_that_is_not_serializable_is_rejected_at_construction(self):
        """No live ORM object, queryset, callable, or HTML fragment can enter a plan."""
        with self.assertRaises(PlanInvalid):
            _change(payload={"device": object()})

    def test_a_payload_is_detached_from_the_caller_mapping(self):
        """A plan never aliases planning state, so a later mutation cannot rewrite it."""
        payload = {"name": "sw-1", "nested": {"face": "front"}}
        change = _change(payload=payload)
        payload["name"] = "mutated"
        payload["nested"]["face"] = "rear"
        self.assertEqual(change.payload, {"name": "sw-1", "nested": {"face": "front"}})

    def test_an_unknown_disposition_is_rejected(self):
        """Section 4.2 fixes the disposition vocabulary."""
        with self.assertRaises(PlanInvalid):
            _unit(disposition="probably-fine")

    def test_every_declared_disposition_is_accepted(self):
        """The five spec dispositions are the complete vocabulary."""
        for disposition in (
            Disposition.ACTIONABLE,
            Disposition.NO_OP,
            Disposition.BLOCKED,
            Disposition.INVALID,
            Disposition.EXCLUDED,
        ):
            with self.subTest(disposition=disposition):
                self.assertEqual(_unit(disposition=disposition).disposition, disposition)

    def test_a_diagnostic_code_uses_the_dotted_namespace(self):
        """Section 4.2 fixes the `<domain>.<condition>` code form."""
        Diagnostic(code="device.name_conflict", severity=Severity.ERROR)
        with self.assertRaises(PlanInvalid):
            Diagnostic(code="name_conflict", severity=Severity.ERROR)

    def test_an_unknown_diagnostic_severity_is_rejected(self):
        """Diagnostics carry info, warning, or error severity."""
        with self.assertRaises(PlanInvalid):
            Diagnostic(code="device.name_conflict", severity="fatal")


class PlanFingerprintTest(SimpleTestCase):
    """Section 4.3: planning is deterministic and the fingerprint covers the decision inputs."""

    def test_equivalent_plans_fingerprint_identically(self):
        """The same inputs produce the same digest, so replanning is comparable."""
        self.assertEqual(_plan().fingerprint, _plan().fingerprint)

    def test_the_fingerprint_is_a_sha256_digest(self):
        """ADR 0002 fixes SHA-256, and one algorithm keeps the runtime consistent."""
        self.assertRegex(_plan().fingerprint, r"^[0-9a-f]{64}$")

    def test_key_order_does_not_change_the_fingerprint(self):
        """Canonical serialization sorts keys, so mapping order is not a decision input."""
        one = _plan(units=(_unit(changes=(_change(payload={"a": 1, "b": 2}),)),))
        other = _plan(units=(_unit(changes=(_change(payload={"b": 2, "a": 1}),)),))
        self.assertEqual(one.fingerprint, other.fingerprint)

    def test_display_wording_is_excluded(self):
        """Display data and translated text never invalidate an accepted selection."""
        self.assertEqual(
            _plan(units=(_unit(display={"label": "Row 2"}),)).fingerprint,
            _plan(units=(_unit(display={"label": "Zeile 2"}),)).fingerprint,
        )

    def test_the_revision_is_excluded(self):
        """Section 4.3 keeps the plan revision separate from the identities."""
        self.assertEqual(_plan(revision=1).fingerprint, _plan(revision=7).fingerprint)

    def test_diagnostic_display_is_excluded_but_the_code_is_included(self):
        """The structured code is a decision input; its rendered wording is not."""
        coded = Diagnostic(code="device.name_conflict", severity=Severity.ERROR, display={"text": "one"})
        reworded = Diagnostic(code="device.name_conflict", severity=Severity.ERROR, display={"text": "two"})
        other_code = Diagnostic(code="device.serial_conflict", severity=Severity.ERROR)
        self.assertEqual(
            _plan(units=(_unit(diagnostics=(coded,)),)).fingerprint,
            _plan(units=(_unit(diagnostics=(reworded,)),)).fingerprint,
        )
        self.assertNotEqual(
            _plan(units=(_unit(diagnostics=(coded,)),)).fingerprint,
            _plan(units=(_unit(diagnostics=(other_code,)),)).fingerprint,
        )

    def test_every_decision_input_changes_the_fingerprint(self):
        """Section 4.3 lists what the digest must cover."""
        baseline = _plan().fingerprint
        variants = {
            "unit identity": _plan(units=(_unit(identity="unit:2"),)),
            "change identity": _plan(units=(_unit(changes=(_change(identity="device:2"),)),)),
            "payload": _plan(units=(_unit(changes=(_change(payload={"name": "sw-2"}),)),)),
            "dependencies": _plan(units=(_unit(changes=(_change(dependencies=("rack:1",)),)),)),
            "preconditions": _plan(units=(_unit(changes=(_change(preconditions={"device_absent": False}),)),)),
            "disposition": _plan(units=(_unit(disposition=Disposition.BLOCKED),)),
            "source fingerprint": _plan(source_fingerprint="src-zzz"),
            "profile fingerprint": _plan(profile_fingerprint="prof-zzz"),
            "actor": _plan(actor="operator-2"),
            "planning context": _plan(planning_context={"site_id": 4}),
        }
        for label, variant in variants.items():
            with self.subTest(input=label):
                self.assertNotEqual(baseline, variant.fingerprint)

    def test_each_unit_carries_its_own_fingerprint(self):
        """Selective execution compares one unit at a time, so units digest independently."""
        unit = _unit()
        self.assertRegex(unit.fingerprint, r"^[0-9a-f]{64}$")
        self.assertEqual(unit.fingerprint, _unit().fingerprint)
        self.assertNotEqual(unit.fingerprint, _unit(identity="unit:2").fingerprint)

    def test_a_unit_fingerprint_ignores_its_display_wording(self):
        """An unrelated wording change never blocks a safe selection."""
        self.assertEqual(_unit(display={"label": "a"}).fingerprint, _unit(display={"label": "b"}).fingerprint)


class PlanSerializationTest(SimpleTestCase):
    """Section 4.8: a serialized plan round-trips and states its schema version."""

    def test_a_plan_round_trips_through_its_serialized_form(self):
        """The session and the job payload carry the plan without losing a decision input."""
        original = _plan(
            units=(
                _unit(
                    diagnostics=(Diagnostic(code="device.name_conflict", severity=Severity.WARNING),),
                    changes=(_change(dependencies=("rack:1",)), _change(identity="device:2")),
                ),
            )
        )
        restored = ImportPlan.from_dict(original.to_dict())
        self.assertEqual(restored, original)
        self.assertEqual(restored.fingerprint, original.fingerprint)

    def test_the_serialized_form_is_json_safe(self):
        """A plan reaches a job payload as JSON, so it holds no exotic value."""
        import json

        self.assertEqual(json.loads(json.dumps(_plan().to_dict())), _plan().to_dict())

    def test_an_incompatible_schema_version_fails_before_any_write(self):
        """Section 4.8 requires replanning instead of a compatibility executor."""
        stale = _plan().to_dict()
        stale["schema_version"] = SCHEMA_VERSION + 1
        with self.assertRaises(PlanSchemaMismatch):
            ImportPlan.from_dict(stale)


class DependencyGraphTest(SimpleTestCase):
    """Section 4.4: the coordinator merges changes into one directed acyclic graph."""

    def test_it_orders_dependencies_before_dependents(self):
        """A dependency executes before the change that declares it."""
        rack = _change(identity="rack:1")
        device = _change(identity="device:1", dependencies=("rack:1",))
        ordered = merge_changes((_unit(changes=(device, rack)),))
        self.assertEqual([c.identity for c in ordered], ["rack:1", "device:1"])

    def test_independent_changes_keep_a_deterministic_order(self):
        """Section 4.3 requires the same ordering for the same inputs."""
        units = (_unit(changes=(_change(identity="device:2"), _change(identity="device:1"))),)
        self.assertEqual([c.identity for c in merge_changes(units)], [c.identity for c in merge_changes(units)])

    def test_a_missing_dependency_reference_is_rejected(self):
        """A dangling identity makes the plan invalid rather than silently ordering."""
        units = (_unit(changes=(_change(identity="device:1", dependencies=("rack:missing",)),)),)
        with self.assertRaises(PlanInvalid) as caught:
            merge_changes(units)
        self.assertIn("rack:missing", str(caught.exception))

    def test_a_cycle_is_rejected(self):
        """The merged graph must stay acyclic."""
        one = _change(identity="a", dependencies=("b",))
        other = _change(identity="b", dependencies=("a",))
        with self.assertRaises(PlanInvalid) as caught:
            merge_changes((_unit(changes=(one, other)),))
        self.assertIn("cycle", str(caught.exception).lower())

    def test_an_identical_identity_is_shared_and_executes_once(self):
        """Two units that need the same supporting change produce one execution."""
        shared = _change(identity="device_type:1")
        units = (_unit(identity="unit:1", changes=(shared,)), _unit(identity="unit:2", changes=(shared,)))
        self.assertEqual([c.identity for c in merge_changes(units)], ["device_type:1"])

    def test_the_same_identity_with_a_different_payload_makes_the_plan_invalid(self):
        """Section 4.4: sharing requires the changes to agree."""
        units = (
            _unit(identity="unit:1", changes=(_change(identity="device_type:1", payload={"model": "a"}),)),
            _unit(identity="unit:2", changes=(_change(identity="device_type:1", payload={"model": "b"}),)),
        )
        with self.assertRaises(PlanInvalid) as caught:
            merge_changes(units)
        self.assertIn("device_type:1", str(caught.exception))

    def test_the_same_identity_with_different_preconditions_makes_the_plan_invalid(self):
        """Preconditions are part of what a shared change promises."""
        units = (
            _unit(identity="unit:1", changes=(_change(identity="d:1", preconditions={"absent": True}),)),
            _unit(identity="unit:2", changes=(_change(identity="d:1", preconditions={"absent": False}),)),
        )
        with self.assertRaises(PlanInvalid):
            merge_changes(units)
