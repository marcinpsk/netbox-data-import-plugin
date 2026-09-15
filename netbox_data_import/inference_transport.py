# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Address-pinned HTTP transport for inference and credential requests."""

import ipaddress

from collections import OrderedDict
from threading import Lock, RLock
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from weakref import WeakKeyDictionary

import requests
from urllib3.exceptions import MaxRetryError, NewConnectionError


_SESSION_LOCKS: WeakKeyDictionary[requests.Session, RLock] = WeakKeyDictionary()
_SESSION_LOCKS_GUARD = Lock()


class ResponseProcessingFailure(requests.RequestException):
    """A response arrived, but Requests failed while processing it."""

    response: requests.Response

    def __init__(self, cause: Exception, response: requests.Response):
        super().__init__(str(cause), response=response)
        self.cause = cause


class ResponseBodyTooLarge(requests.RequestException):
    """The peer sent more response bytes than this request permits."""


def is_preconnect_failure(exc: requests.RequestException) -> bool:
    """Return whether another address can be tried without replaying a sent request."""
    if isinstance(exc, requests.ConnectTimeout):
        return True
    reason = exc.args[0] if exc.args else None
    return isinstance(reason, MaxRetryError) and isinstance(reason.reason, NewConnectionError)


def _session_lock(session: requests.Session) -> RLock:
    """Return the exclusive transport lock owned by one session."""
    with _SESSION_LOCKS_GUARD:
        lock = _SESSION_LOCKS.get(session)
        if lock is None:
            lock = RLock()
            _SESSION_LOCKS[session] = lock
        return lock


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
    response_body_limit: int | None = None,
    **kwargs: Any,
) -> requests.Response:
    """Send one request to a resolved address without changing its HTTP or TLS hostname."""
    if response_body_limit is not None:
        if response_body_limit < 1:
            raise ValueError("A response body limit must be positive.")
        kwargs["stream"] = True
    with _session_lock(session):
        previous_adapters = OrderedDict(session.adapters)
        adapter = _AddressPinnedAdapter(url, resolved_address)
        captured_response = None

        def capture_response(response, *_args, **_kwargs):
            nonlocal captured_response
            captured_response = response
            return response

        hooks = dict(kwargs.pop("hooks", {}) or {})
        response_hooks = hooks.get("response", ())
        if response_hooks is None:
            response_hooks = ()
        elif callable(response_hooks):
            response_hooks = (response_hooks,)
        hooks["response"] = (capture_response, *response_hooks)
        session.mount(url, adapter)
        try:
            try:
                response = session.request(method, url, hooks=hooks, **kwargs)
                if response_body_limit is not None:
                    chunks = []
                    received = 0
                    for chunk in response.iter_content(chunk_size=min(response_body_limit + 1, 65_536)):
                        if not chunk:
                            continue
                        received += len(chunk)
                        if received > response_body_limit:
                            response.close()
                            raise ResponseBodyTooLarge(
                                f"The response body exceeded {response_body_limit} bytes.", response=response
                            )
                        chunks.append(chunk)
                    response._content = b"".join(chunks)
                    response._content_consumed = True
            except (ValueError, requests.RequestException) as exc:
                if captured_response is not None:
                    raise ResponseProcessingFailure(exc, captured_response) from exc
                raise
            else:
                return response
        finally:
            adapter.close()
            session.adapters.clear()
            session.adapters.update(previous_adapters)


__all__ = (
    "ResponseBodyTooLarge",
    "ResponseProcessingFailure",
    "is_preconnect_failure",
    "request_to_resolved_address",
)
