# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Share trace field-key vocabulary between source and target modules."""

from __future__ import annotations

import json

from .identity import identity_text

INTERFACE_PORT_CLASSES = frozenset({"NIC", "Switch Port", "Port"})
FRONT_PORT_CLASSES = frozenset({"Position Front", "Fiber Pair Front"})
REAR_PORT_CLASSES = frozenset({"Punch-Down", "Fiber Pair Back"})
POWER_PORT_CLASSES = frozenset({"Power Input Port"})
POWER_OUTLET_CLASSES = frozenset({"Power Output Port"})
POWER_CLASSES = POWER_PORT_CLASSES | POWER_OUTLET_CLASSES
PORT_CLASSES = INTERFACE_PORT_CLASSES | FRONT_PORT_CLASSES | REAR_PORT_CLASSES | POWER_CLASSES
PORT_CLASS_CHOICES = tuple((port_class, port_class) for port_class in sorted(PORT_CLASSES))

INTERFACE_KIND = "interface"
FRONT_PORT_KIND = "front_port"
REAR_PORT_KIND = "rear_port"
POWER_PORT_KIND = "power_port"
POWER_OUTLET_KIND = "power_outlet"

PORT_CLASS_CLAIMED_KINDS = {
    **dict.fromkeys(INTERFACE_PORT_CLASSES, INTERFACE_KIND),
    **dict.fromkeys(FRONT_PORT_CLASSES, FRONT_PORT_KIND),
    **dict.fromkeys(REAR_PORT_CLASSES, REAR_PORT_KIND),
    **dict.fromkeys(POWER_PORT_CLASSES, POWER_PORT_KIND),
    **dict.fromkeys(POWER_OUTLET_CLASSES, POWER_OUTLET_KIND),
}

FRONT_PORT_MODEL = "dcim.frontport"
REAR_PORT_MODEL = "dcim.rearport"

#: The NetBox termination models each claimed kind admits, in match order (section 6.1).
ADMITTED_TERMINATION_MODELS = {
    INTERFACE_KIND: (
        "dcim.interface",
        "dcim.consoleport",
        "dcim.consoleserverport",
        "dcim.powerport",
        "dcim.poweroutlet",
    ),
    FRONT_PORT_KIND: (FRONT_PORT_MODEL,),
    REAR_PORT_KIND: (REAR_PORT_MODEL,),
    POWER_PORT_KIND: ("dcim.powerport",),
    POWER_OUTLET_KIND: ("dcim.poweroutlet",),
}
#: Every model an explicit selection may name, in the deduplicated union order of the admission table.
CABLE_END_MODELS = tuple(dict.fromkeys(model for models in ADMITTED_TERMINATION_MODELS.values() for model in models))
CABLE_END_KINDS = frozenset(CABLE_END_MODELS)

TERMINATION_ROLE = "termination"
MAPPED_PEER_ROLE = "mapped_peer"
TERMINATION_ROLES = frozenset({TERMINATION_ROLE, MAPPED_PEER_ROLE})

SELECT_TERMINATION_TASK = "select_termination"


def same_device_and_cards(first, second) -> bool:
    """Return whether two Termination References name one device and one cards label.

    The Source Adapter reads it to claim a pass-through, and the Cable Target Module reads it to
    place one, so the rule has one definition.
    """
    return (identity_text(first.device), identity_text(first.cards)) == (
        identity_text(second.device),
        identity_text(second.cards),
    )


def port_class_mapping_errors(source_label: str, port_class: str) -> dict[str, str]:
    """Return why one source PortClass label cannot map to one PortClass, by field, or nothing when it can."""
    errors = {}
    if not source_label.strip():
        errors["source_label"] = "Enter the source PortClass label."
    elif source_label != source_label.strip():
        # The adapter trims each workbook label, so a padded label never matches one.
        errors["source_label"] = "Remove the whitespace at the start and end of the source PortClass label."
    elif source_label in PORT_CLASSES:
        errors["source_label"] = f"'{source_label}' is a fixed PortClass, so it takes no mapping."
    if port_class not in PORT_CLASSES:
        errors["port_class"] = f"'{port_class}' is not a fixed PortClass."
    return errors


def claimed_termination_kind(port_class: str) -> str:
    """Return the NetBox termination kind claimed by one fixed PortClass value."""
    try:
        return PORT_CLASS_CLAIMED_KINDS[port_class]
    except KeyError as exc:
        raise ValueError(f"Unknown PortClass '{port_class}'.") from exc


def is_outside_claim(kind: str, model_label: str) -> bool:
    """Return whether a selected NetBox model is one the claimed kind does not admit to automatic matching."""
    return model_label not in ADMITTED_TERMINATION_MODELS[kind]


def termination_field_key(*, device, cards, port, kind: str, role: str = TERMINATION_ROLE) -> str:
    """Return the canonical JSON key for one termination-selection role."""
    if kind not in ADMITTED_TERMINATION_MODELS:
        raise ValueError(f"Unknown claimed termination kind '{kind}'.")
    if role not in TERMINATION_ROLES:
        raise ValueError(f"Unknown termination field-key role '{role}'.")
    return json.dumps(
        {
            "cards": identity_text(cards),
            "device": identity_text(device),
            "kind": kind,
            "port": identity_text(port),
            "role": role,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _validate_termination_field_data(data):
    """Reject malformed termination field-key data before canonicalization."""
    if not isinstance(data, dict) or set(data) != {"cards", "device", "kind", "port", "role"}:
        raise ValueError
    if not all(isinstance(data[name], str) for name in data):
        raise ValueError


def parse_termination_field_key(value: str) -> dict[str, str]:
    """Parse an exact canonical termination field key."""
    try:
        data = json.loads(value)
        _validate_termination_field_data(data)
        canonical = termination_field_key(**data)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"'{value}' is not a canonical termination field key.") from exc
    if canonical != value:
        raise ValueError(f"'{value}' is not a canonical termination field key.")
    return data


__all__ = (
    "ADMITTED_TERMINATION_MODELS",
    "CABLE_END_KINDS",
    "CABLE_END_MODELS",
    "FRONT_PORT_CLASSES",
    "FRONT_PORT_KIND",
    "FRONT_PORT_MODEL",
    "INTERFACE_KIND",
    "INTERFACE_PORT_CLASSES",
    "MAPPED_PEER_ROLE",
    "PORT_CLASSES",
    "PORT_CLASS_CHOICES",
    "PORT_CLASS_CLAIMED_KINDS",
    "POWER_CLASSES",
    "POWER_OUTLET_CLASSES",
    "POWER_OUTLET_KIND",
    "POWER_PORT_CLASSES",
    "POWER_PORT_KIND",
    "REAR_PORT_CLASSES",
    "REAR_PORT_KIND",
    "REAR_PORT_MODEL",
    "SELECT_TERMINATION_TASK",
    "TERMINATION_ROLE",
    "TERMINATION_ROLES",
    "claimed_termination_kind",
    "is_outside_claim",
    "parse_termination_field_key",
    "port_class_mapping_errors",
    "same_device_and_cards",
    "termination_field_key",
)
