# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Resolve one host with libc in a deadline-bounded child process."""

import json
import signal
import socket
import sys


def main() -> int:
    """Read one request from stdin and write its ordered addresses to stdout."""
    try:
        host, port, timeout = json.load(sys.stdin)
    except Exception:  # noqa: BLE001 - the parent exposes one typed resolution failure
        return 1
    # The alarm is the deadline. An unarmed worker must crash, not read as a failed lookup.
    signal.setitimer(signal.ITIMER_REAL, timeout)
    try:
        answers = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        addresses = list(dict.fromkeys(str(answer[4][0]) for answer in answers))
    except Exception:  # noqa: BLE001 - the parent exposes one typed resolution failure
        return 1
    json.dump(addresses, sys.stdout)
    return 0


if __name__ == "__main__":  # pragma: no cover - child entry point; main() is tested in process
    raise SystemExit(main())
