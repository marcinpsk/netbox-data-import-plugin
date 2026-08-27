# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The target-neutral Import Plan: structure, fingerprints, and the dependency graph."""

from dataclasses import replace

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
    executable_units,
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

    def test_an_empty_target_module_or_operation_is_rejected(self):
        """Both are fingerprint inputs and both are required, so an empty value must fail early."""
        for field in ("target_module", "operation"):
            for value in ("", None, 7):
                with self.subTest(field=field, value=value):
                    fields = {"target_module": "device", "operation": "create"}
                    fields[field] = value
                    with self.assertRaises(PlanInvalid):
                        PlannedChange(identity="device:1", payload={}, **fields)

    def test_a_unit_rejects_changes_and_diagnostics_that_are_not_plan_objects(self):
        """The constructor is the boundary, so a serialized dict must fail here, not at fingerprint."""
        for field_name in ("changes", "diagnostics"):
            for value in ({"identity": "device:1"}, "device:1", 7):
                with self.subTest(field=field_name, value=value):
                    with self.assertRaises(PlanInvalid):
                        SynchronizationUnit(identity="row:1", disposition=Disposition.ACTIONABLE, **{field_name: value})

    def test_a_plan_rejects_units_and_diagnostics_that_are_not_plan_objects(self):
        """A plan carries the same boundary as the units it holds."""
        for field_name in ("units", "diagnostics"):
            for value in ({"identity": "row:1"}, "row:1", 7):
                with self.subTest(field=field_name, value=value):
                    with self.assertRaises(PlanInvalid):
                        ImportPlan(**{field_name: value})

    def test_a_plan_rejects_a_scalar_field_that_is_not_plan_data(self):
        """The module contract is that every value in a plan survives a canonical JSON round trip.

        A caller passing `actor=request.user` built a plan, and the failure then surfaced as a bare
        `TypeError` out of `canonical_json` inside `fingerprint`, which `except PlanError` misses.
        """
        for field_name in ("source_fingerprint", "profile_fingerprint", "actor", "revision", "schema_version"):
            with self.subTest(field=field_name):
                with self.assertRaises(PlanInvalid):
                    ImportPlan(**{field_name: object()})

    def test_a_plan_rejects_a_scalar_field_of_the_wrong_shape(self):
        """JSON-serializable is not enough: each scalar has a declared type the plan relies on.

        `actor={"id": 1}` survived a JSON round trip and froze to a mapping, and `fingerprint` then
        raised a bare `TypeError` out of `canonical_json`. A bool schema version was accepted here
        while `from_dict` refuses one, so a plan could not survive its own serialization.
        """
        for field_name, value in (
            ("source_fingerprint", 7),
            ("profile_fingerprint", ["a"]),
            ("actor", {"id": 1}),
            ("actor", None),
            ("revision", "1"),
            ("revision", True),
            ("schema_version", True),
            ("schema_version", 1.0),
        ):
            with self.subTest(field=field_name, value=value):
                with self.assertRaises(PlanInvalid):
                    ImportPlan(**{field_name: value})

    def test_a_plan_keeps_the_scalar_values_it_accepts(self):
        """The check must not reshape a value a caller legitimately passes."""
        plan = ImportPlan(
            source_fingerprint="abc", profile_fingerprint="def", actor="operator", revision=3, schema_version=1
        )

        self.assertEqual(
            (plan.source_fingerprint, plan.profile_fingerprint, plan.actor, plan.revision, plan.schema_version),
            ("abc", "def", "operator", 3, 1),
        )

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
        plan = _plan()
        self.assertRegex(plan.unit_fingerprint("unit:1"), r"^[0-9a-f]{64}$")
        self.assertEqual(plan.unit_fingerprint("unit:1"), _plan().unit_fingerprint("unit:1"))
        self.assertNotEqual(
            plan.unit_fingerprint("unit:1"),
            _plan(units=(_unit(identity="unit:2"),)).unit_fingerprint("unit:2"),
        )

    def test_a_unit_fingerprint_ignores_its_display_wording(self):
        """An unrelated wording change never blocks a safe selection."""
        one = _plan(units=(_unit(display={"label": "a"}),))
        other = _plan(units=(_unit(display={"label": "b"}),))
        self.assertEqual(one.unit_fingerprint("unit:1"), other.unit_fingerprint("unit:1"))

    def test_every_plan_wide_input_changes_each_unit_fingerprint(self):
        """Source, profile, actor, and context changes invalidate every accepted selection."""
        baseline = _plan().unit_fingerprint("unit:1")
        variants = (
            _plan(schema_version=SCHEMA_VERSION + 1),
            _plan(source_fingerprint="src-zzz"),
            _plan(profile_fingerprint="prof-zzz"),
            _plan(actor="operator-2"),
            _plan(planning_context={"site_id": 4}),
        )
        for variant in variants:
            with self.subTest(plan=variant):
                self.assertNotEqual(baseline, variant.unit_fingerprint("unit:1"))


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

    def test_a_schema_version_must_be_an_integer(self):
        """JSON booleans and floats are not valid Import Plan schema versions."""
        for version in (True, 1.0):
            with self.subTest(version=version):
                payload = _plan().to_dict()
                payload["schema_version"] = version
                with self.assertRaises(PlanSchemaMismatch):
                    ImportPlan.from_dict(payload)


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


