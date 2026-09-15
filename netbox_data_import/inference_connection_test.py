# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The foreground Inference Backend connection test (specification 8.6).

It resolves the configured reference, calls Chat Completions, and reports one typed category. Model
discovery is optional. The result never returns a secret value, provider response text, or a Vault
response body.
"""

from contextlib import suppress
from dataclasses import dataclass

from .inference_adapter import InferenceBackendError, InferenceRequest
from .inference_backend import NoActiveInferenceBackend, adapter_for_backend, resolve_backend_by_id
from .inference_credentials import CredentialFailure, credential_backend_for
from .inference_settings import VAULT_SETTING, InvalidInferenceConfiguration
from .inference_transport import WallClockDeadline, WallClockDeadlineExceeded

CONNECTION_TEST_CATEGORIES = (
    "ok",
    "credential_unavailable",
    "credential_denied",
    "invalid_credential_reference",
    "invalid_secret_material",
    "invalid_configuration",
    "transport_failure",
    "timeout",
    "authentication_failure",
    "rate_limit",
    "malformed_envelope",
    "invalid_response",
)


@dataclass(frozen=True)
class ConnectionTestResult:
    """One typed connection-test outcome: no secret, no Vault response body."""

    category: str
    detail: str
    backend_key: str | None = None
    backend_source: str | None = None
    models: tuple[str, ...] = ()


def _connection_request(response_mode: str) -> InferenceRequest:
    """Return the small request that verifies the configured model and Chat Completions route."""
    return InferenceRequest(
        system_instruction="Return one small JSON object with the key ok and the value true.",
        user_payload_json='{"connection_test":true}',
        requested_response_mode=response_mode,
    )


def run_connection_test(pk: int, backend_key: str) -> ConnectionTestResult:
    """Resolve the authorized row's credential and report with its display key."""
    try:
        backend = resolve_backend_by_id(pk)
        # Model discovery is optional, so the whole foreground test gets one normal-call budget.
        deadline = WallClockDeadline.after(backend.connect_timeout + backend.read_timeout)
        adapter = adapter_for_backend(backend, deadline=deadline)
    except NoActiveInferenceBackend:
        return ConnectionTestResult(
            "invalid_configuration", f"Inference Backend '{backend_key}' no longer exists.", backend_key
        )
    except InvalidInferenceConfiguration as exc:
        return ConnectionTestResult("invalid_configuration", str(exc), backend_key)
    except CredentialFailure as exc:
        return ConnectionTestResult(exc.category, str(exc), backend_key)

    from .inference_backend import plugin_settings

    try:
        with credential_backend_for(
            backend.credential_reference,
            plugin_settings().get(VAULT_SETTING, {}),
            deadline=deadline,
        ) as store:
            api_key = store.resolve(backend.credential_reference)
    except WallClockDeadlineExceeded:
        return ConnectionTestResult(
            "timeout",
            "The connection test exceeded its overall time limit.",
            backend_key,
            backend.source,
        )
    except CredentialFailure as exc:
        return ConnectionTestResult(exc.category, str(exc), backend_key, backend.source)

    models: tuple[str, ...] = ()
    try:
        with suppress(InferenceBackendError):
            models = adapter.discover_models(api_key)
        # Model discovery is optional. The real completion call decides connection success.
        try:
            completion = adapter.complete(_connection_request(backend.response_mode), api_key)
        except InferenceBackendError as exc:
            detail = str(exc)
            if models and exc.diagnostic.status_code == 400:
                detail += " Select one of the available models below, save the backend, and run the test again."
            return ConnectionTestResult(exc.category, detail, backend_key, backend.source, models)
        if completion.is_refusal or not (completion.content_text or "").strip():
            return ConnectionTestResult(
                "invalid_response",
                "The backend completed the test but did not return an answer.",
                backend_key,
                backend.source,
                models,
            )
        if completion.diagnostic.redacted:
            return ConnectionTestResult(
                "invalid_response",
                "The backend echoed credential material. Its response was discarded.",
                backend_key,
                backend.source,
                models,
            )
    finally:
        api_key = ""

    if models:
        model_word = "model" if len(models) == 1 else "models"
        detail = (
            f"The credential resolved, and the API completed a test request. "
            f"The endpoint offered {len(models)} {model_word}."
        )
    else:
        detail = (
            "The credential resolved, and the API completed a test request. "
            "The endpoint did not provide a compatible model list. Enter the model id manually."
        )
    return ConnectionTestResult(
        "ok",
        detail,
        backend_key,
        backend.source,
        models,
    )


__all__ = ("CONNECTION_TEST_CATEGORIES", "ConnectionTestResult", "run_connection_test")
