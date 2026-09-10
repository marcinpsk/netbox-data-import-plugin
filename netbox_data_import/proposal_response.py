# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The strict Resolution Proposal response validator (specification 7.8).

Pure: it reads the returned text and the immutable request snapshot, and returns one answer or
raises. It never repairs a response, never fuzzy-matches an identifier, and never asks again.
"""

import json

from dataclasses import dataclass

RESPONSE_SCHEMA_VERSION = 1
EXPLANATION_MAX_LENGTH = 2000

#: Section 7.8 fixes the response object exactly, so an unexpected member is a response we do not know.
RESPONSE_MEMBERS = frozenset({"schema_version", "outcome", "candidate_id", "explanation"})

OUTCOME_CANDIDATE = "candidate"
OUTCOME_NO_MATCH = "no_match"
OUTCOMES = (OUTCOME_CANDIDATE, OUTCOME_NO_MATCH)

__all__ = [
    "EXPLANATION_MAX_LENGTH",
    "OUTCOMES",
    "OUTCOME_CANDIDATE",
    "OUTCOME_NO_MATCH",
    "RESPONSE_SCHEMA_VERSION",
    "InvalidProposalResponse",
    "ProposalAnswer",
    "candidate_id_for",
    "validate_candidate_ids",
    "validate_response",
]


class InvalidProposalResponse(Exception):
    """The response is not one valid object for this request. Stored reason: `invalid_response`."""


@dataclass(frozen=True)
class ProposalAnswer:
    """What one valid response concluded. `candidate_id` is None exactly when the outcome is no_match."""

    outcome: str
    candidate_id: str | None
    explanation: str


def candidate_id_for(position: int) -> str:
    """Return the opaque id for a candidate at *position*, counted from zero (section 7.8)."""
    return f"candidate-{position + 1:04d}"


def validate_candidate_ids(candidate_ids) -> tuple[str, ...]:
    """Return the request's identifiers, refusing a duplicate before the call is ever made."""
    ids = tuple(candidate_ids)
    if len(set(ids)) != len(ids):
        raise ValueError("Candidate identifiers must be unique within one request.")
    return ids


def _no_duplicate_members(pairs):
    """Reject a repeated member while decoding, because `json.loads` keeps only the last one."""
    seen = {}
    for name, value in pairs:
        if name in seen:
            raise InvalidProposalResponse(f"The response repeats the member '{name}'.")
        seen[name] = value
    return seen


def _decode(content_text):
    """Return the one JSON object the response has to be."""
    if not isinstance(content_text, str) or not content_text.strip():
        raise InvalidProposalResponse("The response carries no content.")
    try:
        decoded = json.loads(content_text, object_pairs_hook=_no_duplicate_members)
    except InvalidProposalResponse:
        raise
    except (ValueError, RecursionError) as exc:
        raise InvalidProposalResponse("The response is not JSON.") from exc
    if not isinstance(decoded, dict):
        raise InvalidProposalResponse("The response is not one JSON object.")
    return decoded


def _explanation(value):
    """Return the required explanation, which the operator reads to understand the answer."""
    if not isinstance(value, str) or not value.strip():
        raise InvalidProposalResponse("The response carries no explanation.")
    if len(value) > EXPLANATION_MAX_LENGTH:
        raise InvalidProposalResponse(f"The explanation is longer than {EXPLANATION_MAX_LENGTH} characters.")
    return value


def validate_response(content_text, *, candidate_ids, schema_version=RESPONSE_SCHEMA_VERSION) -> ProposalAnswer:
    """Return the answer one response states, or raise if it is not exactly one valid answer.

    Identifiers are compared as exact opaque strings: an invented, missing or reshaped one is an
    invalid response, never a near match.
    """
    decoded = _decode(content_text)
    unknown = set(decoded) - RESPONSE_MEMBERS
    if unknown:
        raise InvalidProposalResponse(f"The response carries unknown members: {', '.join(sorted(unknown))}.")
    missing = RESPONSE_MEMBERS - set(decoded)
    if missing:
        raise InvalidProposalResponse(f"The response is missing: {', '.join(sorted(missing))}.")

    # `True` is numerically 1, so a bool would otherwise pass as schema_version 1.
    version = decoded["schema_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != schema_version:
        raise InvalidProposalResponse(f"The response declares schema_version {version!r}, not {schema_version}.")

    outcome = decoded["outcome"]
    if outcome not in OUTCOMES:
        raise InvalidProposalResponse(f"The response declares outcome {outcome!r}.")

    explanation = _explanation(decoded["explanation"])
    candidate_id = decoded["candidate_id"]
    if outcome == OUTCOME_NO_MATCH:
        if candidate_id is not None:
            raise InvalidProposalResponse("A no_match response carries a candidate_id.")
        return ProposalAnswer(outcome=OUTCOME_NO_MATCH, candidate_id=None, explanation=explanation)

    if not isinstance(candidate_id, str) or candidate_id not in validate_candidate_ids(candidate_ids):
        raise InvalidProposalResponse("The response selects a candidate that this request did not offer.")
    return ProposalAnswer(outcome=OUTCOME_CANDIDATE, candidate_id=candidate_id, explanation=explanation)
