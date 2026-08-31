# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Where an address off a source row lands on a device.

The preview names the interface and the sync writes to it, so both read this module. Two copies
would let a row promise one interface and the write pick another.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Any

# `oob_ip` carries no family in NetBox, so it takes either.
IP_FIELD_FAMILY: dict[str, int | None] = {"primary_ip4": 4, "primary_ip6": 6, "oob_ip": None}

# Bounded at both ends: a word cannot leave a shorter valid address, and 45 covers the longest one.
_IP_TOKEN = re.compile(r"(?<![0-9A-Za-z])[0-9A-Fa-f:.]{1,45}(?:/\d{1,3})?(?![0-9A-Za-z])")


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
    def held(self):
        """Return the stored row the device already carries, which only a held target has."""
        if not self.already_held or self.existing is None:
            raise IPAssignmentError(f"{self.address} is not already on this device, so it has no stored row.")
        return self.existing

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


def _normalized_ip(token: str) -> str | None:
    """Return *token* as 'address/prefix', or None when it is not one address."""
    try:
        if "/" in token:
            return str(ipaddress.ip_interface(token))
        addr = ipaddress.ip_address(token)
    except ValueError:
        return None
    return f"{addr}/32" if addr.version == 4 else f"{addr}/128"


def parse_address(raw_value) -> str | None:
    """Return the one address a source value names, as 'address/prefix', or None.

    Sources export an address inside a label or with a separator appended, so the whole value is
    tried first and the addresses spelled inside it only after that.
    """
    raw = str(raw_value).strip()
    if not raw:
        return None
    whole = _normalized_ip(raw)
    if whole is not None:
        return whole
    for token in _IP_TOKEN.findall(raw):
        while token:
            found = _normalized_ip(token)
            if found is not None:
                return found
            if token[-1] not in ".:":
                break
            token = token[:-1]
    return None


def normalized_address(field: str, value) -> str:
    """Return the address the row names as 'host/prefix', in the family the field takes."""
    address = parse_address(value)
    if address is None:
        raise IPAssignmentError(f"Cannot read an IP address from '{value}'.")
    family = IP_FIELD_FAMILY.get(field)
    version = ipaddress.ip_interface(address).version
    if family is not None and version != family:
        raise IPAssignmentError(f"'{value}' is an IPv{version} address; this field takes IPv{family}.")
    return address


def already_assigned(device, field, address) -> bool:
    """Return whether the device already carries exactly this address on *field*.

    The writer selects the address that this device holds. The primary field must point to that
    same row, while duplicate rows held by other objects do not affect the settled state.
    """
    current = getattr(device, field, None)
    if current is None:
        return False
    try:
        if _host(current.address) != _host(address):
            return False
        held = held_by_device(device, address)
    except ValueError:
        return False
    return held is not None and held.pk == current.pk


def _host(address) -> str:
    """Return the host part, which is what identifies an address inside one VRF."""
    return str(ipaddress.ip_interface(str(address)).ip)


def held_by_device(device, address):
    """Return the address this device already carries, or None.

    Every interface is searched, not only a management one, and the mask is ignored: a workbook
    states a bare address where the device holds the same host inside its real subnet.

    An address the device points at but no interface of its own holds is not a match. NetBox
    requires an IP field to name an address on that device, so that state is repaired through the
    normal path rather than copied onto a second field.
    """
    from ipam.models import IPAddress

    wanted = _host(address)
    candidates = IPAddress.objects.filter(interface__device=device).select_related("vrf")
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


def apply(target: IPTarget, user=None):
    """Write the address *target* names onto its interface, and return the IPAddress.

    The row sync and the import writer both land here, so an address is created, scoped and
    checked the same way whichever one runs.
    """
    from ipam.models import IPAddress

    from .object_permissions import enforce_saved_object_permission

    address = target.existing
    action = "change" if address is not None else "add"
    if address is None:
        address = IPAddress(address=target.address, vrf=target.interface.vrf)
    address.assigned_object = target.interface
    address.full_clean()
    address.save()
    # An ObjectPermission's constraints are only evaluated against the saved row.
    enforce_saved_object_permission(address, user, action)
    return address
