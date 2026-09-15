# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Address-pinned HTTP transport for inference and credential requests."""

import ipaddress
import socket
import time

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from contextlib import suppress
from dataclasses import dataclass
from threading import Event, Lock, RLock, Timer
from typing import Any, Callable, TypeVar
from urllib.parse import urlsplit, urlunsplit
from weakref import WeakKeyDictionary

import requests
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import MaxRetryError, NewConnectionError
from urllib3.response import HTTPResponse
from urllib3.util import Timeout as Urllib3Timeout


_SESSION_LOCKS: WeakKeyDictionary[requests.Session, RLock] = WeakKeyDictionary()
_SESSION_LOCKS_GUARD = Lock()
_DEADLINE_WORKERS = ThreadPoolExecutor(max_workers=4, thread_name_prefix="inference-deadline")
_Result = TypeVar("_Result")


class WallClockDeadlineExceeded(requests.Timeout):
    """One foreground operation exhausted its shared wall-clock budget."""


@dataclass(frozen=True)
class WallClockDeadline:
    """Share one time budget across DNS and every HTTP request in an operation."""

    expires_at: float

    @classmethod
    def after(cls, seconds: float) -> "WallClockDeadline":
        """Return a deadline that expires after the supplied positive interval."""
        if seconds <= 0:
            raise ValueError("A wall-clock deadline must be positive.")
        return cls(time.monotonic() + seconds)

    def remaining(self) -> float:
        """Return the remaining seconds, or fail when the shared budget is exhausted."""
        remaining = self.expires_at - time.monotonic()
        if remaining <= 0:
            raise WallClockDeadlineExceeded("The operation exceeded its overall time limit.")
        return remaining

    def request_timeout(self, timeout: tuple[float, float]) -> Urllib3Timeout:
        """Cap one Requests connect and initial read to the remaining shared budget."""
        remaining = self.remaining()
        return Urllib3Timeout(
            total=remaining,
            connect=min(timeout[0], remaining),
            read=min(timeout[1], remaining),
        )

    def run(self, operation: Callable[..., _Result], *args: Any, **kwargs: Any) -> _Result:
        """Run a blocking operation without letting it hold the caller past the deadline."""
        future = _DEADLINE_WORKERS.submit(operation, *args, **kwargs)
        try:
            return future.result(timeout=self.remaining())
        except FutureTimeoutError:
            future.cancel()
            raise WallClockDeadlineExceeded("The operation exceeded its overall time limit.") from None


def _run_connection_until_deadline(
    connection: HTTPConnection,
    deadline: WallClockDeadline,
    operation: Callable[[], _Result],
) -> _Result:
    """Interrupt connection or response-header I/O when its wall-clock budget expires."""
    expired = Event()

    def abort_socket() -> None:
        expired.set()
        connected_socket = connection.sock
        if connected_socket is None:
            return
        with suppress(OSError):
            connected_socket.shutdown(socket.SHUT_RDWR)
        with suppress(OSError):
            connected_socket.close()

    timer = Timer(deadline.remaining(), abort_socket)
    timer.daemon = True
    timer.start()
    try:
        result = operation()
    except Exception:
        if expired.is_set():
            raise WallClockDeadlineExceeded("The operation exceeded its overall time limit.") from None
        raise
    else:
        deadline.remaining()
        return result
    finally:
        timer.cancel()

    def connect(self) -> None:
        """Bound TCP and TLS connection work to the shared deadline."""
        self._run_until_deadline(self._connect_without_deadline)

    def getresponse(self) -> HTTPResponse:
        """Bound the status line and response headers to the shared deadline."""
        return self._run_until_deadline(self._getresponse_without_deadline)


