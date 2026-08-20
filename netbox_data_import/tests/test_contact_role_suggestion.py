# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The Contact modal proposes a role for each candidate value it can recognize."""

from django.test import SimpleTestCase

from netbox_data_import.contact_resolution import suggest_contact_roles


class SuggestContactRolesTest(SimpleTestCase):
    """The value shape decides email and phone; a header keyword decides name."""

    def test_a_workbook_row_maps_all_three_fields(self):
        """The sample layout is the case the operator meets most often."""
        suggestions = suggest_contact_roles(
            {
                "Primary Contact": "grace.hopper@example.invalid",
                "Owner": "Lab Ops",
                "Contact": "Grace Hopper",
                "Contact Number": "+44 20 7946 0102",
            }
        )

        self.assertEqual(
            suggestions,
            {"email": "Primary Contact", "phone": "Contact Number", "name": "Contact"},
        )

    def test_a_column_the_suggestion_cannot_place_is_left_out(self):
        """`Owner` holds an organization, so no field claims it and the operator decides."""
        suggestions = suggest_contact_roles({"Owner": "Lab Ops"})

        self.assertEqual(suggestions, {})

    def test_an_address_is_recognized_wherever_the_column_sits(self):
        """A header gives no hint here, so the address itself has to carry the decision."""
        suggestions = suggest_contact_roles({"Column 7": "ada@example.invalid"})

        self.assertEqual(suggestions, {"email": "Column 7"})

    def test_a_header_keyword_breaks_a_tie_between_two_addresses(self):
        """Two columns hold addresses, so the header decides which one is the Contact email."""
        suggestions = suggest_contact_roles(
            {
                "Reported By": "helpdesk@example.invalid",
                "Email Address": "ada@example.invalid",
            }
        )

        self.assertEqual(suggestions["email"], "Email Address")

    def test_two_indistinguishable_addresses_propose_nothing(self):
        """Picking one by column order would store an arbitrary identity under a one-click save."""
        suggestions = suggest_contact_roles(
            {
                "Office": "alice.office@example.invalid",
                "Personal": "alice.personal@example.invalid",
            }
        )

        self.assertNotIn("email", suggestions)
        # An address is not a name either, whatever its column is called.
        self.assertNotIn("name", suggestions)

    def test_two_indistinguishable_numbers_propose_nothing(self):
        """The same reasoning applies to the phone field."""
        suggestions = suggest_contact_roles({"Desk": "+1 202-555-0111", "Backup": "+1 202-555-0112"})

        self.assertNotIn("phone", suggestions)

    def test_a_phone_number_survives_its_punctuation(self):
        """Source files write numbers with spaces, dashes, dots, and parentheses."""
        for written in ("+44 20 7946 0102", "(020) 7946-0102", "020.7946.0102", "+1-202-555-0106"):
            with self.subTest(written=written):
                self.assertEqual(suggest_contact_roles({"Contact Number": written}), {"phone": "Contact Number"})

    def test_a_short_number_is_not_a_phone_number(self):
        """A rack unit or an asset count must never land in the phone field."""
        suggestions = suggest_contact_roles({"Contact Number": "42"})

        self.assertEqual(suggestions, {})

    def test_no_column_is_proposed_for_two_fields(self):
        """One column supplies at most one Contact field, so the proposal cannot double book."""
        suggestions = suggest_contact_roles(
            {
                "Contact Name": "grace@example.invalid",
                "Contact Phone": "+44 20 7946 0102",
            }
        )

        self.assertEqual(len(set(suggestions.values())), len(suggestions))

    def test_an_empty_row_proposes_nothing(self):
        """A row with no candidate values must not raise on the way to an empty proposal."""
        self.assertEqual(suggest_contact_roles({}), {})
