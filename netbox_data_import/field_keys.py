# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Share trace field-key vocabulary between source and target modules."""

from __future__ import annotations

import json

from .values import identity_text

INTERFACE_PORT_CLASSES = frozenset({"NIC", "Switch Port", "Port"})
FRONT_PORT_CLASSES = frozenset({"Position Front", "Fiber Pair Front"})
REAR_PORT_CLASSES = frozenset({"Punch-Down", "Fiber Pair Back"})
PORT_CLASSES = INTERFACE_PORT_CLASSES | FRONT_PORT_CLASSES | REAR_PORT_CLASSES

INTERFACE_KIND = "interface"
FRONT_PORT_KIND = "front_port"
REAR_PORT_KIND = "rear_port"

PORT_CLASS_CLAIMED_KINDS = {
    **dict.fromkeys(INTERFACE_PORT_CLASSES, INTERFACE_KIND),
    **dict.fromkeys(FRONT_PORT_CLASSES, FRONT_PORT_KIND),
    **dict.fromkeys(REAR_PORT_CLASSES, REAR_PORT_KIND),
}

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


def claimed_termination_kind(port_class: str) -> str:
    """Return the NetBox termination kind claimed by one fixed PortClass value."""
    try:
        return PORT_CLASS_CLAIMED_KINDS[port_class]
    except KeyError as exc:
        raise ValueError(f"Unknown PortClass '{port_class}'.") from exc


def termination_field_key(*, device, cards, port, kind: str, role: str = TERMINATION_ROLE) -> str:
    """Return the canonical JSON key for one termination-selection role."""
    if kind not in {INTERFACE_KIND, FRONT_PORT_KIND, REAR_PORT_KIND}:
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


def parse_termination_field_key(value: str) -> dict[str, str]:
    """Parse an exact canonical termination field key."""
    try:
        data = json.loads(value)
        if not isinstance(data, dict) or set(data) != {"cards", "device", "kind", "port", "role"}:
            raise ValueError
        if not all(isinstance(data[name], str) for name in data):
            raise ValueError
        canonical = termination_field_key(**data)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"'{value}' is not a canonical termination field key.") from exc
    if canonical != value:
        raise ValueError(f"'{value}' is not a canonical termination field key.")
    return data


__all__ = (
    "FRONT_PORT_CLASSES",
    "FRONT_PORT_KIND",
    "INTERFACE_KIND",
    "INTERFACE_PORT_CLASSES",
    "MAPPED_PEER_ROLE",
    "PORT_CLASSES",
    "PORT_CLASS_CLAIMED_KINDS",
    "REAR_PORT_CLASSES",
    "REAR_PORT_KIND",
    "SELECT_TERMINATION_TASK",
    "TERMINATION_ROLE",
    "TERMINATION_ROLES",
    "claimed_termination_kind",
    "parse_termination_field_key",
    "same_device_and_cards",
    "termination_field_key",
)
