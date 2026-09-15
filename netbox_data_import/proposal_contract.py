# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The shared Resolution Proposal response and persistence vocabulary."""

RESPONSE_SCHEMA_VERSION = 2
EXPLANATION_MAX_LENGTH = 2000
RESPONSE_MEMBER_NAMES = (
    "schema_version",
    "outcome",
    "candidate_id",
    "candidate_display_name",
    "explanation",
)
RESPONSE_MEMBERS = frozenset(RESPONSE_MEMBER_NAMES)

OUTCOME_CANDIDATE = "candidate"
OUTCOME_NO_MATCH = "no_match"
OUTCOMES = (OUTCOME_CANDIDATE, OUTCOME_NO_MATCH)
OUTCOME_CHOICES = ((OUTCOME_CANDIDATE, "Candidate"), (OUTCOME_NO_MATCH, "No match"))

__all__ = [
    "EXPLANATION_MAX_LENGTH",
    "OUTCOMES",
    "OUTCOME_CANDIDATE",
    "OUTCOME_CHOICES",
    "OUTCOME_NO_MATCH",
    "RESPONSE_MEMBERS",
    "RESPONSE_MEMBER_NAMES",
    "RESPONSE_SCHEMA_VERSION",
]
