# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""GraphQL filter compatibility contracts."""

from unittest.mock import patch

import strawberry_django
from django.test import SimpleTestCase

from netbox_data_import.graphql import filters


class StringFilterLookupCompatibilityTest(SimpleTestCase):
    """Legacy Strawberry releases expose the same string lookup vocabulary."""

    def test_the_legacy_fallback_exposes_only_string_lookups(self):
        """A generic legacy lookup must not add comparison operations to text fields."""
        with patch.object(strawberry_django, "StrFilterLookup", None, create=True):
            lookup = filters._string_filter_lookup()

        self.assertIs(lookup, filters._FallbackStrFilterLookup)
        self.assertSetEqual(
            {field.python_name for field in lookup.__strawberry_definition__.fields},
            {
                "contains",
                "ends_with",
                "exact",
                "i_contains",
                "i_ends_with",
                "i_exact",
                "i_regex",
                "i_starts_with",
                "in_list",
                "is_null",
                "regex",
                "starts_with",
            },
        )
