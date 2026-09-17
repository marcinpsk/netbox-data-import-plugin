# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Reject a `|` inside a code span in a Markdown table row.

GitHub Flavored Markdown reads `|` as a cell separator even inside backticks, so the rest of the
value becomes an extra cell and disappears from the rendered table. Python-Markdown, which builds
the MkDocs site, keeps it. A reader who copies the value from GitHub gets a truncated one.

The repository renders every page in both, so a value that holds a `|` belongs in a fenced block
instead of a table cell.
"""

import pathlib
import re

from django.test import SimpleTestCase

REPOSITORY = pathlib.Path(__file__).resolve().parents[2]


def _table_rows_with_a_piped_code_span(path):
    """Yield each table row in *path* whose code span holds a cell separator."""
    inside_fence = False
    for number, line in enumerate(path.read_text().splitlines(), 1):
        if line.lstrip().startswith("```"):
            inside_fence = not inside_fence
        elif not inside_fence and line.startswith("|"):
            for span in re.findall(r"`[^`]*`", line):
                if "|" in span:
                    yield f"{path.relative_to(REPOSITORY)}:{number} {span}"


class MarkdownTableRenderingTest(SimpleTestCase):
    """Every Markdown table renders the same on GitHub and in the MkDocs site."""

    def test_no_table_cell_hides_a_cell_separator_in_a_code_span(self):
        offenders = [
            row
            for path in sorted(REPOSITORY.glob("*.md")) + sorted(REPOSITORY.glob("docs/**/*.md"))
            for row in _table_rows_with_a_piped_code_span(path)
        ]
        self.assertEqual(offenders, [])
