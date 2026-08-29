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


class PlanningTargetUnavailable(Exception):
    """The planning context names a target this reader cannot resolve."""


class NetBoxReader:
    """Permission-scoped reads of the NetBox objects planning compares against."""

    def __init__(self, actor, site=None, location=None, tenant=None):
        self._actor = actor
        self._site = site
        self._location = location
        self._tenant = tenant

    def for_target(self, *, site, location=None, tenant=None) -> NetBoxReader:
        """Return this reader bound to the import target as well as the actor.

        Section 2.1 fixes `TargetModule.plan` at four parameters and none of them is the target, so
        the accessor of target state carries which target state is relevant.
        """
        return type(self)(self._actor, site=site, location=location, tenant=tenant)

    def for_planning_context(self, planning_context) -> NetBoxReader:
        """Return this reader bound to the target a planning context names.

        The target resolves through this reader's own scope, so an operator cannot plan against a
        site, location or tenant they may not view.
        """
        from dcim.models import Location, Site
        from tenancy.models import Tenant

        if planning_context.get("site_id") is None:
            raise PlanningTargetUnavailable("A planning context names the site the import writes into.")
        return self.for_target(
            site=self._required(Site, planning_context["site_id"]),
            location=self._optional(Location, planning_context.get("location_id")),
            tenant=self._optional(Tenant, planning_context.get("tenant_id")),
        )

    def _required(self, model, pk):
        """Return the object *pk* names, or refuse when it is gone or out of scope."""
        found = self._scoped(model, "view").filter(pk=pk).first()
        if found is None:
            raise PlanningTargetUnavailable(f"{model._meta.verbose_name} {pk} is gone, or this actor cannot view it.")
        return found

    def _optional(self, model, pk):
        """Return the object *pk* names, or None when the context names none."""
        return None if pk is None else self._required(model, pk)

    @property
    def site(self):
        """Return the site this import writes into, or None before a target is bound."""
        return self._site

    @property
    def location(self):
        """Return the location this import writes into, if the operator chose one."""
        return self._location

    @property
    def tenant(self):
        """Return the tenant this import writes into, if the operator chose one."""
        return self._tenant

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


__all__ = ("NetBoxReader", "PlanningTargetUnavailable")