class PlanBoundaryTest(SimpleTestCase):
    """Section 4.1: the constructor is the boundary that keeps planning state out of a plan."""

    def test_a_non_finite_number_is_rejected(self):
        """NaN and Infinity are not JSON, and NaN never equals itself, so sharing would break."""
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                with self.assertRaises(PlanInvalid):
                    _change(payload={"u_position": value})

    def test_a_dependency_that_is_not_an_identity_string_is_rejected(self):
        """Dependencies are stable identities, so an ORM object must fail at construction."""
        with self.assertRaises(PlanInvalid):
            _change(dependencies=(object(),))

    def test_a_bare_string_of_dependencies_is_rejected(self):
        """tuple("rack:1") would silently become six single-character identities."""
        with self.assertRaises(PlanInvalid):
            _change(dependencies="rack:1")

    def test_a_diagnostic_identity_that_is_not_a_string_is_rejected(self):
        """The affected identities are a fingerprint input, so they must be plan data."""
        with self.assertRaises(PlanInvalid):
            Diagnostic(code="device.name_conflict", severity=Severity.ERROR, identities=(object(),))

    def test_duplicate_detection_holds_at_workbook_scale(self):
        """One unit exists per reviewable source row, so the scan must not be quadratic."""
        units = tuple(_unit(identity=f"row:{index}") for index in range(3000))
        self.assertEqual(len(_plan(units=units).units), 3000)
        with self.assertRaises(PlanInvalid):
            _plan(units=units + (_unit(identity="row:2999"),))

    def test_duplicate_unit_identities_are_rejected(self):
        """Selection resolves a unit by identity, so two units cannot share one."""
        with self.assertRaises(PlanInvalid):
            _plan(units=(_unit(identity="row:7"), _unit(identity="row:7", disposition=Disposition.BLOCKED)))

    def test_the_serialized_form_does_not_alias_the_plan(self):
        """A caller that edits the serialized form must not rewrite the plan it came from."""
        plan = _plan()
        data = plan.to_dict()
        data["planning_context"]["site_id"] = 99
        data["units"][0]["changes"][0]["payload"]["name"] = "mutated"
        data["units"][0]["display"]["label"] = "mutated"
        self.assertEqual(plan.planning_context, {"site_id": 3})
        self.assertEqual(plan.units[0].changes[0].payload, {"name": "sw-1"})
        self.assertEqual(plan.fingerprint, _plan().fingerprint)

    def test_nested_serialized_data_does_not_alias_the_plan(self):
        """Every JSON container is detached, not only each top-level mapping."""
        diagnostic = Diagnostic(
            code="device.name_conflict",
            severity=Severity.WARNING,
            display={"message": {"text": "Conflict"}},
        )
        plan = _plan(
            units=(
                _unit(
                    diagnostics=(diagnostic,),
                    display={"row": {"label": "Row 2"}},
                ),
            ),
            planning_context={"site": {"id": 3}},
        )

        data = plan.to_dict()
        data["planning_context"]["site"]["id"] = 99
        data["units"][0]["display"]["row"]["label"] = "mutated"
        data["units"][0]["diagnostics"][0]["display"]["message"]["text"] = "mutated"

        self.assertEqual(plan.planning_context, {"site": {"id": 3}})
        self.assertEqual(plan.units[0].display, {"row": {"label": "Row 2"}})
        self.assertEqual(plan.units[0].diagnostics[0].display, {"message": {"text": "Conflict"}})

    def test_the_frozen_value_types_refuse_mapping_mutation(self):
        """A frozen plan must keep a stable hash and fingerprint after construction."""
        diagnostic = Diagnostic(
            code="device.name_conflict",
            severity=Severity.WARNING,
            display={"text": "Conflict"},
        )
        change = _change(payload={"name": "sw-1", "labels": ["edge"]})
        unit = _unit(changes=(change,), diagnostics=(diagnostic,))
        plan = _plan(units=(unit,), planning_context={"site": {"id": 3}})
        changes = {change}
        fingerprint = plan.fingerprint
        mappings = (
            ("diagnostic display", diagnostic.display),
            ("change payload", change.payload),
            ("change preconditions", change.preconditions),
            ("unit display", unit.display),
            ("planning context", plan.planning_context),
            ("nested planning context", plan.planning_context["site"]),
        )

        for label, mapping in mappings:
            with self.subTest(mapping=label), self.assertRaises(TypeError):
                mapping["mutated"] = True

        with self.assertRaises(AttributeError):
            change.payload["labels"].append("mutated")

        self.assertIn(change, changes)
        self.assertEqual(plan.fingerprint, fingerprint)

    def test_frozen_mapping_values_can_construct_derived_plan_objects(self):
        """A caller can reuse a frozen value without converting it through serialization first."""
        change = _change(payload={"name": "sw-1", "labels": ["edge"]})
        derived_change = replace(change, operation="update")
        reused_payload = _change(identity="device:2", payload=change.payload)
        derived_plan = replace(_plan(units=(_unit(changes=(change,)),)), revision=2)

        self.assertEqual(derived_change.payload, change.payload)
        self.assertEqual(reused_payload.payload, change.payload)
        self.assertEqual(derived_plan.revision, 2)

    def test_the_value_types_are_hashable(self):
        """A coordinator deduplicating changes with a set must not hit an unhashable dict."""
        self.assertEqual(len({_change(), _change()}), 1)
        self.assertEqual(len({_change(), _change(identity="device:2")}), 2)
        self.assertEqual(len({_unit(), _unit()}), 1)
        self.assertEqual(len({_plan(), _plan()}), 1)

    def test_a_malformed_serialized_plan_raises_a_typed_plan_error(self):
        """Section 4.8 requires a caller to replan, so one `except PlanError` must cover every case."""
        from netbox_data_import.plan import PlanError

        for broken in (
            None,
            [],
            {"schema_version": SCHEMA_VERSION, "units": [{"identity": "u1"}], "diagnostics": []},
            {"schema_version": SCHEMA_VERSION, "units": None, "diagnostics": []},
            {"schema_version": SCHEMA_VERSION},
            {"units": [], "diagnostics": []},
        ):
            with self.subTest(broken=broken):
                with self.assertRaises(PlanError):
                    ImportPlan.from_dict(broken)

    def test_a_structurally_broken_plan_is_invalid_rather_than_a_version_mismatch(self):
        """A caller distinguishes 'replan' from 'this payload is corrupt'."""
        with self.assertRaises(PlanInvalid):
            ImportPlan.from_dict({"schema_version": SCHEMA_VERSION, "units": None, "diagnostics": []})


