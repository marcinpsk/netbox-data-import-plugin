# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Own the Cable policy question: the runtime choices, and which stored decision is in force."""

from django.core.exceptions import ValidationError


def _flatten_choice_groups(choices):
    """Return value and label pairs from flat or grouped NetBox choices."""
    flattened = []
    for value, label in choices:
        if isinstance(label, (tuple, list)):
            flattened.extend(label)
        else:
            flattened.append((value, label))
    return tuple(flattened)


def cable_type_choices():
    """Return the Cable Type values offered by the running NetBox instance."""
    from dcim.choices import CableTypeChoices

    return _flatten_choice_groups(CableTypeChoices.CHOICES)


def cable_profile_choices():
    """Return the Cable Profile values offered by the running NetBox instance."""
    from dcim.choices import CableProfileChoices

    return _flatten_choice_groups(CableProfileChoices.CHOICES)


def cable_profile_accepts_one_termination_per_side(value) -> bool:
    """Return whether NetBox reports one connector on each side of the Cable Profile."""
    from dcim.models import Cable

    profile_class = Cable(profile=value).profile_class
    return profile_class is not None and len(profile_class.a_connectors) == 1 and len(profile_class.b_connectors) == 1


def compatible_cable_profile_choices():
    """Return running Cable Profiles that permit one termination on each side."""
    return tuple(
        (value, label)
        for value, label in cable_profile_choices()
        if cable_profile_accepts_one_termination_per_side(value)
    )


def policy_choice_errors(cable_type, cable_profile):
    """Return runtime-choice and profile-cardinality errors by model field."""
    errors = {}
    type_values = {value for value, _label in cable_type_choices()}
    profile_values = {value for value, _label in cable_profile_choices()}
    if cable_type is not None and cable_type not in type_values:
        errors["cable_type"] = ValidationError(
            "The selected Cable Type is no longer offered by this NetBox instance.",
            code="cable.policy_stale",
        )
    if cable_profile is not None and cable_profile not in profile_values:
        errors["cable_profile"] = ValidationError(
            "The selected Cable Profile is no longer offered by this NetBox instance.",
            code="cable.policy_stale",
        )
    elif cable_profile is not None and not cable_profile_accepts_one_termination_per_side(cable_profile):
        errors["cable_profile"] = ValidationError(
            "The selected Cable Profile does not permit one termination on each side.",
            code="cable.profile_incompatible",
        )
    return errors


def policy_in_force(override, mapping):
    """Return the stored decision that governs one segment: its override, else the CableClass row."""
    return mapping if override is None else override


def cable_type_label(value) -> str:
    """Return the operator-facing name of one stored Cable Type value."""
    return "None" if value is None else str(dict(cable_type_choices()).get(value, value))


def cable_profile_label(value) -> str:
    """Return the operator-facing name of one stored Cable Profile value."""
    return "None" if value is None else str(dict(cable_profile_choices()).get(value, value))


__all__ = (
    "cable_profile_accepts_one_termination_per_side",
    "cable_profile_choices",
    "cable_profile_label",
    "cable_type_choices",
    "cable_type_label",
    "compatible_cable_profile_choices",
    "policy_choice_errors",
    "policy_in_force",
)
