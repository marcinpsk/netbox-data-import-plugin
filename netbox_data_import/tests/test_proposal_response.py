# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The strict Resolution Proposal response validator (specification 7.8)."""

import json

from django.test import SimpleTestCase

from netbox_data_import.proposal_response import (
    EXPLANATION_MAX_LENGTH,
    OUTCOME_CANDIDATE,
    OUTCOME_NO_MATCH,
    InvalidProposalResponse,
    candidate_id_for,
    validate_candidate_ids,
    validate_response,
)

CANDIDATES = ("candidate-0001", "candidate-0002", "candidate-0003")


def response(**overrides):
    """Return the JSON text of one valid candidate response, with *overrides* applied."""
    body = {
        "schema_version": 1,
        "outcome": OUTCOME_CANDIDATE,
        "candidate_id": "candidate-0002",
        "explanation": "The port name matches this interface exactly.",
    }
    body.update(overrides)
    return json.dumps(body)


class ValidResponseTest(SimpleTestCase):
    """The two answers a proposal may conclude."""

    def test_a_candidate_response_returns_its_selection(self):
        answer = validate_response(response(), candidate_ids=CANDIDATES)

        self.assertEqual(answer.outcome, OUTCOME_CANDIDATE)
        self.assertEqual(answer.candidate_id, "candidate-0002")
        self.assertEqual(answer.explanation, "The port name matches this interface exactly.")

    def test_a_no_match_response_carries_no_selection(self):
        text = response(outcome=OUTCOME_NO_MATCH, candidate_id=None, explanation="Two ports fit equally.")

        answer = validate_response(text, candidate_ids=CANDIDATES)

        self.assertEqual(answer.outcome, OUTCOME_NO_MATCH)
        self.assertIsNone(answer.candidate_id)

    def test_an_explanation_at_the_limit_is_accepted(self):
        answer = validate_response(response(explanation="x" * EXPLANATION_MAX_LENGTH), candidate_ids=CANDIDATES)

        self.assertEqual(len(answer.explanation), EXPLANATION_MAX_LENGTH)


class RejectedResponseTest(SimpleTestCase):
    """Everything section 7.8 classifies as an invalid backend response."""

    def assert_rejected(self, text, candidate_ids=CANDIDATES):
        """Require the validator to refuse *text* rather than repair it."""
        with self.assertRaises(InvalidProposalResponse) as caught:
            validate_response(text, candidate_ids=candidate_ids)
        return str(caught.exception)

    def test_a_repeated_member_is_rejected(self):
        """`json.loads` keeps the last duplicate, so a check after decoding cannot see there was one."""
        text = '{"schema_version": 1, "outcome": "candidate", "outcome": "no_match", "candidate_id": null, "explanation": "x"}'

        self.assertIn("repeats the member", self.assert_rejected(text))

    def test_text_that_is_not_json_is_rejected(self):
        self.assert_rejected("not json at all")

    def test_a_json_array_is_not_one_object(self):
        self.assert_rejected('[{"schema_version": 1}]')

    def test_a_json_scalar_is_not_one_object(self):
        self.assert_rejected("42")

    def test_empty_content_is_rejected(self):
        for text in ("", "   ", None):
            with self.subTest(text=text):
                self.assert_rejected(text)

    def test_an_unknown_member_is_rejected(self):
        self.assert_rejected(response(confidence=0.9))

    def test_every_missing_member_is_rejected(self):
        for member in ("schema_version", "outcome", "candidate_id", "explanation"):
            with self.subTest(member=member):
                body = json.loads(response())
                del body[member]

                self.assertIn(member, self.assert_rejected(json.dumps(body)))

    def test_a_wrong_schema_version_is_rejected(self):
        self.assert_rejected(response(schema_version=2))

    def test_a_string_schema_version_is_rejected(self):
        self.assert_rejected(response(schema_version="1"))

    def test_a_boolean_schema_version_is_rejected(self):
        """`True` is numerically 1, so it would otherwise pass an equality check against version 1."""
        self.assert_rejected(response(schema_version=True))

    def test_an_unknown_outcome_is_rejected(self):
        self.assert_rejected(response(outcome="maybe"))

    def test_an_empty_explanation_is_rejected(self):
        for explanation in ("", "   ", None, 7):
            with self.subTest(explanation=explanation):
                self.assert_rejected(response(explanation=explanation))

    def test_an_over_long_explanation_is_rejected(self):
        self.assert_rejected(response(explanation="x" * (EXPLANATION_MAX_LENGTH + 1)))

    def test_a_no_match_carrying_a_candidate_is_rejected(self):
        self.assert_rejected(response(outcome=OUTCOME_NO_MATCH))

    def test_an_invented_candidate_is_rejected(self):
        self.assert_rejected(response(candidate_id="candidate-9999"))

    def test_a_candidate_id_is_compared_exactly(self):
        """Never trim, normalize or fuzzy-match: a reshaped id is an invalid response, not a near miss."""
        for invented in (" candidate-0002", "candidate-0002 ", "CANDIDATE-0002", "candidate-2"):
            with self.subTest(candidate_id=invented):
                self.assert_rejected(response(candidate_id=invented))

    def test_a_non_string_candidate_id_is_rejected(self):
        self.assert_rejected(response(candidate_id=2))

    def test_a_candidate_response_against_an_empty_request_set_is_rejected(self):
        self.assert_rejected(response(), candidate_ids=())


class CandidateIdentifierTest(SimpleTestCase):
    """Identifiers are opaque and generated per request (section 7.8)."""

    def test_identifiers_are_zero_padded_from_one(self):
        self.assertEqual(candidate_id_for(0), "candidate-0001")
        self.assertEqual(candidate_id_for(47), "candidate-0048")

    def test_a_duplicate_identifier_is_refused_before_the_call(self):
        with self.assertRaises(ValueError):
            validate_candidate_ids(("candidate-0001", "candidate-0001"))

    def test_unique_identifiers_are_returned_in_order(self):
        self.assertEqual(validate_candidate_ids(CANDIDATES), CANDIDATES)
