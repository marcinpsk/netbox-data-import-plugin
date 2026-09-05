# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Fail when a planner emits a diagnostic code the normative spec tables do not list.

The tables state the conditions and dispositions as normative, so a condition the planner enforces
without a row makes the spec wrong rather than incomplete. Two such rows were missing when this
check was written. Codes are read as string constants, because a code can reach its diagnostic
through a conditional expression or a keyword argument rather than a literal first argument.
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "netbox_data_import"
SPEC = ROOT / "docs" / "spec" / "target-neutral-import-architecture.md"
EMITTING_MODULES = ("cable_target.py", "models.py", "trace_workbook.py")
CODE = re.compile(r"(cable|trace)\.[a-z_]+")
TABLE_HEADER = re.compile(r"^\|\s*Condition\s*\|\s*Diagnostic code\s*\|")
TABLE_CODE = re.compile(r"`((?:cable|trace)\.[a-z_]+)`")


def emitted_codes() -> set[str]:
    """Return every Cable and trace diagnostic code the planner names as a string constant."""
    codes: set[str] = set()
    for name in EMITTING_MODULES:
        for node in ast.walk(ast.parse((PACKAGE / name).read_text())):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and CODE.fullmatch(node.value):
                codes.add(node.value)
    return codes


def documented_codes() -> set[str]:
    """Return the codes the normative diagnostics tables list, ignoring codes named in prose."""
    codes: set[str] = set()
    in_table = False
    for line in SPEC.read_text().splitlines():
        if TABLE_HEADER.match(line):
            in_table = True
            continue
        if in_table and not line.startswith("|"):
            in_table = False
        if in_table:
            codes.update(TABLE_CODE.findall(line))
    return codes


def main() -> int:
    """Report each emitted code the normative tables do not list."""
    emitted = emitted_codes()
    if not emitted:
        print("check-spec-diagnostics: found no diagnostic codes, so the scan is broken", file=sys.stderr)
        return 1
    documented = documented_codes()
    if not documented:
        print(f"check-spec-diagnostics: found no diagnostics table in {SPEC.name}", file=sys.stderr)
        return 1
    missing = sorted(emitted - documented)
    if missing:
        print("check-spec-diagnostics: the normative tables list no row for:", file=sys.stderr)
        for code in missing:
            print(f"  {code}", file=sys.stderr)
        return 1
    stale = sorted(documented - emitted)
    if stale:
        print("check-spec-diagnostics: the normative tables list codes no planner emits:", file=sys.stderr)
        for code in stale:
            print(f"  {code}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
