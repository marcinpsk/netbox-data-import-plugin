# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Extract authoritative preview rows for asynchronous row actions."""

from html.parser import HTMLParser


class _PreviewRowExtractor(HTMLParser):
    """Capture selected outer table rows without reconstructing their markup."""

    def __init__(self, target_ids):
        super().__init__(convert_charrefs=False)
        self.target_ids = set(target_ids)
        self.fragments = {}
        self._target_id = None
        self._tr_depth = 0
        self._parts = []

    def handle_starttag(self, tag, attrs):
        start = self.get_starttag_text()
        element_id = dict(attrs).get("id")
        if self._target_id is None and tag == "tr" and element_id in self.target_ids:
            self._target_id = element_id
            self._tr_depth = 1
            self._parts = [start]
            return
        if self._target_id is not None:
            self._parts.append(start)
            if tag == "tr":
                self._tr_depth += 1

    def handle_startendtag(self, tag, attrs):
        if self._target_id is not None:
            self._parts.append(self.get_starttag_text())

    def handle_endtag(self, tag):
        if self._target_id is None:
            return
        self._parts.append(f"</{tag}>")
        if tag == "tr":
            self._tr_depth -= 1
            if self._tr_depth == 0:
                self.fragments[self._target_id] = "".join(self._parts)
                self._target_id = None
                self._parts = []

    def handle_data(self, data):
        if self._target_id is not None:
            self._parts.append(data)

    def handle_entityref(self, name):
        if self._target_id is not None:
            self._parts.append(f"&{name};")

    def handle_charref(self, name):
        if self._target_id is not None:
            self._parts.append(f"&#{name};")

    def handle_comment(self, data):
        if self._target_id is not None:
            self._parts.append(f"<!--{data}-->")


def extract_preview_row(html: str, row_number: int) -> str:
    """Return the main and detail rows rendered by the full preview template."""
    target_ids = (f"row-{row_number}", f"diff-{row_number}")
    parser = _PreviewRowExtractor(target_ids)
    parser.feed(html)
    if target_ids[0] not in parser.fragments:
        raise ValueError("The refreshed preview no longer contains this row.")
    return "".join(parser.fragments[target_id] for target_id in target_ids if target_id in parser.fragments)
