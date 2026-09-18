# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The `api_root` trust boundary (specification 8.3).

`api_root` names a destination the NetBox server itself calls, so the same rules apply at the form
boundary and again at request time. Resolution is a separate step: the allowlist approves a name,
and `assert_resolved_address_allowed` decides whether the address that name answered with is one
NetBox may reach.

Specification 8.3 asks for a recheck after resolution. The inference and Vault request paths hand
that result to the address-pinned transport, which connects without resolving the hostname again.
The transport keeps the original HTTP Host header and TLS hostname verification.

A bearer credential may travel in cleartext over HTTP to an approved local endpoint.
`is_local_endpoint` covers loopback, private and link-local addresses, so this includes a private
network, not only loopback. This residual risk is an accepted deployment choice.
"""

import ipaddress
import json
import os
import socket
import subprocess
import sys

from collections.abc import Iterable, Sequence
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .inference_transport import WallClockDeadline, WallClockDeadlineExceeded

SUPPORTED_SCHEMES = ("https", "http")
DNS_WORKER_COMMAND = ("-I", "-S", str(Path(__file__).with_name("_dns_worker.py")))
# The deployment owns the token; the plugin never stores one.
VAULT_TOKEN_ENVIRONMENT_VARIABLE = "VAULT_TOKEN"  # noqa: S105 - This is an environment variable name.

# Link-local already covers 169.254.0.0/16, so these name the destinations worth their own message.
CLOUD_METADATA_ADDRESSES = frozenset(
    {
        "169.254.169.254",  # AWS, Azure, GCP, OpenStack
        "169.254.170.2",  # AWS ECS task metadata
        "100.100.100.200",  # Alibaba Cloud
        "fd00:ec2::254",  # AWS IMDSv2 over IPv6
    }
)

# Parsed from the exported strings, so an alternate spelling of the same address cannot slip past.
_CLOUD_METADATA_IPS = frozenset(ipaddress.ip_address(address) for address in CLOUD_METADATA_ADDRESSES)

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
    host = parts.hostname.lower()
    # urlsplit strips the brackets, and only an IPv6 literal can leave a colon in a hostname.
    if ":" in host:
        host = f"[{host}]"
    return urlunsplit((parts.scheme.lower(), f"{host}:{port}", "", "", ""))


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
    api_root = api_root.strip() if isinstance(api_root, str) else api_root
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
        if address in _CLOUD_METADATA_IPS:
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


def _resolution_failure(setting: str, host: str) -> InvalidInferenceConfiguration:
    """Return the public failure for a host that did not produce usable addresses."""
    return InvalidInferenceConfiguration(f"'{setting}' host '{host}' could not be resolved.")


def _worker_addresses(stdout: str) -> tuple[str, ...] | None:
    """Return validated worker output, or None when the child did not produce addresses."""
    try:
        payload = json.loads(stdout)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, list) or not all(isinstance(address, str) for address in payload):
        return None
    try:
        for address in payload:
            ipaddress.ip_address(address)
    except ValueError:
        return None
    return tuple(payload)


def _kill_and_reap(process: subprocess.Popen[str]) -> None:
    """Stop and reap a DNS worker without replacing the caller's result or failure."""
    with suppress(OSError):
        process.kill()
    with suppress(OSError, subprocess.SubprocessError):
        process.wait()


def _communicate_before_deadline(
    process: subprocess.Popen[str],
    request: str,
    deadline: WallClockDeadline,
    setting: str,
    host: str,
) -> str:
    """Exchange one worker request within the deadline and keep failures typed."""
    timeout = deadline.remaining()
    try:
        stdout, _stderr = process.communicate(input=request, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise WallClockDeadlineExceeded("The operation exceeded its overall time limit.") from None
    except Exception as exc:
        raise _resolution_failure(setting, host) from exc
    return stdout


def _resolve_addresses_before_deadline(
    host: str,
    port: int,
    setting: str,
    deadline: WallClockDeadline,
) -> tuple[str, ...]:
    """Resolve one host in a child that the parent can terminate and reap."""
    child_timeout = deadline.remaining()
    if not sys.executable:
        raise InvalidInferenceConfiguration(
            f"'{setting}' cannot be resolved before its deadline because the Python executable is unavailable."
        )
    environment = os.environ.copy()
    environment.pop(VAULT_TOKEN_ENVIRONMENT_VARIABLE, None)
    try:
        process = subprocess.Popen(  # noqa: S603 - the executable and arguments are module-owned
            (sys.executable, *DNS_WORKER_COMMAND),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env=environment,
        )
    except Exception as exc:
        raise _resolution_failure(setting, host) from exc
    try:
        request = json.dumps((host, port, child_timeout))
        stdout = _communicate_before_deadline(process, request, deadline, setting, host)
        addresses = _worker_addresses(stdout)
        deadline.remaining()
    finally:
        _kill_and_reap(process)
    if process.returncode != 0 or addresses is None:
        raise _resolution_failure(setting, host)
    return addresses


def resolve_addresses(
    api_root: str,
    setting: str = "api_root",
    deadline: WallClockDeadline | None = None,
) -> tuple[str, ...]:
    """Return every address the API root's host answers with."""
    parts = split_url(api_root, setting)
    port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    if deadline is not None:
        return _resolve_addresses_before_deadline(parts.hostname, port, setting, deadline)
    try:
        answers = socket.getaddrinfo(parts.hostname, port, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise _resolution_failure(setting, parts.hostname) from exc
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
