# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Reads the bounded-response rule must catch, and the bounded form it must not."""

from netbox_data_import.inference_transport import request_to_resolved_address


def missed_unbounded_read(session, url, address):
    # ruleid: nbdi-bounded-response-body
    return request_to_resolved_address(session, "GET", url, address)


def missed_unbounded_read_with_other_keywords(session, url, address, deadline):
    # ruleid: nbdi-bounded-response-body
    return request_to_resolved_address(session, "GET", url, address, deadline=deadline, allow_redirects=False)


def bounded_read_is_fine(session, url, address):
    # ok: nbdi-bounded-response-body
    return request_to_resolved_address(session, "GET", url, address, response_body_limit=1024)
