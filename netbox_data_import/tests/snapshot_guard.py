# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Fail a plugin write to an existing change-logged object that has no current prechange snapshot."""

import sys
from types import FrameType

from django.db.models.signals import m2m_changed, pre_save

_PLUGIN_PREFIX = "netbox_data_import."
_TESTS_PACKAGE = "netbox_data_import.tests"
_DISPATCH_UID = "netbox_data_import.tests.snapshot_guard"
_M2M_ACTIONS = ("pre_add", "pre_remove", "pre_clear")
_violations: list[str] = []
# The outermost save frame of each plugin create, kept alive so a later call cannot reuse its identity.
_insert_calls: dict[int, FrameType] = {}


class MissingChangelogSnapshot(AssertionError):
    """A plugin frame wrote an existing change-logged object without a current snapshot."""


def _is_django(module: str) -> bool:
    return module == "django" or module.startswith("django.")


def _is_plugin(module: str) -> bool:
    in_tests = module == _TESTS_PACKAGE or module.startswith(f"{_TESTS_PACKAGE}.")
    return module.startswith(_PLUGIN_PREFIX) and not in_tests


def _is_own_write(frame, instance, via_manager: bool) -> bool:
    """Return whether *frame* is part of the instance's own save, or of its own related manager."""
    owner = frame.f_locals.get("self")
    if via_manager:
        return getattr(owner, "instance", None) is instance
    return frame.f_code.co_name in ("save", "save_base") and owner is instance


def _calling_frame(instance, *, via_manager=False):
    """Return the nearest frame outside Django and the instance's own write.

    The second value is the outermost frame that was skipped, which identifies one save call.
    """
    outermost_save = None
    frame = sys._getframe(2)
    while frame is not None:
        if not _is_django(frame.f_globals.get("__name__", "")) and not _is_own_write(frame, instance, via_manager):
            return frame, outermost_save
        outermost_save = frame
        frame = frame.f_back
    return None, None


def _stored_snapshot(model, instance, using):
    """Return the snapshot NetBox's ``snapshot()`` takes of the stored row, or None when it is gone."""
    stored = model._base_manager.db_manager(using).filter(pk=instance.pk).first()
    if stored is None:
        return None
    stored.snapshot()
    return stored._prechange_snapshot


def _require_current(model, instance, using, frame) -> None:
    """Raise and record when *instance* carries a missing or stale snapshot."""
    snapshot = getattr(instance, "_prechange_snapshot", None)
    if snapshot is None:
        problem = "no prechange snapshot"
    else:
        stored = _stored_snapshot(model, instance, using) or {}
        stale = sorted(key for key in {*snapshot, *stored} if snapshot.get(key) != stored.get(key))
        if not stale:
            return
        problem = f"a stale prechange snapshot (differs in {', '.join(stale)})"
    message = (
        f"{model._meta.label} pk={instance.pk} was written with {problem} "
        f"from {frame.f_code.co_filename}:{frame.f_lineno}. Call snapshot() before the change."
    )
    _violations.append(message)
    raise MissingChangelogSnapshot(message)


def require_current_snapshot(sender, instance, raw=False, using=None, **kwargs):
    """Check a save of an existing change-logged row that plugin code makes."""
    if raw or not hasattr(instance, "snapshot"):
        return
    frame, outermost_save = _calling_frame(instance)
    if frame is None or not _is_plugin(frame.f_globals.get("__name__", "")):
        return
    if instance._state.adding:
        _insert_calls[id(instance)] = outermost_save
        return
    # A model save that inserts and then saves again (NetBox's Cable) is still the plugin's create.
    if _insert_calls.get(id(instance)) is outermost_save:
        return
    _require_current(sender, instance, using, frame)


def require_current_snapshot_for_m2m(sender, instance, action, using=None, **kwargs):
    """Check a many-to-many change to an existing change-logged object that plugin code makes."""
    if action not in _M2M_ACTIONS or not hasattr(instance, "snapshot") or instance._state.adding:
        return
    frame, _outermost_save = _calling_frame(instance, via_manager=True)
    if frame is None or not _is_plugin(frame.f_globals.get("__name__", "")):
        return
    _require_current(type(instance), instance, using, frame)


def connect() -> None:
    """Check every save and many-to-many change for the rest of the session."""
    pre_save.connect(require_current_snapshot, dispatch_uid=_DISPATCH_UID, weak=False)
    m2m_changed.connect(require_current_snapshot_for_m2m, dispatch_uid=_DISPATCH_UID, weak=False)


def disconnect() -> None:
    """Stop checking writes."""
    pre_save.disconnect(dispatch_uid=_DISPATCH_UID)
    m2m_changed.disconnect(dispatch_uid=_DISPATCH_UID)


def take_violations() -> list[str]:
    """Return and forget every violation recorded since the last call, and forget the recorded creates."""
    taken = list(_violations)
    _violations.clear()
    _insert_calls.clear()
    return taken
