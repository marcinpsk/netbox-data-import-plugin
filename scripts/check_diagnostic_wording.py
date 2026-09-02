# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Fail when a Target Module emits a diagnostic code the operator wording table does not answer.

`review_workspace._diagnostic_message` falls back to the raw code, which reads as an internal name
rather than an instruction. This is a static check over the source, so it runs as tooling instead of
in the Django test suite.
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys

PACKAGE = pathlib.Path(__file__).resolve().parents[1] / "netbox_data_import"
CODE = re.compile(r"(device|rack)\.[a-z_]+")
TABLE_ENTRY = re.compile(r'"((?:device|rack)\.[a-z_]+)":')


def emitted_codes(source: pathlib.Path) -> set[str]:
    """Return every diagnostic code the module names as a string constant."""
    return {
        node.value
        for node in ast.walk(ast.parse(source.read_text()))
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and CODE.fullmatch(node.value)
    }


def main() -> int:
    """Report each emitted code the wording table does not answer."""
    emitted = emitted_codes(PACKAGE / "target_modules.py")
    if not emitted:
        print("check-diagnostic-wording: found no diagnostic codes, so the scan is broken", file=sys.stderr)
        return 1
    answered = set(TABLE_ENTRY.findall((PACKAGE / "review_workspace.py").read_text()))
    missing = sorted(emitted - answered)
    if missing:
        print("check-diagnostic-wording: _DIAGNOSTIC_MESSAGES has no wording for:", file=sys.stderr)
        for code in missing:
            print(f"  {code}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
