# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for shared test helpers."""

import sys

from django.test import SimpleTestCase

from netbox_data_import.tests.helpers import assert_absent_from


class AssertAbsentFromTest(SimpleTestCase):
    """Inspect decoded structures without changing the strings under test."""

    def test_a_needle_in_a_dict_key_reports_the_key_path(self):
        with self.assertRaisesRegex(AssertionError, r"\$\['outer'\] keys\[0\]"):
            assert_absent_from(self, {"outer": {"hidden-name": 1}}, "hidden")

    def test_a_needle_in_nested_json_text_reports_the_decoded_path(self):
        with self.assertRaisesRegex(AssertionError, r"\$\['body'\] decoded JSON"):
            assert_absent_from(self, {"body": '{"members": [{"hidden\\"name": true}]}'}, 'hidden"name')

    def test_an_escape_bearing_needle_reports_its_list_path(self):
        needle = 'hidden "value\\\tpart'

        with self.assertRaisesRegex(AssertionError, r"\$\[1\]"):
            assert_absent_from(self, ["safe", f"prefix {needle} suffix"], needle)

    def test_a_deep_decoded_structure_cannot_escape_inspection(self):
        previous_limit = sys.getrecursionlimit()
        self.addCleanup(sys.setrecursionlimit, previous_limit)
        sys.setrecursionlimit(1000)
        payload = {"body": "[" * 995 + '"\\u0068idden"' + "]" * 995}

        with self.assertRaisesRegex(AssertionError, r"\$\['body'\] decoded JSON"):
            assert_absent_from(self, payload, "hidden")

    def test_a_clean_structure_passes(self):
        assert_absent_from(
            self,
            {"safe": ["text", (1, None, True), '{"nested": ["still safe"]}']},
            "hidden",
        )
