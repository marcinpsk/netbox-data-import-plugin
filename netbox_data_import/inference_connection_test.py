# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The Inference Backend connection test (specification 8.6).

It runs on the worker queue, on the same secret boundary as a proposal job, so no web process ever
resolves a credential. It resolves the configured reference and reports one typed category. It
never returns a secret value and never a Vault response body.
"""

from dataclasses import dataclass

from .inference_backend import NoActiveInferenceBackend, resolve_backend_by_key
from .inference_credentials import CredentialFailure, credential_backend_for
from .inference_settings import VAULT_SETTING, InvalidInferenceConfiguration

CONNECTION_TEST_CATEGORIES = (
    "ok",
    "credential_unavailable",
    "credential_denied",
    "invalid_credential_reference",
    "invalid_secret_material",
    "invalid_configuration",
)


@dataclass(frozen=True)
class ConnectionTestResult:
    """One typed connection-test outcome: no secret, no Vault response body."""

    category: str
    detail: str
    backend_key: str | None = None
    backend_source: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        """Return the result as job data. It carries no credential reference."""
        return {
            "category": self.category,
            "detail": self.detail,
            "backend_key": self.backend_key,
            "backend_source": self.backend_source,
        }


def run_connection_test(backend_key: str) -> ConnectionTestResult:
    """Resolve one named backend's credential once and report what happened."""
    try:
        backend = resolve_backend_by_key(backend_key)
    except NoActiveInferenceBackend as exc:
        return ConnectionTestResult("invalid_configuration", str(exc))
    except InvalidInferenceConfiguration as exc:
        return ConnectionTestResult("invalid_configuration", str(exc))
    except CredentialFailure as exc:
        return ConnectionTestResult(exc.category, str(exc))

    from .inference_backend import plugin_settings

    try:
        store = credential_backend_for(backend.credential_reference, plugin_settings().get(VAULT_SETTING, {}))
        store.resolve(backend.credential_reference)
    except CredentialFailure as exc:
        return ConnectionTestResult(exc.category, str(exc), backend.backend_key, backend.source)
    return ConnectionTestResult(
        "ok",
        "The credential resolved and holds usable material.",
        backend.backend_key,
        backend.source,
    )


__all__ = ("CONNECTION_TEST_CATEGORIES", "ConnectionTestResult", "run_connection_test")
