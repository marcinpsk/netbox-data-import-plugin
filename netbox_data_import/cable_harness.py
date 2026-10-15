# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Breakout harness facts and the one constructor of every Cable write.

This module is the only reader of Interface `channels` and `channel_id`, and of the breakout Cable
Profile classes. On a NetBox without channel fields it reports no channel child, so a plan there
never meets a harness. Planning and execution build each Cable write through one constructor, so a
write that NetBox refuses, or that puts a leg on another channel, never reaches NetBox.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from typing import Any

from .plan import canonical_json
from .target_runtime import PreconditionFailed

INTERFACE = "dcim.interface"
# The mirrored Cable fields of a child whose parent holds no profiled Cable.
_EMPTY_MIRROR = (None, "", None, ())


class IntentRefused(PreconditionFailed):
    """A Cable write that NetBox would refuse, or that would put a leg on another channel."""

    def __init__(self, code: str, message: str, facts: dict | None = None):
        super().__init__(message)
        self.code = code
        self.facts = facts or {}


@cache
def supported() -> bool:
    """Return whether the running NetBox models Interface channels."""
    from dcim.models import Interface

    return {"channels", "channel_id"} <= {field.name for field in Interface._meta.get_fields()}


def _label(component) -> str:
    return component._meta.label_lower


def _record(component) -> list:
    """Return the object, its id and its Device, as a Planned Change records one termination."""
    return [_label(component), component.pk, component.device_id]


def channel_parent(component) -> int | None:
    """Return the parent of a channel child Interface, or None for every other termination."""
    if not supported() or _label(component) != INTERFACE or component.channel_id is None:
        return None
    return component.parent_id


def channel(component) -> int:
    """Return the channel of one channel child Interface."""
    return component.channel_id


def is_plain_interface(component) -> bool:
    """Return whether *component* is an Interface that is neither channelized nor a channel child."""
    return _label(component) == INTERFACE and component.channel_id is None and not component.channels


@dataclass(frozen=True)
class Child:
    """One Interface whose parent is a channelized Interface, as read under the plan's locks."""

    component: Any
    terminated: bool

    @property
    def channel(self) -> int | None:
        """Return the channel the child binds to, or None for a plain subinterface."""
        return self.component.channel_id

    @property
    def mirror(self) -> tuple:
        """Return the Cable fields NetBox mirrors onto the child, with NetBox's two empty spellings merged."""
        row = self.component
        return (row.cable_id, row.cable_end or "", row.cable_connector, tuple(row.cable_positions or ()))


@dataclass(frozen=True)
class Parent:
    """One channelized Interface and every Interface whose parent it is."""

    component: Any
    children: tuple[Child, ...]

    @property
    def pk(self) -> int:
        """Return the parent Interface id."""
        return self.component.pk

    @property
    def channels(self) -> int:
        """Return the channel count of the parent, 0 when it is not channelized."""
        return self.component.channels or 0

    def evidence(self) -> dict:
        """Return the channel facts a unit fingerprints: the count and the full child set."""
        return {"channels": self.channels, "children": [[child.component.pk, child.channel] for child in self.children]}

    def faults(self, cable_profile: str) -> list[tuple[Child, str]]:
        """Return each channel child that contradicts NetBox's channel rules, with the rule it breaks.

        The mirror rule restates `propagate_channel_cables` and `clear_channel_cables`.
        """
        row = self.component
        profiled = bool(row.cable_id and cable_profile)
        found = []
        for child in self.children:
            if child.channel is None:
                continue
            expected = (row.cable_id, row.cable_end, row.cable_connector, (child.channel,))
            if child.terminated:
                found.append((child, "termination"))
            elif child.channel > self.channels:
                found.append((child, "channel"))
            elif child.mirror != (expected if profiled else _EMPTY_MIRROR):
                found.append((child, "mirror"))
        return found


