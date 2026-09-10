# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The OpenAI-compatible Inference Backend adapter (specification 8.1, 8.3, 8.4).

One non-streaming Chat Completions call, built entirely from configuration. The adapter knows
nothing about NetBox, candidates, proposals or jobs: it takes an InferenceRequest and returns an
InferenceCompletion, or raises a typed backend error.

A returned completion means the call reached the backend and the envelope parsed. Deciding what the
content means belongs to the application service, not here.
"""

import json

from dataclasses import dataclass
from collections.abc import Sequence

import requests

from .inference_trust import (
    InvalidInferenceConfiguration,
    assert_resolved_address_allowed,
    resolve_addresses,
    validate_api_root,
)

CHAT_COMPLETIONS_PATH = "/chat/completions"

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


@dataclass(frozen=True)
class ResponseDiagnostic:
    """What one call received, for an operator to read when it failed.

    `text` is the decoded body, captured before any validation, and is None unless the receipt is
    `empty` or `present`. `redacted` records that the credential appeared and the text was replaced.
    """

    receipt: str
    text: str | None = None
    status_code: int | None = None
    redacted: bool = False


ABSENT_DIAGNOSTIC = ResponseDiagnostic(receipt=BODY_ABSENT)


def _echoes_key(text: str, api_key: str) -> bool:
    """Return whether the body carries the credential, literally or behind a JSON escape."""
    if api_key in text:
        return True
    try:
        return api_key in json.dumps(json.loads(text), ensure_ascii=False)
    except (ValueError, RecursionError):
        return False


def _diagnostic(response, api_key: str) -> ResponseDiagnostic:
    """Return the diagnostic one answered call carries, with the credential taken out of it."""
    try:
        text = response.text
    except Exception:  # noqa: BLE001 - a body that cannot be decoded is a lost body, not a new failure
        return ResponseDiagnostic(receipt=BODY_INTERRUPTED, status_code=response.status_code)
    if api_key and _echoes_key(text, api_key):
        return ResponseDiagnostic(receipt=BODY_PRESENT, text=_REDACTED, status_code=response.status_code, redacted=True)
    receipt = BODY_PRESENT if text else BODY_EMPTY
    return ResponseDiagnostic(receipt=receipt, text=text, status_code=response.status_code)


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
    # A refusal's text lives only here: `is_refusal` is computed from it and the message is dropped.
    diagnostic: ResponseDiagnostic = ABSENT_DIAGNOSTIC


def _retry_after(response) -> int | None:
    """Return the Retry-After seconds a rate-limited answer states, when it states one."""
    raw = response.headers.get("Retry-After")
    try:
        return int(raw) if raw is not None else None
    except ValueError:
        return None


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

    def _check_destination(self) -> None:
        """Reject a destination the deployment has not approved, rechecked at request time."""
        try:
            validate_api_root(self.api_root, self.allowlist, self.authentication)
            assert_resolved_address_allowed(self.api_root, self.allowlist, resolve_addresses(self.api_root))
        except InvalidInferenceConfiguration as exc:
            raise InvalidBackendConfiguration(str(exc)) from None

    def complete(self, request: InferenceRequest, api_key: str) -> InferenceCompletion:
        """Return one completion, or raise the typed error the backend condition maps to."""
        self._check_response_mode(request)
        self._check_destination()
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.authentication == "bearer":
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            response = self._session.post(
                f"{self.api_root}{CHAT_COMPLETIONS_PATH}",
                json=self._body(request),
                headers=headers,
                timeout=(self.connect_timeout, self.read_timeout),
                # A redirect is a different destination, so it is refused rather than followed.
                allow_redirects=False,
            )
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
        return self._read(response, api_key)

    def _read(self, response, api_key: str = "") -> InferenceCompletion:
        """Classify the answer, then parse the one envelope a completed call returns."""
        # Captured before any classification, so every failure below can carry what it received.
        diagnostic = _diagnostic(response, api_key)
        status = response.status_code
        if status in (301, 302, 303, 307, 308):
            raise InvalidBackendConfiguration(
                f"The backend redirected the call (HTTP {status}), which is not followed.", diagnostic=diagnostic
            )
        if status in (401, 403):
            raise AuthenticationFailure(f"The backend refused the credential (HTTP {status}).", diagnostic=diagnostic)
        if status == 429:
            raise RateLimited(
                "The backend rate limited the call.", retry_after=_retry_after(response), diagnostic=diagnostic
            )
        if status in TRANSIENT_STATUSES:
            raise TransportFailure(f"The backend answered HTTP {status}.", diagnostic=diagnostic)
        if status >= 400:
            raise InvalidBackendConfiguration(
                f"The backend rejected the request (HTTP {status}), which repeating cannot fix.",
                diagnostic=diagnostic,
            )
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
