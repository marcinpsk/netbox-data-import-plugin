# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Fail a plugin save of an existing change-logged object that has no current prechange snapshot."""

import sys
from types import FrameType

from django.db.models.signals import pre_save

_PLUGIN_PREFIX = "netbox_data_import."
_TESTS_PACKAGE = "netbox_data_import.tests"
_DISPATCH_UID = "netbox_data_import.tests.snapshot_guard"
_violations: list[str] = []
# The outermost save frame of each plugin create, kept alive so a later call cannot reuse its identity.
_insert_calls: dict[int, FrameType] = {}


class MissingChangelogSnapshot(AssertionError):
    """A plugin frame saved an existing change-logged object without a current snapshot."""


def _is_django(module: str) -> bool:
    return module == "django" or module.startswith("django.")


def _is_plugin(module: str) -> bool:
    in_tests = module == _TESTS_PACKAGE or module.startswith(f"{_TESTS_PACKAGE}.")
    return module.startswith(_PLUGIN_PREFIX) and not in_tests


def _deciding_frame(instance):
    """Return the nearest frame outside Django and the instance's own save chain, and that chain's outermost frame."""
    chain = None
    frame = sys._getframe(2)
    while frame is not None:
        if not _is_django(frame.f_globals.get("__name__", "")) and not (
            frame.f_code.co_name in ("save", "save_base") and frame.f_locals.get("self") is instance
        ):
            return frame, chain
        chain = frame
        frame = frame.f_back
    return None, None


def _stored_snapshot(sender, instance, using):
    """Return the snapshot NetBox's ``snapshot()`` takes of the stored row, or None when it is gone."""
    stored = sender._base_manager.db_manager(using).filter(pk=instance.pk).first()
    if stored is None:
        return None
    stored.snapshot()
    return stored._prechange_snapshot


def require_current_snapshot(sender, instance, raw=False, using=None, **kwargs):
    """Raise and record when plugin code saves an existing change-logged row with a missing or stale snapshot."""
    if raw or not hasattr(instance, "snapshot"):
        return
    frame, chain = _deciding_frame(instance)
    if frame is None or not _is_plugin(frame.f_globals.get("__name__", "")):
        return
    if instance._state.adding:
        _insert_calls[id(instance)] = chain
        return
    # A model save that inserts and then saves again (NetBox's Cable) is still the plugin's create.
    if _insert_calls.get(id(instance)) is chain:
        return
    snapshot = getattr(instance, "_prechange_snapshot", None)
    if snapshot is None:
        problem = "no prechange snapshot"
    else:
        stored = _stored_snapshot(sender, instance, using) or {}
        stale = sorted(key for key in {*snapshot, *stored} if snapshot.get(key) != stored.get(key))
        if not stale:
            return
        problem = f"a stale prechange snapshot (differs in {', '.join(stale)})"
    message = (
        f"{sender._meta.label} pk={instance.pk} was saved with {problem} "
        f"from {frame.f_code.co_filename}:{frame.f_lineno}. Call snapshot() before the change."
    )
    _violations.append(message)
    raise MissingChangelogSnapshot(message)


def connect() -> None:
    """Check every save for the rest of the session."""
    pre_save.connect(require_current_snapshot, dispatch_uid=_DISPATCH_UID, weak=False)


def disconnect() -> None:
    """Stop checking saves."""
    pre_save.disconnect(dispatch_uid=_DISPATCH_UID)


def take_violations() -> list[str]:
    """Return and forget every violation recorded since the last call, and forget the recorded creates."""
    taken = list(_violations)
    _violations.clear()
    _insert_calls.clear()
    return taken
