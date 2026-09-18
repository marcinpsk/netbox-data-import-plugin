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
        signal.setitimer(signal.ITIMER_REAL, timeout)
        answers = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        addresses = list(dict.fromkeys(str(answer[4][0]) for answer in answers))
    except Exception:  # noqa: BLE001 - the parent exposes one typed resolution failure
        json.dump({"error": "Name resolution failed."}, sys.stdout)
        return 1
    json.dump(addresses, sys.stdout)
    return 0


if __name__ == "__main__":  # pragma: no cover - child entry point; main() is tested in process
    raise SystemExit(main())
