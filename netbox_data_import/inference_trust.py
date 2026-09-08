# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The `api_root` trust boundary (specification 8.3).

`api_root` names a destination the NetBox server itself calls, so the same rules apply at the form
boundary and again at request time. Resolution is a separate step: the allowlist approves a name,
and `assert_resolved_address_allowed` decides whether the address that name answered with is one
NetBox may reach.

Specification 8.3 asks for a recheck after resolution, which is what this module performs. It does
not pin the socket to the address it checked, so a name that answers differently between the check
and the connect is a residual window. Closing it needs an address-pinned transport with its own TLS
hostname handling, which is tracked in issue #147.
"""

import ipaddress
import socket

from collections.abc import Iterable, Sequence
from urllib.parse import urlsplit

SUPPORTED_SCHEMES = ("https", "http")

# Link-local already covers 169.254.0.0/16, so these name the destinations worth their own message.
CLOUD_METADATA_ADDRESSES = frozenset(
    {
        "169.254.169.254",  # AWS, Azure, GCP, OpenStack
        "169.254.170.2",  # AWS ECS task metadata
        "100.100.100.200",  # Alibaba Cloud
        "fd00:ec2::254",  # AWS IMDSv2 over IPv6
    }
)

LOCAL_HOST_NAMES = frozenset({"localhost", "localhost.localdomain"})


class InvalidInferenceConfiguration(ValueError):
    """An Inference Backend configuration value the deployment may not use."""


def split_url(value: str, setting: str, *, quote_value: bool = True):
    """Return the parsed URL, rejecting a value that is not a usable absolute URL.

    `quote_value=False` keeps the rejected value out of the message, for a setting that can carry
    a secret in its userinfo and whose failures are persisted.
    """
    if not isinstance(value, str):
        raise InvalidInferenceConfiguration(f"'{setting}' must be a string, got {type(value).__name__}.")
    got = f" Got '{value}'." if quote_value else ""
    try:
        # urlsplit raises a bare ValueError on malformed bracket syntax, which callers do not catch.
        parts = urlsplit(value.strip())
    except ValueError as exc:
        raise InvalidInferenceConfiguration(f"'{setting}' is not a usable URL.{got}") from exc
    if not parts.scheme:
        raise InvalidInferenceConfiguration(f"'{setting}' must name a scheme, for example https://host:443.{got}")
    if parts.scheme.lower() not in SUPPORTED_SCHEMES:
        scheme = f" Got '{parts.scheme}'." if quote_value else ""
        raise InvalidInferenceConfiguration(
            f"'{setting}' must use the scheme {' or '.join(SUPPORTED_SCHEMES)}.{scheme}"
        )
    if parts.username or parts.password:
        raise InvalidInferenceConfiguration(f"'{setting}' must not carry a credential in its userinfo component.")
    if not parts.hostname:
        raise InvalidInferenceConfiguration(f"'{setting}' must name a host.{got}")
    if "*" in parts.netloc:
        raise InvalidInferenceConfiguration(f"'{setting}' must not use a wildcard.{got}")
    return parts


def origin_of(value: str, setting: str) -> str:
    """Return the scheme, host and port of one absolute URL, lower-cased."""
    parts = split_url(value, setting)
    unusable = f"'{setting}' must name a port between 1 and 65535. Got '{value}'."
    try:
        # urlsplit defers the cast, so a non-numeric or out-of-range port raises only here.
        port = parts.port
    except ValueError as exc:
        raise InvalidInferenceConfiguration(unusable) from exc
    if port is None:
        raise InvalidInferenceConfiguration(
            f"'{setting}' must name an explicit port, for example https://host:443. Got '{value}'."
        )
    if port < 1:
        # urlsplit returns zero rather than raising, and no connection can use it.
        raise InvalidInferenceConfiguration(unusable)
    return f"{parts.scheme.lower()}://{parts.hostname.lower()}:{port}"


def validate_origin(value: str, setting: str) -> str:
    """Return one exact allowlist origin, rejecting a path, query, fragment or wildcard."""
    parts = split_url(value, setting)
    if parts.path or parts.query or parts.fragment:
        raise InvalidInferenceConfiguration(
            f"'{setting}' entries carry no path, query or fragment component. Got '{value}'."
        )
    return origin_of(value, setting)


def _allowlist_entry_for(origin: str, allowlist: Iterable[str]) -> str | None:
    """Return the allowlist entry that approves *origin*, or None."""
    for entry in allowlist:
        if origin_of(entry, setting="allowlist") == origin:
            return entry
    return None


def is_local_endpoint(origin: str) -> bool:
    """Return whether an origin literally names a local address, which is the approval to reach one."""
    host = urlsplit(origin).hostname or ""
    if host.lower() in LOCAL_HOST_NAMES:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_link_local


def _assert_origin_approved(url: str, allowlist: Sequence[str], authentication: str, setting: str) -> str:
    """Reject a URL whose origin the deployment has not approved, or whose scheme it may not use."""
    parts = split_url(url, setting)
    origin = origin_of(url, setting)
    if _allowlist_entry_for(origin, allowlist) is None:
        raise InvalidInferenceConfiguration(
            f"'{setting}' origin '{origin}' is not on the inference_backend_origin_allowlist."
        )
    if parts.scheme.lower() != "https" and authentication == "bearer" and not is_local_endpoint(origin):
        raise InvalidInferenceConfiguration(
            f"'{setting}' must use https when authentication is bearer, unless the allowlist approves it as a "
            f"local endpoint. Got '{url}'."
        )
    return origin


def validate_api_root(
    api_root: str,
    allowlist: Sequence[str],
    authentication: str = "bearer",
    setting: str = "api_root",
) -> str:
    """Return the validated API root, rejecting an origin the deployment has not approved."""
    parts = split_url(api_root, setting)
    if parts.path.endswith("/"):
        raise InvalidInferenceConfiguration(
            f"'{setting}' must have no trailing slash. The client appends /chat/completions. Got '{api_root}'."
        )
    # A bare `?` or `#` splits into an empty component, so the raw value is what shows it.
    if parts.query or parts.fragment or "?" in api_root or "#" in api_root:
        raise InvalidInferenceConfiguration(
            f"'{setting}' carries no query or fragment component. The client appends /chat/completions to the "
            f"path, which either one would swallow. Got '{api_root}'."
        )
    _assert_origin_approved(api_root, allowlist, authentication, setting)
    return api_root


def assert_resolved_address_allowed(
    api_root: str,
    allowlist: Sequence[str],
    addresses: Iterable[str],
    setting: str = "api_root",
) -> None:
    """Reject a destination whose resolved address NetBox must not reach."""
    origin = origin_of(api_root, setting)
    approved_local = is_local_endpoint(origin) and _allowlist_entry_for(origin, allowlist) is not None
    resolved = tuple(addresses)
    if not resolved:
        raise InvalidInferenceConfiguration(f"'{setting}' host '{origin}' resolved to no address.")
    for candidate in resolved:
        address = ipaddress.ip_address(candidate)
        if candidate in CLOUD_METADATA_ADDRESSES:
            raise InvalidInferenceConfiguration(
                f"'{setting}' resolves to the cloud metadata address {candidate}, which NetBox must not call."
            )
        if approved_local:
            continue
        category = next(
            (
                label
                for matches, label in (
                    (address.is_loopback, "loopback"),
                    (address.is_link_local, "link-local"),
                    (address.is_reserved, "reserved"),
                    (address.is_private, "private"),
                )
                if matches
            ),
            None,
        )
        if category:
            raise InvalidInferenceConfiguration(
                f"'{setting}' resolves to the {category} address {candidate}, which the allowlist does not approve "
                f"as a local endpoint."
            )


def resolve_addresses(api_root: str, setting: str = "api_root") -> tuple[str, ...]:
    """Return every address the API root's host answers with."""
    parts = split_url(api_root, setting)
    port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    try:
        answers = socket.getaddrinfo(parts.hostname, port, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise InvalidInferenceConfiguration(f"'{setting}' host '{parts.hostname}' could not be resolved.") from exc
    return tuple(dict.fromkeys(str(answer[4][0]) for answer in answers))


__all__ = (
    "CLOUD_METADATA_ADDRESSES",
    "InvalidInferenceConfiguration",
    "assert_resolved_address_allowed",
    "is_local_endpoint",
    "origin_of",
    "resolve_addresses",
    "split_url",
    "validate_api_root",
    "validate_origin",
)
