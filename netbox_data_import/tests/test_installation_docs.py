# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Installation guidance matches the runtime contracts operators must migrate."""

from pathlib import Path

from django.test import SimpleTestCase

from netbox_data_import.transform_regex import TransformPattern


INSTALLATION_GUIDE = Path(__file__).resolve().parents[2] / "docs" / "installation.md"


class InstallationGuideRegexTest(SimpleTestCase):
    """The RE2 migration guide distinguishes its two Unicode behaviors."""

    def test_case_folding_is_unicode_while_word_classes_are_ascii(self):
        """The guide states the behavior exposed by the installed RE2 binding."""
        insensitive = TransformPattern.compile(r"(?i)(ä)")
        word = TransformPattern.compile(r"(\w)")

        self.assertEqual(insensitive.capture_groups("Ä"), ("Ä",))
        self.assertIsNone(TransformPattern.compile(r"(?i)(ß)").capture_groups("SS"))
        self.assertIsNone(word.capture_groups("Ä"))

        guide = " ".join(INSTALLATION_GUIDE.read_text().split())
        self.assertIn("uses Unicode simple case folding", guide)
        self.assertIn("families use ASCII semantics", guide)
