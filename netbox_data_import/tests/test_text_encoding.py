# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Text reads and writes name their encoding, so the caller's locale cannot decide it.

`Path.read_text` and `Path.write_text` fall back to `locale.getpreferredencoding(False)`. The
project supports Python 3.12 and newer and forces no UTF-8 mode, so on a non-UTF-8 locale a
repository file holding non-ASCII text raises `UnicodeDecodeError` instead of being read.
"""

import ast
import pathlib

from django.test import SimpleTestCase

REPOSITORY = pathlib.Path(__file__).resolve().parents[2]
TEXT_METHODS = frozenset({"read_text", "write_text"})
# The OpenGrep fixtures are deliberate violations, and ruff skips them for the same reason.
SKIPPED = ("/.git/", "/.venv/", "/node_modules/", "/build/", "/dist/", "/.opengrep/fixtures/")


def _python_files():
    """Yield every Python file the repository owns."""
    for path in sorted(REPOSITORY.rglob("*.py")):
        if not any(part in f"/{path.relative_to(REPOSITORY).as_posix()}/" for part in SKIPPED):
            yield path


def _text_calls(tree):
    """Yield each text read or write with whether it names an encoding."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in TEXT_METHODS:
            continue
        # read_text takes encoding first, so a positional argument already names it.
        positional = node.func.attr == "read_text" and bool(node.args)
        yield node, positional or any(keyword.arg == "encoding" for keyword in node.keywords)


class TextIoNamesItsEncodingTest(SimpleTestCase):
    """A read that depends on the runner's locale is a portability bug waiting for one."""

    def test_every_text_read_and_write_names_its_encoding(self):
        scanned = 0
        offenders = []
        for path in _python_files():
            for node, names_encoding in _text_calls(ast.parse(path.read_text(encoding="utf-8"))):
                scanned += 1
                if not names_encoding:
                    offenders.append(f"{path.relative_to(REPOSITORY)}:{node.lineno} {node.func.attr}")

        # A scan that matches nothing would pass this guard while the calls are renamed away.
        self.assertTrue(scanned, "the guard found no text read or write, so it checked nothing")
        self.assertEqual(offenders, [], "pass encoding='utf-8' to every text read and write")
