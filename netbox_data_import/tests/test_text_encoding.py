# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Text reads and writes name their encoding, so the runner's locale cannot decide it."""

import ast
import inspect
import pathlib

from django.test import SimpleTestCase

REPOSITORY = pathlib.Path(__file__).resolve().parents[2]
TEXT_METHODS = frozenset({"read_text", "write_text"})


def _encoding_positions():
    """Map each text method to the positional index its encoding argument occupies."""
    positions = {}
    for method in TEXT_METHODS:
        parameters = inspect.signature(getattr(pathlib.Path, method)).parameters.values()
        names = [
            parameter.name
            for parameter in parameters
            if parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
        ]
        positions[method] = names.index("encoding") - 1
    return positions


ENCODING_POSITIONS = _encoding_positions()
# The OpenGrep fixtures are deliberate violations, and ruff skips them for the same reason.
SKIPPED = ("/.git/", "/.venv/", "/node_modules/", "/build/", "/dist/", "/.opengrep/fixtures/")


def _python_files():
    """Yield every Python file the repository owns."""
    for path in sorted(REPOSITORY.rglob("*.py")):
        if not any(part in f"/{path.relative_to(REPOSITORY).as_posix()}/" for part in SKIPPED):
            yield path


def _receiver_is_a_class(value):
    """Say whether a text call goes through a class rather than an instance."""
    # `pathlib.Path.write_text(path, data)` passes the receiver itself, shifting every index.
    name = value.attr if isinstance(value, ast.Attribute) else getattr(value, "id", "")
    return name[:1].isupper()


def _names_an_encoding(node, position):
    """Say whether one text call names an encoding the runner's locale cannot decide."""
    named = next((keyword.value for keyword in node.keywords if keyword.arg == "encoding"), None)
    # A splat hides how many arguments reach *position*, and an unbound call shifts it.
    if named is None and not any(isinstance(argument, ast.Starred) for argument in node.args):
        if _receiver_is_a_class(node.func.value):
            return False
        named = node.args[position] if len(node.args) > position else None
    return named is not None and not (isinstance(named, ast.Constant) and named.value is None)


def _text_calls(tree):
    """Yield each text read or write with whether it names an encoding."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in TEXT_METHODS:
            continue
        yield node, _names_an_encoding(node, ENCODING_POSITIONS[node.func.attr])


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


class EncodingGuardTest(SimpleTestCase):
    """Each signature says where its encoding sits, so the guard reads it from pathlib."""

    def _unencoded_calls(self, source):
        """Run the guard over one snippet and name the calls it reports."""
        return [node.func.attr for node, encoded in _text_calls(ast.parse(source)) if not encoded]

    def test_a_positional_read_encoding_is_accepted(self):
        self.assertEqual(self._unencoded_calls('path.read_text("utf-8")'), [])

    def test_a_positional_write_encoding_is_accepted(self):
        self.assertEqual(self._unencoded_calls('path.write_text(data, "utf-8")'), [])

    def test_a_read_without_an_encoding_is_reported(self):
        self.assertEqual(self._unencoded_calls("path.read_text()"), ["read_text"])

    def test_a_write_carrying_only_its_data_is_reported(self):
        self.assertEqual(self._unencoded_calls("path.write_text(data)"), ["write_text"])

    def test_an_explicit_none_encoding_is_reported(self):
        self.assertEqual(self._unencoded_calls("path.write_text(data, None)"), ["write_text"])
        self.assertEqual(self._unencoded_calls("path.read_text(encoding=None)"), ["read_text"])

    def test_an_unbound_call_passing_its_receiver_is_reported(self):
        self.assertEqual(self._unencoded_calls("pathlib.Path.read_text(path)"), ["read_text"])
        self.assertEqual(self._unencoded_calls("pathlib.Path.write_text(path, data)"), ["write_text"])

    def test_an_unbound_call_that_names_its_encoding_is_accepted(self):
        self.assertEqual(self._unencoded_calls('pathlib.Path.write_text(path, data, encoding="utf-8")'), [])

    def test_a_splatted_argument_list_is_reported(self):
        self.assertEqual(self._unencoded_calls("path.write_text(data, *rest)"), ["write_text"])