def read_parents(parent_ids, *, lock: bool) -> dict[int, Parent]:
    """Read each parent, then every Interface under it, locking both in that order when asked."""
    from core.models import ObjectType
    from dcim.models import CableTermination, Interface

    if not parent_ids:
        return {}
    ids = sorted(parent_ids)
    parents = Interface.objects.filter(pk__in=ids).order_by("pk")
    children = Interface.objects.filter(parent_id__in=ids).order_by("pk")
    if lock:
        parents = parents.select_for_update(of=("self",))
        children = children.select_for_update(of=("self",))
    parent_rows = list(parents)
    child_rows = list(children)
    terminated = set(
        CableTermination.objects.filter(
            termination_type=ObjectType.objects.get_for_model(Interface),
            termination_id__in=[row.pk for row in child_rows],
        ).values_list("termination_id", flat=True)
    )
    by_parent: dict[int, list[Child]] = {}
    for row in child_rows:
        by_parent.setdefault(row.parent_id, []).append(Child(component=row, terminated=row.pk in terminated))
    return {row.pk: Parent(component=row, children=tuple(by_parent.get(row.pk, ()))) for row in parent_rows}


def _routes_channels(profile_class, channels: int) -> bool:
    """Return whether a profile joins one A connector of *channels* positions, position k to B connector k."""
    if profile_class is None:
        return False
    profile = profile_class()
    numbers = range(1, channels + 1)
    return (
        profile.a_connectors == {1: channels}
        and profile.b_connectors == dict.fromkeys(numbers, 1)
        and all(
            profile.get_mapped_position("A", 1, k) == (k, 1) and profile.get_mapped_position("B", k, 1) == (1, k)
            for k in numbers
        )
    )


@cache
def breakout_profile(channels: int) -> str | None:
    """Return the running instance's one Cable Profile that breaks *channels* lanes out one per B connector."""
    from dcim.models import Cable

    from .cable_policy import cable_profile_choices

    matches = [
        value
        for value, _label in cable_profile_choices()
        if _routes_channels(Cable(profile=value).profile_class, channels)
    ]
    if len(matches) > 1:
        raise RuntimeError(f"Several Cable Profiles break out {channels} channels: {', '.join(matches)}.")
    return matches[0] if matches else None


@dataclass(frozen=True)
class CableIntent:
    """The ends and the profile of one Cable write, each end list in the order NetBox numbers connectors."""

    a_terminations: tuple
    b_terminations: tuple
    profile: str
    # A harness intent: the channel child behind each B end, and the parent's channel count.
    children: tuple = ()
    capacity: int = 0

    @property
    def trailing(self) -> tuple[int, ...]:
        """Return the channels after the last leg that the harness leaves unused."""
        return tuple(range(len(self.b_terminations) + 1, self.capacity + 1))

    def snapshot(self) -> frozenset:
        """Return the CableTermination rows NetBox writes for this intent, as `update_terminations` numbers them."""
        from dcim.models import Cable

        profile = Cable(profile=self.profile).profile_class()
        rows = set()
        for side, ends, connectors in (
            ("A", self.a_terminations, profile.a_connectors),
            ("B", self.b_terminations, profile.b_connectors),
        ):
            for connector, end in enumerate(ends, start=1):
                rows.add((side, connector, tuple(range(1, connectors[connector] + 1)), _label(end), end.pk))
        return frozenset(rows)

    def proved_by(self, cable_profile: str, snapshot: frozenset) -> bool:
        """Return whether an existing Cable with this profile and termination snapshot is this write."""
        return cable_profile == self.profile and snapshot == self.snapshot()


def _refuse_channel_child(end) -> None:
    if channel_parent(end) is not None:
        raise IntentRefused(
            "cable.harness_unsupported",
            "NetBox cannot cable a channel subinterface directly. The import cables its parent instead.",
        )


def ordinary_intent(first, second, *, profile: str) -> CableIntent:
    """Return a one-to-one Cable write, refusing a channel child at either end."""
    _refuse_channel_child(first)
    _refuse_channel_child(second)
    return CableIntent(a_terminations=(first,), b_terminations=(second,), profile=profile)


