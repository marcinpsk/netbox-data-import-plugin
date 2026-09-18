# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
r"""Reject an unescaped `|` inside a code span in a Markdown table row.

GitHub Flavored Markdown reads `|` as a cell separator even inside backticks, so the rest of the
value becomes an extra cell and disappears from the rendered table. Python-Markdown, which builds
the MkDocs site, keeps it. A reader who copies the value from GitHub gets a truncated one.

The repository renders every page in both, so a value that holds an unescaped `|` belongs in a
fenced block instead of a table cell. An escaped `\|` renders as a pipe in both, so it may stay.
"""

import pathlib
import re
import tempfile

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
                if re.search(r"(?<!\\)(?:\\\\)*\|", span):
                    yield f"{path.relative_to(REPOSITORY)}:{number} {span}"


class MarkdownTableRenderingTest(SimpleTestCase):
    """Every Markdown table renders the same on GitHub and in the MkDocs site."""

    def _offenders_for(self, code_span):
        """Run the repository guard over one explicit temporary table row."""
        with tempfile.TemporaryDirectory(dir=REPOSITORY) as directory:
            path = pathlib.Path(directory) / "table.md"
            path.write_text(f"| Value |\n| --- |\n| `{code_span}` |\n")
            return list(_table_rows_with_a_piped_code_span(path))

    def test_an_escaped_pipe_in_a_code_span_is_accepted(self):
        self.assertEqual(self._offenders_for(r"left\|right"), [])

    def test_an_unescaped_pipe_in_a_code_span_is_rejected(self):
        self.assertEqual(len(self._offenders_for("left|right")), 1)

    def test_no_table_cell_hides_a_cell_separator_in_a_code_span(self):
        offenders = [
            row
            for path in sorted(REPOSITORY.glob("*.md")) + sorted(REPOSITORY.glob("docs/**/*.md"))
            for row in _table_rows_with_a_piped_code_span(path)
        ]
        self.assertEqual(offenders, [])
