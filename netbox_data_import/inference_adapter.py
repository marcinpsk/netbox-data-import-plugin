# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The OpenAI-compatible Inference Backend adapter (specification 8.1, 8.3, 8.4).

The adapter makes one non-streaming Chat Completions call and can discover optional model-id
suggestions. It knows nothing about NetBox, candidates, proposals or jobs: it takes an
InferenceRequest and returns an InferenceCompletion, or raises a typed backend error.

A returned completion means the call reached the backend and the envelope parsed. Deciding what the
content means belongs to the application service, not here.
"""

import json

from dataclasses import dataclass
from collections.abc import Sequence
from contextlib import suppress

import requests
from urllib3.exceptions import ReadTimeoutError

from .inference_transport import (
    ResponseBodyTooLarge,
    ResponseProcessingFailure,
    WallClockDeadline,
    WallClockDeadlineExceeded,
    is_preconnect_failure,
    request_to_resolved_address,
)
from .inference_settings import MODEL_MAX_LENGTH
from .inference_trust import (
    InvalidInferenceConfiguration,
    assert_resolved_address_allowed,
    resolve_addresses,
    validate_api_root,
)

CHAT_COMPLETIONS_PATH = "/chat/completions"
MODELS_PATH = "/models"
CHAT_COMPLETION_RESPONSE_LIMIT = 65_536
MODEL_DISCOVERY_LIMIT = 100
MODEL_DISCOVERY_RESPONSE_LIMIT = 65_536
DIAGNOSTIC_TEXT_LIMIT = 4096

# Section 8.4 rejects every other terminal reason: only a completed answer is a completion.
ACCEPTED_FINISH_REASONS = ("stop",)

# A caller must tell an absent body from a cut-short one, an empty one and a present one.
BODY_ABSENT = "absent"
BODY_INTERRUPTED = "interrupted"
BODY_EMPTY = "empty"
BODY_PRESENT = "present"

# Specification 13.3 enumerates the transient statuses. Every other error status is the request.
TRANSIENT_STATUSES = (500, 502, 503, 504)

_REDACTED = "[redacted: the backend echoed the credential]"
_BODY_CLEAN = "clean"
_BODY_ECHOES = "echoes"


@dataclass(frozen=True)
class ResponseDiagnostic:
    """What one call received, for an operator to read when it failed.

    `text` is retained only for an empty or unauthenticated response, bounded by
    DIAGNOSTIC_TEXT_LIMIT, or as a fixed redaction marker. `redacted` records that the body was
    replaced for credential safety. `withheld` records that an authenticated response body was not
    retained. `truncated` records that retained unauthenticated text is incomplete.
    """

    receipt: str
    text: str | None = None
    status_code: int | None = None
    redacted: bool = False
    withheld: bool = False
    truncated: bool = False


ABSENT_DIAGNOSTIC = ResponseDiagnostic(receipt=BODY_ABSENT)


def _inspect(text: str, api_key: str) -> str:
    """Return whether the body contains a complete literal or decoded key."""
    pending: list[object] = [text]
    while pending:
        value = pending.pop()
        if isinstance(value, str):
            if api_key in value:
                return _BODY_ECHOES
            with suppress(ValueError, RecursionError):
                # Members are flattened into the list so a key echoed as a member name is seen too.
                pending.append(
                    json.loads(value, object_pairs_hook=lambda pairs: [item for pair in pairs for item in pair])
                )
        elif isinstance(value, list):
            pending.extend(value)
    return _BODY_CLEAN


def _diagnostic(response, api_key: str) -> ResponseDiagnostic:
    """Return the diagnostic one answered call carries, with the credential taken out of it."""
    try:
        text = response.text
    except Exception:  # noqa: BLE001 - a body that cannot be decoded is a lost body, not a new failure
        return ResponseDiagnostic(receipt=BODY_INTERRUPTED, status_code=response.status_code)
    verdict = _inspect(text, api_key) if api_key else _BODY_CLEAN
    if verdict == _BODY_ECHOES:
        return ResponseDiagnostic(receipt=BODY_PRESENT, text=_REDACTED, status_code=response.status_code, redacted=True)
    receipt = BODY_PRESENT if text else BODY_EMPTY
    if api_key and text:
        return ResponseDiagnostic(receipt=receipt, status_code=response.status_code, withheld=True)
    return ResponseDiagnostic(
        receipt=receipt,
        text=text[:DIAGNOSTIC_TEXT_LIMIT],
        status_code=response.status_code,
        truncated=len(text) > DIAGNOSTIC_TEXT_LIMIT,
    )


class InferenceBackendError(Exception):
    """A typed Inference Backend failure. No message carries the API key."""

    category = "backend_error"
    # A caller must never parse a message or an HTTP object to decide whether to try again.
    retryable = False

    def __init__(self, *args, diagnostic: ResponseDiagnostic | None = None):
        super().__init__(*args)
        self.diagnostic = diagnostic or ABSENT_DIAGNOSTIC


class TransportFailure(InferenceBackendError):
    """The backend could not be reached, or answered with a server error."""

    category = "transport_failure"
    retryable = True


class BackendTimeout(InferenceBackendError):
    """The backend did not answer inside the configured limits."""

    category = "timeout"
    retryable = True


class AuthenticationFailure(InferenceBackendError):
    """The backend refused the credential."""

    category = "authentication_failure"


class RateLimited(InferenceBackendError):
    """The backend refused the call for rate reasons."""

    category = "rate_limit"
    retryable = True

    def __init__(self, message, retry_after=None, diagnostic=None):
        super().__init__(message, diagnostic=diagnostic)
        self.retry_after = retry_after


class InvalidBackendConfiguration(InferenceBackendError):
    """The backend configuration cannot be used as given."""

    category = "invalid_configuration"


class MalformedEnvelope(InferenceBackendError):
    """The backend answered, but not with one usable completion."""

    category = "malformed_envelope"


@dataclass(frozen=True)
class InferenceRequest:
    """What the application asks of a backend."""

    system_instruction: str
    user_payload_json: str
    requested_response_mode: str


@dataclass(frozen=True)
class InferenceCompletion:
    """What one completed backend call returned."""

    content_text: str | None
    is_refusal: bool
    finish_reason: str
    backend_request_id: str | None
    backend_response_id: str | None
    backend_model: str | None
    # The adapter computes `is_refusal` while parsing. It does not retain the raw refusal body.
    diagnostic: ResponseDiagnostic = ABSENT_DIAGNOSTIC


def _retry_after(response) -> int | None:
    """Return the Retry-After seconds a rate-limited answer states, when it states one."""
    raw = response.headers.get("Retry-After")
    try:
        return int(raw) if raw is not None else None
    except ValueError:
        return None


def _status_failure(status: int, diagnostic, retry_after: int | None) -> InferenceBackendError | None:
    """Return the typed failure one HTTP status maps to, or None when the status carries none."""
    if 300 <= status < 400:
        return InvalidBackendConfiguration(
            f"The backend redirected the call (HTTP {status}), which is not followed.", diagnostic=diagnostic
        )
    if status in (401, 403):
        return AuthenticationFailure(f"The backend refused the credential (HTTP {status}).", diagnostic=diagnostic)
    if status == 429:
        return RateLimited("The backend rate limited the call.", retry_after=retry_after, diagnostic=diagnostic)
    if status in TRANSIENT_STATUSES:
        return TransportFailure(f"The backend answered HTTP {status}.", diagnostic=diagnostic)
    if status >= 400:
        return InvalidBackendConfiguration(
            f"The backend rejected the request (HTTP {status}), which repeating cannot fix.",
            diagnostic=diagnostic,
        )
    return None


def _processing_failure(exc: ResponseProcessingFailure, api_key: str) -> InferenceBackendError:
    """Return the typed failure one interrupted or refused response body maps to."""
    diagnostic = ResponseDiagnostic(receipt=BODY_INTERRUPTED, status_code=exc.response.status_code)
    if isinstance(exc.cause, ResponseBodyTooLarge):
        # The body is refused unread, so the status is the only classification left.
        return _status_failure(exc.response.status_code, diagnostic, _retry_after(exc.response)) or MalformedEnvelope(
            "The backend response is too large.", diagnostic=diagnostic
        )
    if isinstance(exc.cause, ValueError):
        return InvalidBackendConfiguration(
            f"The backend answered with a location this delivery cannot use ({type(exc.cause).__name__}).",
            diagnostic=_diagnostic(exc.response, api_key),
        )
    if isinstance(exc.cause, requests.Timeout) or _is_response_read_timeout(exc.cause):
        return BackendTimeout(
            "The backend did not finish its answer inside the configured limits.", diagnostic=diagnostic
        )
    return TransportFailure(f"The backend answer was interrupted ({type(exc.cause).__name__}).", diagnostic=diagnostic)


def _is_response_read_timeout(exc: Exception) -> bool:
    """Return whether Requests timed out after it had received response headers."""
    return isinstance(exc, requests.ConnectionError) and bool(exc.args) and isinstance(exc.args[0], ReadTimeoutError)


class OpenAICompatibleAdapter:
    """Call one OpenAI-compatible Chat Completions endpoint, without streaming or tools."""

    def __init__(
        self,
        api_root: str,
        model: str,
        allowlist: Sequence[str],
        authentication: str = "bearer",
        response_mode: str = "prompt_json",
        connect_timeout: int = 5,
        read_timeout: int = 60,
        session: "requests.Session | None" = None,
        deadline: WallClockDeadline | None = None,
    ):
        # Verbatim: normalizing here would pass the request-time recheck a value the form refuses.
        self.api_root = api_root
        self.model = model
        self.allowlist = tuple(allowlist)
        self.authentication = authentication
        self.response_mode = response_mode
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self._session = session or requests.Session()
        self._deadline = deadline

    def _check_response_mode(self, request: InferenceRequest) -> None:
        """Reject a request for a mode this backend is not configured to serve."""
        if request.requested_response_mode != self.response_mode:
            raise InvalidBackendConfiguration(
                f"This backend is configured for '{self.response_mode}', "
                f"but the request asked for '{request.requested_response_mode}'."
            )
        if self.response_mode == "json_schema":
            raise InvalidBackendConfiguration(
                "The json_schema response mode needs a schema to send, and this delivery stores none."
            )

    def _body(self, request: InferenceRequest) -> dict:
        """Return the Chat Completions body: one choice, no streaming, no tools."""
        body = {
            "model": self.model,
            "n": 1,
            "stream": False,
            "messages": [
                {"role": "system", "content": request.system_instruction},
                {"role": "user", "content": request.user_payload_json},
            ],
        }
        if self.response_mode == "json_object":
            body["response_format"] = {"type": "json_object"}
        return body

    def _resolved_destinations(self) -> tuple[str, ...]:
        """Return all approved addresses, rechecking the destination at request time."""
        try:
            validate_api_root(self.api_root, self.allowlist, self.authentication)
            addresses = (
                resolve_addresses(self.api_root)
                if self._deadline is None
                else self._deadline.run(resolve_addresses, self.api_root)
            )
            assert_resolved_address_allowed(self.api_root, self.allowlist, addresses)
        except WallClockDeadlineExceeded:
            raise BackendTimeout("The connection test exceeded its overall time limit.") from None
        except InvalidInferenceConfiguration as exc:
            raise InvalidBackendConfiguration(str(exc)) from None
        return addresses

    def _send(
        self,
        method: str,
        path: str,
        api_key: str,
        *,
        response_body_limit: int,
        **kwargs,
    ) -> requests.Response:
        """Send one authenticated request through the checked and pinned destination boundary."""
        resolved_addresses = self._resolved_destinations()
        headers = {"Accept": "application/json"}
        if "json" in kwargs:
            headers["Content-Type"] = "application/json"
        if self.authentication == "bearer":
            headers["Authorization"] = f"Bearer {api_key}"
        connection_failure: requests.RequestException | None = None
        for resolved_address in resolved_addresses:
            try:
                response = request_to_resolved_address(
                    self._session,
                    method,
                    f"{self.api_root}{path}",
                    resolved_address,
                    headers=headers,
                    timeout=(self.connect_timeout, self.read_timeout),
                    deadline=self._deadline,
                    response_body_limit=response_body_limit,
                    # A redirect is a different destination, so it is refused rather than followed.
                    allow_redirects=False,
                    **kwargs,
                )
            except ResponseProcessingFailure as exc:
                raise _processing_failure(exc, api_key) from None
            except requests.ConnectTimeout as exc:
                connection_failure = exc
                continue
            except requests.ConnectionError as exc:
                if is_preconnect_failure(exc):
                    connection_failure = exc
                    continue
                raise TransportFailure(
                    f"The backend could not be reached ({type(exc).__name__}).",
                    diagnostic=ABSENT_DIAGNOSTIC,
                ) from None
            except requests.Timeout:
                raise BackendTimeout("The backend did not answer inside the configured limits.") from None
            except requests.RequestException as exc:
                # A cut-short body raises with no response attached, so its bytes cannot be recovered.
                receipt = BODY_INTERRUPTED if isinstance(exc, requests.exceptions.ChunkedEncodingError) else BODY_ABSENT
                raise TransportFailure(
                    f"The backend could not be reached ({type(exc).__name__}).",
                    diagnostic=ResponseDiagnostic(receipt=receipt),
                ) from None
            except ValueError as exc:
                # `requests` raises this bare while preparing an unparsable redirect target.
                raise InvalidBackendConfiguration(
                    f"The backend answered with a location this delivery cannot use ({type(exc).__name__})."
                ) from None
            return response

        if isinstance(connection_failure, requests.ConnectTimeout):
            raise BackendTimeout("The backend did not answer inside the configured limits.") from None
        raise TransportFailure(
            f"The backend could not be reached ({type(connection_failure).__name__}).",
            diagnostic=ABSENT_DIAGNOSTIC,
        ) from None

    @staticmethod
    def _raise_for_status(response: requests.Response, api_key: str) -> ResponseDiagnostic:
        """Return a safe diagnostic for success, or raise the typed HTTP failure."""
        diagnostic = _diagnostic(response, api_key)
        failure = _status_failure(response.status_code, diagnostic, _retry_after(response))
        if failure is not None:
            raise failure
        return diagnostic

    def complete(self, request: InferenceRequest, api_key: str) -> InferenceCompletion:
        """Return one completion, or raise the typed error the backend condition maps to."""
        self._check_response_mode(request)
        response = self._send(
            "POST",
            CHAT_COMPLETIONS_PATH,
            api_key,
            response_body_limit=CHAT_COMPLETION_RESPONSE_LIMIT,
            json=self._body(request),
        )
        return self._read(response, api_key)

    def discover_models(self, api_key: str) -> tuple[str, ...]:
        """Return bounded model-id suggestions from a compatible optional models endpoint."""
        response = self._send(
            "GET",
            MODELS_PATH,
            api_key,
            response_body_limit=MODEL_DISCOVERY_RESPONSE_LIMIT,
        )
        diagnostic = self._raise_for_status(response, api_key)
        if diagnostic.redacted:
            raise MalformedEnvelope("The backend model list contained credential material.", diagnostic=diagnostic)
        try:
            envelope = response.json()
        except (ValueError, RecursionError):
            raise MalformedEnvelope("The backend model list is not JSON.", diagnostic=diagnostic) from None
        if not isinstance(envelope, dict) or not isinstance(envelope.get("data"), list):
            raise MalformedEnvelope("The backend did not provide a compatible model list.", diagnostic=diagnostic)

        models = []
        seen = set()
        for item in envelope["data"]:
            model_id = item.get("id") if isinstance(item, dict) else None
            if (
                not isinstance(model_id, str)
                or not model_id
                or model_id != model_id.strip()
                or not model_id.isprintable()
                or len(model_id) > MODEL_MAX_LENGTH
                or model_id in seen
            ):
                continue
            seen.add(model_id)
            models.append(model_id)
            if len(models) == MODEL_DISCOVERY_LIMIT:
                break
        return tuple(models)

    def _read(self, response, api_key: str = "") -> InferenceCompletion:
        """Classify the answer, then parse the one envelope a completed call returns."""
        # Captured before any parsing, so every failure below can carry what it received.
        diagnostic = self._raise_for_status(response, api_key)
        try:
            envelope = response.json()
        except (ValueError, RecursionError):
            # Below Python 3.14 the decoder recurses, so deep nesting raises outside the ValueError tree.
            raise MalformedEnvelope(
                "The backend answered with a body that is not JSON.", diagnostic=diagnostic
            ) from None
        return self._completion(envelope, diagnostic)

    @staticmethod
    def _completion(envelope, diagnostic: ResponseDiagnostic = ABSENT_DIAGNOSTIC) -> InferenceCompletion:
        """Return the completion one envelope describes, rejecting anything else."""
        if not isinstance(envelope, dict):
            raise MalformedEnvelope("The backend answered with no completion object.", diagnostic=diagnostic)
        choices = envelope.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            raise MalformedEnvelope("A completion carries exactly one choice.", diagnostic=diagnostic)
        choice = choices[0]
        if not isinstance(choice, dict):
            raise MalformedEnvelope("The backend answered with an unreadable choice.", diagnostic=diagnostic)
        finish_reason = choice.get("finish_reason")
        if finish_reason not in ACCEPTED_FINISH_REASONS:
            raise MalformedEnvelope("The backend stopped for a reason that produces no answer.", diagnostic=diagnostic)
        message = choice.get("message")
        if not isinstance(message, dict):
            raise MalformedEnvelope("The backend answered with an unreadable message.", diagnostic=diagnostic)
        content = message.get("content")
        refusal = message.get("refusal")
        # Content parts are common on OpenAI-compatible servers, and this delivery reads text only.
        if content is not None and not isinstance(content, str):
            raise MalformedEnvelope(
                "The backend answered with content this delivery cannot read as text.", diagnostic=diagnostic
            )
        # A refusal is a completed call that produced no answer, not a failure to call.
        is_refusal = bool(refusal) or not (content or "").strip()
        return InferenceCompletion(
            content_text=content,
            is_refusal=is_refusal,
            finish_reason=finish_reason,
            backend_request_id=envelope.get("request_id"),
            backend_response_id=envelope.get("id"),
            backend_model=envelope.get("model"),
            diagnostic=diagnostic,
        )


def encode_payload(value) -> str:
    """Return the compact JSON one user payload travels as."""
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


__all__ = (
    "CHAT_COMPLETIONS_PATH",
    "MODELS_PATH",
    "MODEL_DISCOVERY_LIMIT",
    "AuthenticationFailure",
    "BackendTimeout",
    "InferenceBackendError",
    "InferenceCompletion",
    "InferenceRequest",
    "InvalidBackendConfiguration",
    "MalformedEnvelope",
    "OpenAICompatibleAdapter",
    "RateLimited",
    "TransportFailure",
    "encode_payload",
)