def harness_intent(parent, legs) -> CableIntent:
    """Return the breakout Cable write for *legs*, the (channel child, far end) pairs of *parent*.

    The B side is ordered by channel, so B connector k carries channel k. A write that would shift a
    leg to another connector, or that NetBox would refuse, is refused here with its diagnostic code.
    """
    if not legs:
        raise ValueError("A harness intent needs at least one leg.")
    for end in (parent, *(far for _child, far in legs)):
        _refuse_channel_child(end)
    if any(_label(far) == INTERFACE and far.pk == parent.pk for _child, far in legs):
        raise IntentRefused(
            "cable.harness_unsupported", "A leg ends on its own parent, so the trace runs inside one harness."
        )
    if any(channel_parent(child) != parent.pk for child, _far in legs):
        raise IntentRefused("cable.harness_channel_state_invalid", "A leg is no longer a channel of this parent.")
    ordered = sorted(legs, key=lambda leg: leg[0].channel_id)
    numbers = [child.channel_id for child, _far in ordered]
    repeated = sorted({number for number in numbers if numbers.count(number) > 1})
    fars = [(_label(far), far.pk) for _child, far in ordered]
    shared = sorted({child.channel_id for child, far in ordered if fars.count((_label(far), far.pk)) > 1})
    if repeated or shared:
        raise IntentRefused(
            "cable.harness_leg_conflict",
            "Two legs of this harness claim one channel or one far end.",
            {"conflicts": sorted(set(repeated) | set(shared))},
        )
    capacity = parent.channels or 0
    if numbers[-1] > capacity:
        raise IntentRefused("cable.harness_channel_state_invalid", "A leg names a channel the parent does not have.")
    models = sorted({model for model, _pk in fars})
    if len(models) > 1:
        raise IntentRefused(
            "cable.harness_mixed_terminations",
            "The far ends of this harness are of more than one kind, and NetBox cables one kind per side.",
            {"models": models},
        )
    missing = sorted(set(range(1, numbers[-1] + 1)) - set(numbers))
    if missing:
        raise IntentRefused(
            "cable.harness_channel_gap",
            "A channel between the legs of this harness has no leg, so a later leg would take its connector.",
            {"missing": missing},
        )
    profile = breakout_profile(capacity)
    if profile is None:
        raise IntentRefused(
            "cable.harness_profile_unavailable",
            "This NetBox instance offers no breakout Cable Profile for the parent's channel count.",
            {"channels": capacity},
        )
    return CableIntent(
        a_terminations=(parent,),
        b_terminations=tuple(far for _child, far in ordered),
        profile=profile,
        children=tuple(child for child, _far in ordered),
        capacity=capacity,
    )


def harness_payload(intent: CableIntent) -> dict:
    """Return what a harness Planned Change records so execution can rebuild the same intent."""
    return {
        "profile": intent.profile,
        "parent": _record(intent.a_terminations[0]),
        "legs": [[child.pk, _record(far)] for child, far in zip(intent.children, intent.b_terminations, strict=True)],
    }


def locked_harness_intent(recorded, free_termination) -> CableIntent:
    """Rebuild a recorded harness intent from locked live rows, refusing one whose facts moved.

    *free_termination* locks one recorded termination and refuses it when it is gone, moved or cabled.
    """
    from dcim.models import Interface

    parent = free_termination(*recorded["parent"])
    fars = [free_termination(*far) for _child, far in recorded["legs"]]
    child_ids = [child for child, _far in recorded["legs"]]
    children = Interface.objects.filter(pk__in=sorted(child_ids)).order_by("pk").select_for_update(of=("self",))
    by_pk = {child.pk: child for child in children}
    if set(child_ids) - by_pk.keys():
        raise PreconditionFailed("A channel subinterface of this harness is gone, so the harness cannot be created.")
    intent = harness_intent(parent, [(by_pk[child], far) for child, far in zip(child_ids, fars, strict=True)])
    if canonical_json(harness_payload(intent)) != canonical_json(recorded):
        raise PreconditionFailed("The channels of this harness changed after the plan was made.")
    return intent


__all__ = (
    "CableIntent",
    "Child",
    "IntentRefused",
    "Parent",
    "breakout_profile",
    "channel",
    "channel_parent",
    "harness_intent",
    "harness_payload",
    "is_plain_interface",
    "locked_harness_intent",
    "ordinary_intent",
    "read_parents",
    "supported",
)
