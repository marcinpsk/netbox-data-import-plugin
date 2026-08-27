# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Where an address off a source row lands on a device.

The preview names the interface and the sync writes to it, so both read this module. Two copies
would let a row promise one interface and the write pick another.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Any

# `oob_ip` carries no family in NetBox, so it takes either.
IP_FIELD_FAMILY: dict[str, int | None] = {"primary_ip4": 4, "primary_ip6": 6, "oob_ip": None}


class IPAssignmentError(Exception):
    """The address cannot be placed, with the repair the operator has to make."""


@dataclass(frozen=True)
class IPTarget:
    """The interface an address would land on, and the row it would use."""

    address: str
    interface: Any
    existing: Any | None
    already_held: bool

    @property
    def interface_name(self) -> str:
        """Return the interface name, or an empty string when the row is unassigned."""
        return getattr(self.interface, "name", "") or ""

    @property
    def summary(self) -> str:
        """Return what the sync reports once it has written."""
        if not self.interface_name:
            return self.address
        return f"{self.address} on {self.interface_name}"

    @property
    def placement(self) -> str:
        """Return what the preview shows beside the address it already prints."""
        if not self.interface_name:
            return ""
        if self.already_held:
            # The stored mask can differ from the one the row states, so it is worth printing.
            return f"already on {self.interface_name} as {self.address}"
        return f"would go to {self.interface_name}"


def normalized_address(field: str, value) -> str:
    """Return the address the row names as 'host/prefix', in the family the field takes."""
    from .engine import _parse_ip_with_prefix

    address = _parse_ip_with_prefix(value)
    if address is None:
        raise IPAssignmentError(f"Cannot read an IP address from '{value}'.")
    family = IP_FIELD_FAMILY.get(field)
    version = ipaddress.ip_interface(address).version
    if family is not None and version != family:
        raise IPAssignmentError(f"'{value}' is an IPv{version} address; this field takes IPv{family}.")
    return address


def _host(address) -> str:
    """Return the host part, which is what identifies an address inside one VRF."""
    return str(ipaddress.ip_interface(str(address)).ip)


def held_by_device(device, address):
    """Return the address this device already carries, or None.

    Every interface is searched, not only a management one, and the mask is ignored: a workbook
    states a bare address where the device holds the same host inside its real subnet. The
    device's own IP fields are searched too, so an address it keeps as its out-of-band IP is
    found even when nothing else points at it.
    """
    from ipam.models import IPAddress

    wanted = _host(address)
    candidates = list(IPAddress.objects.filter(interface__device=device).select_related("vrf"))
    candidates += [held for held in (getattr(device, name, None) for name in IP_FIELD_FAMILY) if held is not None]
    matches = [candidate for candidate in candidates if _host(candidate.address) == wanted]
    if not matches:
        return None
    # A management interface answers first when the device holds the address more than once.
    matches.sort(key=lambda ip: not getattr(ip.assigned_object, "mgmt_only", False))
    return matches[0]


def interface_for(device):
    """Return the interface an address off a source file belongs on.

    A management interface answers first: that is what the device type marks it for, and an
    address in a source workbook is a management address far more often than not.
    """
    from dcim.models import InterfaceTemplate

    interfaces = sorted(device.interfaces.all(), key=lambda i: (not i.mgmt_only, i.name))
    if interfaces:
        return interfaces[0]
    model = device.device_type.model
    declared = list(InterfaceTemplate.objects.filter(device_type=device.device_type).order_by("name")[:5])
    if not declared:
        raise IPAssignmentError(
            f"The device type '{model}' declares no interfaces, so there is nowhere to put this "
            f"address. Add an interface to the device type, then sync again."
        )
    names = ", ".join(template.name for template in declared)
    raise IPAssignmentError(
        f"This device has none of the interfaces its type '{model}' declares ({names}). "
        f"Add them to the device, then sync again."
    )


def resolve(device, field: str, value) -> IPTarget:
    """Return where *value* would land on *device*, or raise with the repair to make."""
    from ipam.models import IPAddress

    address = normalized_address(field, value)
    held = held_by_device(device, address)
    if held is not None:
        return IPTarget(address=str(held.address), interface=held.assigned_object, existing=held, already_held=True)

    interface = interface_for(device)
    # The interface's VRF scopes the address: the same host in another VRF is a different address.
    existing = IPAddress.objects.filter(address__net_host=_host(address), vrf=interface.vrf).first()
    if existing is not None and existing.assigned_object is not None:
        owner = getattr(existing.assigned_object, "device", None) or existing.assigned_object
        raise IPAssignmentError(f"Address {existing.address} is already assigned to '{owner}'.")
    return IPTarget(address=address, interface=interface, existing=existing, already_held=False)