class ExecutableUnitsTest(SimpleTestCase):
    """Section 4.6: only actionable units enter an execution transaction."""

    def test_it_keeps_only_the_actionable_units(self):
        """Blocked, invalid, excluded, and no-op units never execute."""
        units = tuple(
            _unit(identity=f"unit:{index}", disposition=disposition)
            for index, disposition in enumerate(
                (
                    Disposition.ACTIONABLE,
                    Disposition.NO_OP,
                    Disposition.BLOCKED,
                    Disposition.INVALID,
                    Disposition.EXCLUDED,
                )
            )
        )
        self.assertEqual([unit.identity for unit in executable_units(units)], ["unit:0"])


class SelectiveMergeTest(SimpleTestCase):
    """Section 4.5: a dependency must already be reconciled or be included explicitly."""

    def test_a_reconciled_dependency_satisfies_a_subset_merge(self):
        """A later selective execution must not be rejected for depending on an executed change."""
        unit = _unit(identity="unit:2", changes=(_change(identity="device:5", dependencies=("device_type:1",)),))
        ordered = merge_changes((unit,), reconciled=("device_type:1",))
        self.assertEqual([change.identity for change in ordered], ["device:5"])

    def test_an_unreconciled_dependency_still_fails_the_subset_merge(self):
        """Selective synchronization never expands silently."""
        unit = _unit(identity="unit:2", changes=(_change(identity="device:5", dependencies=("device_type:1",)),))
        with self.assertRaises(PlanInvalid):
            merge_changes((unit,))

    def test_a_reconciled_identity_is_not_executed_again(self):
        """The reconciled set orders the selection; it never adds work."""
        unit = _unit(identity="unit:2", changes=(_change(identity="device:5", dependencies=("device_type:1",)),))
        ordered = merge_changes((unit,), reconciled=("device_type:1",))
        self.assertNotIn("device_type:1", [change.identity for change in ordered])