def _deadline_pool_classes(deadline: WallClockDeadline):
    """Return urllib3 pools whose connections enforce one operation deadline."""

    class DeadlineHTTPConnection(HTTPConnection):
        def connect(self) -> None:
            _run_connection_until_deadline(self, deadline, super().connect)

        def getresponse(self) -> HTTPResponse:  # type: ignore[override]
            return _run_connection_until_deadline(self, deadline, super().getresponse)

    class DeadlineHTTPSConnection(HTTPSConnection):
        def connect(self) -> None:
            _run_connection_until_deadline(self, deadline, super().connect)

        def getresponse(self) -> HTTPResponse:  # type: ignore[override]
            return _run_connection_until_deadline(self, deadline, super().getresponse)

    class DeadlineHTTPConnectionPool(HTTPConnectionPool):
        ConnectionCls = DeadlineHTTPConnection

    class DeadlineHTTPSConnectionPool(HTTPSConnectionPool):
        ConnectionCls = DeadlineHTTPSConnection

    return {"http": DeadlineHTTPConnectionPool, "https": DeadlineHTTPSConnectionPool}


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


def _consume_response(response, response_body_limit, deadline: WallClockDeadline | None) -> None:
    """Read one response under its size and wall-clock limits."""
    chunks = bytearray()
    chunk_size = 1 if deadline is not None else min(response_body_limit + 1, 65_536)
    iterator = response.iter_content(chunk_size=chunk_size)
    while True:
        if deadline is not None:
            remaining = deadline.remaining()
            connection = getattr(response.raw, "connection", None)
            sock = getattr(connection, "sock", None)
            if sock is not None:
                sock.settimeout(remaining)
        try:
            chunk = next(iterator)
        except StopIteration:
            break
        if not chunk:
            continue
        chunks.extend(chunk)
        if response_body_limit is not None and len(chunks) > response_body_limit:
            response.close()
            raise ResponseBodyTooLarge(f"The response body exceeded {response_body_limit} bytes.", response=response)
    if deadline is not None:
        deadline.remaining()
    response._content = bytes(chunks)
    response._content_consumed = True


class _AddressPinnedAdapter(requests.adapters.HTTPAdapter):
    """Connect to one numeric address while preserving the origin hostname."""

    def __init__(
        self,
        origin_url: str,
        resolved_address: str,
        deadline: WallClockDeadline | None = None,
    ):
        super().__init__()
        parts = urlsplit(origin_url)
        self._origin = (parts.scheme.lower(), parts.hostname, parts.port)
        address = ipaddress.ip_address(resolved_address)
        self._address = f"[{address}]" if address.version == 6 else str(address)
        self._deadline = deadline
        if deadline is not None:
            self.poolmanager.pool_classes_by_scheme = _deadline_pool_classes(deadline)

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
            try:
                # A proxy would resolve the hostname again and bypass the pinned address.
                response = super().send(request, *args, **{**kwargs, "proxies": {}})
            except requests.RequestException:
                if self._deadline is not None:
                    self._deadline.remaining()
                raise
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
    deadline: WallClockDeadline | None = None,
    **kwargs: Any,
) -> requests.Response:
    """Send one request to a resolved address without changing its HTTP or TLS hostname."""
    if response_body_limit is not None and response_body_limit < 1:
        raise ValueError("A response body limit must be positive.")
    if response_body_limit is not None or deadline is not None:
        kwargs["stream"] = True
    with _session_lock(session):
        previous_adapters = OrderedDict(session.adapters)
        adapter = _AddressPinnedAdapter(url, resolved_address, deadline)
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
                if deadline is not None:
                    kwargs["timeout"] = deadline.request_timeout(kwargs["timeout"])
                response = session.request(method, url, hooks=hooks, **kwargs)
                if response_body_limit is not None or deadline is not None:
                    _consume_response(response, response_body_limit, deadline)
            except (ValueError, requests.RequestException) as exc:
                if captured_response is not None:
                    captured_response.close()
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
    "WallClockDeadline",
    "WallClockDeadlineExceeded",
    "is_preconnect_failure",
    "request_to_resolved_address",
)
