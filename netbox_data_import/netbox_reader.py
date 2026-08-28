# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The read-only target-state accessor planning uses (section 2.1).

Every read is bound to one actor and scoped to that actor's object permissions, so planning cannot
report target state the operator cannot view. The scope decision therefore lives here once, instead
of at each call site where forgetting it silently widens the read.

`unrestricted()` exists for a caller that has no actor. It is a named constructor rather than a None
default so that an unscoped read is always something the caller asked for.
"""

from __future__ import annotations


class NetBoxReader:
    """Permission-scoped reads of the NetBox objects planning compares against."""

    def __init__(self, actor):
        self._actor = actor

    @classmethod
    def for_actor(cls, actor) -> NetBoxReader:
        """Return a reader scoped to *actor*, which is required."""
        if actor is None:
            raise ValueError("A scoped NetBoxReader needs an actor. Use unrestricted() for no actor.")
        return cls(actor)

    @classmethod
    def unrestricted(cls) -> NetBoxReader:
        """Return a reader that applies no object permissions."""
        return cls(None)

    @classmethod
    def for_optional_actor(cls, actor) -> NetBoxReader:
        """Return a scoped reader, or an unrestricted one when the caller has no actor.

        `run_import` still accepts no actor, so this names that boundary in one place instead of
        repeating the decision at each read.
        """
        return cls.unrestricted() if actor is None else cls.for_actor(actor)

    @property
    def actor(self):
        """Return the actor every read is scoped to, or None for an unrestricted reader."""
        return self._actor

    def _scoped(self, model, action: str):
        """Return *model*'s objects limited to what the actor may take *action* on."""
        if self._actor is None:
            return model.objects.all()
        return model.objects.restrict(self._actor, action)

    def devices(self, action: str = "view"):
        """Return the Devices the actor may take *action* on."""
        from dcim.models import Device

        return self._scoped(Device, action)

    def racks(self, action: str = "view"):
        """Return the Racks the actor may take *action* on."""
        from dcim.models import Rack

        return self._scoped(Rack, action)


__all__ = ("NetBoxReader",)
