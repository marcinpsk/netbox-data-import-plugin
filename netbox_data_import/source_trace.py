# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Adapter-neutral source values for traced physical paths."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .field_keys import PORT_CLASS_CLAIMED_KINDS, REAR_PORT_CLASSES
from .values import identity_text

if TYPE_CHECKING:
    from .adapters import SourceDiagnostic


IdentityKey = tuple[str, str, str, str]


@dataclass(frozen=True)
class TerminationReference:
    """Name one source termination and retain its corroboration values."""

    device: str
    cards: str
    port: str
    port_class: str
    u_position: str = ""
    rack: str = ""
    location: str = ""

    @property
    def identity_key(self) -> IdentityKey:
        """Return the normalized device, cards, port, and claimed-kind identity."""
        return (
            identity_text(self.device),
            identity_text(self.cards),
            identity_text(self.port),
            PORT_CLASS_CLAIMED_KINDS.get(self.port_class, identity_text(self.port_class)),
        )


@dataclass(frozen=True)
class EndpointSummary:
    """Retain the source From and To statements in their original direction."""

    from_termination: TerminationReference
    to_termination: TerminationReference
    from_text: str
    to_text: str


@dataclass(frozen=True)
class SegmentEvidence:
    """Describe one source-claimed cable between two Termination References."""

    left: TerminationReference
    cable_class: str
    right: TerminationReference


@dataclass(frozen=True)
class PassThroughClaim:
    """Describe the source-claimed continuation through one device."""

    device: str
    cards: str
    entry_port: str
    exit_port: str


@dataclass(frozen=True)
class TraceProvenance:
    """Locate one Source Trace occurrence in its source document."""

    workbook_fingerprint: str
    sheet: str
    block_ordinal: int
    row_start: int
    row_end: int
    export_timestamp: str
    from_text: str
    to_text: str
    direction: str


@dataclass(frozen=True)
class SourceTrace:
    """Carry canonical Segment Evidence for one path or an Endpoint Summary fallback."""

    endpoint_summary: EndpointSummary
    segments: tuple[SegmentEvidence, ...]
    pass_through_claims: tuple[PassThroughClaim, ...]
    corroboration: tuple[TerminationReference, ...]
    identity: str
    content_fingerprint: str
    provenance: tuple[TraceProvenance, ...]
    errors: tuple[SourceDiagnostic, ...] = ()

    @property
    def valid(self) -> bool:
        """Return whether source validation found no errors on this trace."""
        return not self.errors

    @property
    def ends_at_rear_port(self) -> bool:
        """Return whether the original To termination is a rear port."""
        return self.endpoint_summary.to_termination.port_class in REAR_PORT_CLASSES


__all__ = (
    "EndpointSummary",
    "IdentityKey",
    "PassThroughClaim",
    "SegmentEvidence",
    "SourceTrace",
    "TerminationReference",
    "TraceProvenance",
)
