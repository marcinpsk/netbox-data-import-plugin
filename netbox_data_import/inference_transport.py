# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Address-pinned HTTP transport for inference and credential requests."""

import ipaddress

from collections import OrderedDict
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests


class _AddressPinnedAdapter(requests.adapters.HTTPAdapter):
    """Connect to one numeric address while preserving the origin hostname."""

    def __init__(self, origin_url: str, resolved_address: str):
        super().__init__()
        parts = urlsplit(origin_url)
        self._origin = (parts.scheme.lower(), parts.hostname, parts.port)
        address = ipaddress.ip_address(resolved_address)
        self._address = f"[{address}]" if address.version == 6 else str(address)

    def build_connection_pool_key_attributes(self, request, verify, cert=None):
        """Keep TLS SNI and certificate verification bound to the origin hostname."""
        host_params, pool_kwargs = super().build_connection_pool_key_attributes(request, verify, cert)
        if self._origin[0] == "https":
            pool_kwargs["assert_hostname"] = self._origin[1]
            pool_kwargs["server_hostname"] = self._origin[1]
        return host_params, pool_kwargs

    def send(self, request, *args, **kwargs):
        """Rewrite only the connection URL, then restore the prepared request."""
        parts = urlsplit(request.url)
        request_origin = (parts.scheme.lower(), parts.hostname, parts.port)
        if request_origin != self._origin:
            raise requests.exceptions.InvalidURL("The pinned transport received a request for another origin.")
        original_url = request.url
        original_host = request.headers.get("Host")
        port = f":{parts.port}" if parts.port is not None else ""
        request.url = urlunsplit((parts.scheme, f"{self._address}{port}", parts.path, parts.query, parts.fragment))
        request.headers["Host"] = parts.netloc
        try:
            # A proxy would resolve the hostname again and bypass the pinned address.
            response = super().send(request, *args, **{**kwargs, "proxies": {}})
            response.url = original_url
            return response
        finally:
            request.url = original_url
            if original_host is None:
                request.headers.pop("Host", None)
            else:
                request.headers["Host"] = original_host


def request_to_resolved_address(
    session: requests.Session,
    method: str,
    url: str,
    resolved_address: str,
    **kwargs: Any,
) -> requests.Response:
    """Send one request to a resolved address without changing its HTTP or TLS hostname."""
    previous_adapters = OrderedDict(session.adapters)
    adapter = _AddressPinnedAdapter(url, resolved_address)
    session.mount(url, adapter)
    try:
        return session.request(method, url, **kwargs)
    finally:
        adapter.close()
        session.adapters.clear()
        session.adapters.update(previous_adapters)


__all__ = ("request_to_resolved_address",)
