# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""GraphQL filter contracts."""

import strawberry_django
from django.test import SimpleTestCase

from netbox_data_import.graphql import filters


class StringFilterLookupContractTest(SimpleTestCase):
    """The supported Strawberry release supplies the string lookup input."""

    def test_profile_filters_use_the_supported_lookup_directly(self):
        """The NetBox version floor makes a local compatibility input unnecessary."""
        self.assertFalse(hasattr(filters, "_FallbackStrFilterLookup"))
        self.assertFalse(hasattr(filters, "_string_filter_lookup"))
        for field_name in ("name", "description", "source_adapter"):
            with self.subTest(field=field_name):
                self.assertEqual(
                    filters.ImportProfileFilter.__annotations__[field_name],
                    strawberry_django.StrFilterLookup | None,
                )
