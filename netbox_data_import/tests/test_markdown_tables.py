# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Reject an unescaped `|` in a table code span: GitHub ends the cell there, Python-Markdown does not."""

import pathlib
import re
import tempfile

from django.test import SimpleTestCase
from markdown_it import MarkdownIt

REPOSITORY = pathlib.Path(__file__).resolve().parents[2]
MARKDOWN = MarkdownIt("commonmark")
GFM = MarkdownIt("commonmark").enable("table")
UNESCAPED_PIPE = re.compile(r"(?<!\\)(?:\\\\)*\|")


def _line_numbers(document, parser, wanted):
    """Number every line the parser puts inside one of the *wanted* block tokens."""
    lines = set()
    for token in parser.parse(document):
        if token.type in wanted and token.map:
            lines.update(range(token.map[0] + 1, token.map[1] + 1))
    return lines


def _code_spans(line):
    """Yield the content of each code span on one line, at any backtick count."""
    for token in MARKDOWN.parseInline(line, {}):
        for child in token.children or []:
            if child.type == "code_inline":
                yield child.content


def _table_rows_with_a_piped_code_span(path):
    """Yield each table row in *path* whose code span holds a cell separator."""
    document = path.read_text(encoding="utf-8")
    fenced = _line_numbers(document, MARKDOWN, ("fence", "code_block"))
    rows = _line_numbers(document, GFM, ("table_open",))
    for number, line in enumerate(document.splitlines(), 1):
        # A pipe in a header breaks the cell count, so GFM emits no table to find the row in.
        if number in fenced or (number not in rows and not line.lstrip().startswith("|")):
            continue
        for span in _code_spans(line):
            if UNESCAPED_PIPE.search(span):
                yield f"{path.relative_to(REPOSITORY)}:{number} `{span}`"


class MarkdownTableRenderingTest(SimpleTestCase):
    """Every Markdown table renders the same on GitHub and in the MkDocs site."""

    def _offenders_for(self, code_span):
        """Run the repository guard over one explicit temporary table row."""
        with tempfile.TemporaryDirectory(dir=REPOSITORY) as directory:
            path = pathlib.Path(directory) / "table.md"
            path.write_text(f"| Value |\n| --- |\n| `{code_span}` |\n", encoding="utf-8")
            return list(_table_rows_with_a_piped_code_span(path))

    TABLE = "| V |\n| --- |\n| `left|right` |\n"

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
        self.assertEqual(self._offender_lines(f"~~~\n{self.TABLE}~~~\n"), [])

    def test_a_backtick_fence_hides_its_contents_from_the_guard(self):
        self.assertEqual(self._offender_lines(f"```\n{self.TABLE}```\n"), [])

    def test_a_longer_backtick_fence_is_not_closed_by_a_shorter_one(self):
        self.assertEqual(self._offender_lines(f"````\n```\n{self.TABLE}````\n"), [])

    def test_a_table_after_a_closed_tilde_fence_is_still_checked(self):
        self.assertEqual(self._offender_lines(f"~~~\ncode\n~~~\n\n{self.TABLE}"), [7])

    def test_an_over_indented_fence_does_not_close_a_block(self):
        self.assertEqual(self._offender_lines(f"```\n     ```\n{self.TABLE}```\n\n{self.TABLE}"), [10])

    def test_a_closing_fence_carrying_trailing_content_does_not_close_a_block(self):
        self.assertEqual(self._offender_lines(f"```\n```not-a-close\n{self.TABLE}```\n\n{self.TABLE}"), [10])

    def test_a_fence_nested_in_a_list_item_closes_on_its_own_indentation(self):
        self.assertEqual(self._offender_lines(f"- item\n\n  ```\n  code\n    ```\n\n{self.TABLE}"), [9])

    def test_a_backtick_in_an_info_string_opens_no_fence(self):
        self.assertEqual(self._offender_lines(f"```bad`info\n\n{self.TABLE}"), [5])

    def test_a_multi_backtick_code_span_is_scanned(self):
        self.assertEqual(self._offender_lines("| V |\n| --- |\n| ``left|right`` |\n"), [3])

    def test_an_indented_table_row_is_scanned(self):
        self.assertEqual(self._offender_lines("| V |\n| --- |\n  | `left|right` |\n"), [3])

    def test_a_table_row_without_a_leading_pipe_is_scanned(self):
        self.assertEqual(self._offender_lines("V | Other\n--- | ---\n`left|right` | x\n"), [3])

    def test_a_header_whose_pipe_breaks_the_table_is_still_scanned(self):
        """GFM counts two header cells against one delimiter and emits no table at all."""
        self.assertEqual(self._offender_lines("| `left|right` |\n| --- |\n| value |\n"), [1])

    def test_a_code_span_outside_a_table_is_not_an_offender(self):
        self.assertEqual(self._offender_lines("Some `left|right` in prose.\n"), [])

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
