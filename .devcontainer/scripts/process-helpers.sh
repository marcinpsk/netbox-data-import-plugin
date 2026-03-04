#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
# Shared process management helpers.
# Sourced by load-aliases.sh and start-netbox.sh.

# Graceful termination: SIGTERM, wait, then SIGKILL if still alive.
graceful_kill_pid() {
  local pid="$1"
  [ -z "$pid" ] && return 1
  # Reject non-positive-integer PIDs to prevent accidental group kills
  case "$pid" in
    ''|*[!0-9]*) echo "graceful_kill_pid: invalid PID '$pid'" >&2; return 1 ;;
  esac
  [ "$pid" -le 0 ] 2>/dev/null && { echo "graceful_kill_pid: PID must be > 0" >&2; return 1; }
  kill -0 "$pid" 2>/dev/null || return 1
  kill -15 "$pid" 2>/dev/null || true
  sleep 2
  kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
}

graceful_kill_pattern() {
  local pattern="$1"
  [ -z "$pattern" ] && { echo "graceful_kill_pattern: empty pattern, refusing to kill" >&2; return 1; }
  pkill -15 -f "$pattern" 2>/dev/null || true
  sleep 2
  pgrep -f "$pattern" >/dev/null 2>&1 && pkill -9 -f "$pattern" 2>/dev/null || true
}

# Verify a PID matches the expected process before killing it
is_expected_pid() {
  local pid="$1" pattern="$2"
  [ -z "$pid" ] || [ -z "$pattern" ] && return 1
  case "$pid" in
    ''|*[!0-9]*) echo "is_expected_pid: invalid PID '$pid'" >&2; return 1 ;;
  esac
  [ "$pid" -le 0 ] 2>/dev/null && { echo "is_expected_pid: PID must be > 0" >&2; return 1; }
  ps -p "$pid" -o args= 2>/dev/null | grep -Eq "$pattern"
}
