# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Permission-scoped target-state reads for import planning."""

from __future__ import annotations

from .catalog import import_location_required
from .field_keys import CABLE_END_KINDS
from .public_refusal import PublicRefusal


class PlanningTargetUnavailable(PublicRefusal):
    """The planning context names a target this reader cannot resolve."""


class NetBoxReader:
    """Permission-scoped reads of the NetBox objects planning compares against."""

    def __init__(self, actor, site=None, location=None, tenant=None, *, location_unavailable=False):
        self._actor = actor
        self._site = site
        self._location = location
        self._tenant = tenant
        self._location_unavailable = location_unavailable

    def for_target(self, *, site, location=None, tenant=None) -> NetBoxReader:
        """Bind the planning target without widening the actor's read scope."""
        return type(self)(self._actor, site=site, location=location, tenant=tenant)

    def for_planning_context(self, planning_context, *, output_kinds: frozenset[str]) -> NetBoxReader:
        """Resolve a planning context for *output_kinds*, inside this reader's permission scope."""
        from dcim.models import Location, Site
        from tenancy.models import Tenant

        if planning_context.get("site_id") is None:
            raise PlanningTargetUnavailable("A planning context names the site the import writes into.")
        site = self._required(Site, planning_context["site_id"])
        location_id = planning_context.get("location_id")
        location = None
        if location_id is not None:
            location = self._scoped(Location, "view").filter(pk=location_id, site=site).first()
        location_unavailable = location_id is not None and location is None
        # Section 6.1: a Location that only ranks candidates is evidence, not a target.
        if location_unavailable and import_location_required(output_kinds):
            self._required(Location, location_id)
            raise PlanningTargetUnavailable("The selected location does not belong to the selected site.")
        return type(self)(
            self._actor,
            site=site,
            location=location,
            tenant=self._optional(Tenant, planning_context.get("tenant_id")),
            location_unavailable=location_unavailable,
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
    def location_unavailable(self) -> bool:
        """Return whether the chosen import Location is gone, hidden, or outside the site."""
        return self._location_unavailable

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
        """Return a scoped reader, or an unrestricted one for an explicit system caller."""
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

    def locations(self, action: str = "view"):
        """Return the Locations the actor may take *action* on."""
        from dcim.models import Location

        return self._scoped(Location, action)

    def terminations(self, model_label: str, action: str = "view"):
        """Return the rows of one Cable End Kind, named ``app_label.model``, the actor may take *action* on."""
        from django.apps import apps

        if model_label not in CABLE_END_KINDS:
            raise ValueError(f"'{model_label}' is not a Cable End Kind.")
        return self._scoped(apps.get_model(model_label), action)

    def port_mappings(self):
        """Return the PortMapping rows of the Devices this actor may view.

        NetBox keeps the model private, with no manager of its own, so the parent Device carries
        the scope.
        """
        from dcim.models import PortMapping

        return PortMapping.objects.filter(device__in=self.devices())


__all__ = ("NetBoxReader", "PlanningTargetUnavailable")
