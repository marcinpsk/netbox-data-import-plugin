# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Reject a new multi-line comment block, and a recorded one that no longer exists.

A run of two or more whole-line `#` comments is a block, checked against `comment_blocks.json`. A
block missing from that record fails as new, and a recorded block whose first line changed fails as
stale, so neither the debt nor the record grows quietly.

Banners, blank `#` lines and pragmas separate rather than explain, so they neither count nor join
two blocks. Migrations are excluded, matching the ruff `per-file-ignores` carve-out.
"""

import json
import pathlib
import tokenize
from collections import Counter

from django.test import SimpleTestCase

PACKAGE = pathlib.Path(__file__).resolve().parents[1]
BASELINE = pathlib.Path(__file__).resolve().parent / "comment_blocks.json"

_PRAGMAS = ("# noqa", "# type:", "# ruff:", "# fmt:", "# pragma:", "# SPDX", "# Copyright")
_RULE_CHARACTERS = set("-=*_")


def _is_banner(text):
    """Return True for a `# ---` rule or a bare `#`, which separate rather than explain."""
    return not set(text.lstrip("#").strip()) - _RULE_CHARACTERS


def _own_line_comments(path):
    """Map each line number carrying a whole-line explanatory comment to its text."""
    lines = path.read_text().splitlines()
    found = {}
    with path.open("rb") as handle:
        for token in tokenize.tokenize(handle.readline):
            if token.type != tokenize.COMMENT:
                continue
            row = token.start[0]
            # A trailing comment explains one statement, so it never joins the block above it.
            if not lines[row - 1].lstrip().startswith("#"):
                continue
            text = token.string.strip()
            if not _is_banner(text) and not text.startswith(_PRAGMAS):
                found[row] = text
    return found


def _block_first_lines(path):
    """Yield the first line of every run of two or more consecutive whole-line comments."""
    comments = _own_line_comments(path)
    for row in sorted(comments):
        if row - 1 in comments:
            continue
        length = 0
        while row + length in comments:
            length += 1
        if length > 1:
            yield comments[row]


def _blocks_in_package():
    """Return the multi-line comment blocks the package holds, keyed by repository path."""
    found = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        if "migrations" in path.parts:
            continue
        first_lines = list(_block_first_lines(path))
        if first_lines:
            found[path.relative_to(PACKAGE.parent).as_posix()] = Counter(first_lines)
    return found


class CommentBlocksStayOnOneLineTest(SimpleTestCase):
    """The multi-line comment blocks the package still holds are recorded, and the record shrinks."""

    # The whole value of the guard is the list it prints, so it is never worth truncating.
    maxDiff = None

    def test_no_new_comment_block_runs_past_one_line(self):
        """A block absent from the record is new debt, whoever wrote it."""
        recorded = {path: Counter(texts) for path, texts in json.loads(BASELINE.read_text()).items()}
        found = _blocks_in_package()

        added = [
            f"{path}: {text}"
            for path in sorted(found)
            for text in sorted((found[path] - recorded.get(path, Counter())).elements())
        ]

        self.assertEqual(
            added,
            [],
            "Give each of these its reason in one line. Record it in comment_blocks.json only if it cannot be.",
        )

    def test_the_recorded_blocks_all_still_exist(self):
        """A fixed block has to leave the record, or the record stops meaning anything."""
        recorded = {path: Counter(texts) for path, texts in json.loads(BASELINE.read_text()).items()}
        found = _blocks_in_package()

        stale = [
            f"{path}: {text}"
            for path in sorted(recorded)
            for text in sorted((recorded[path] - found.get(path, Counter())).elements())
        ]

        self.assertEqual(stale, [], "These are fixed or moved. Delete them from comment_blocks.json.")
