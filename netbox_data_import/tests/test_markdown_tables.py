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
from markdown_it import MarkdownIt

REPOSITORY = pathlib.Path(__file__).resolve().parents[2]
MARKDOWN = MarkdownIt("commonmark")


def _code_block_lines(document):
    """Number every line the Markdown parser reads as part of a code block."""
    inside = set()
    for token in MARKDOWN.parse(document):
        if token.type in ("fence", "code_block") and token.map:
            inside.update(range(token.map[0] + 1, token.map[1] + 1))
    return inside


def _table_rows_with_a_piped_code_span(path):
    """Yield each table row in *path* whose code span holds a cell separator."""
    document = path.read_text(encoding="utf-8")
    fenced = _code_block_lines(document)
    for number, line in enumerate(document.splitlines(), 1):
        if number in fenced or not line.startswith("|"):
            continue
        for span in re.findall(r"`[^`]*`", line):
            if re.search(r"(?<!\\)(?:\\\\)*\|", span):
                yield f"{path.relative_to(REPOSITORY)}:{number} {span}"


class MarkdownTableRenderingTest(SimpleTestCase):
    """Every Markdown table renders the same on GitHub and in the MkDocs site."""

    def _offenders_for(self, code_span):
        """Run the repository guard over one explicit temporary table row."""
        with tempfile.TemporaryDirectory(dir=REPOSITORY) as directory:
            path = pathlib.Path(directory) / "table.md"
            path.write_text(f"| Value |\n| --- |\n| `{code_span}` |\n", encoding="utf-8")
            return list(_table_rows_with_a_piped_code_span(path))

    def _offender_lines(self, document):
        """Run the repository guard over one document and number the rows it reports."""
        return [int(row.split(":")[1].split()[0]) for row in self._offenders_in(document)]

    def _offenders_in(self, document):
        """Run the repository guard over one explicit temporary document."""
        with tempfile.TemporaryDirectory(dir=REPOSITORY) as directory:
            path = pathlib.Path(directory) / "document.md"
            path.write_text(document, encoding="utf-8")
            return list(_table_rows_with_a_piped_code_span(path))

    def test_a_tilde_fence_hides_its_contents_from_the_guard(self):
        self.assertEqual(self._offenders_in("~~~\n| `left|right` |\n~~~\n"), [])

    def test_a_backtick_fence_hides_its_contents_from_the_guard(self):
        self.assertEqual(self._offenders_in("```\n| `left|right` |\n```\n"), [])

    def test_a_longer_backtick_fence_is_not_closed_by_a_shorter_one(self):
        self.assertEqual(self._offenders_in("````\n```\n| `left|right` |\n````\n"), [])

    def test_a_table_after_a_closed_tilde_fence_is_still_checked(self):
        self.assertEqual(len(self._offenders_in("~~~\ncode\n~~~\n| `left|right` |\n")), 1)

    def test_an_over_indented_fence_does_not_close_a_block(self):
        document = "```\n     ```\n| `left|right` |\n```\n| `left|right` |\n"
        self.assertEqual(self._offender_lines(document), [5])

    def test_a_closing_fence_carrying_trailing_content_does_not_close_a_block(self):
        document = "```\n```not-a-close\n| `left|right` |\n```\n| `left|right` |\n"
        self.assertEqual(self._offender_lines(document), [5])

    def test_a_fence_nested_in_a_list_item_closes_on_its_own_indentation(self):
        document = "- item\n\n  ```\n  code\n    ```\n\n| `left|right` |\n"
        self.assertEqual(self._offender_lines(document), [7])

    def test_a_backtick_in_an_info_string_opens_no_fence(self):
        self.assertEqual(self._offender_lines("```bad`info\n\n| `left|right` |\n"), [3])

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
