# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The credential boundary and its Vault KV v2 implementation (specification 8.5, 8.6).

The seam has four responsibilities: validate a typed reference, resolve it through the selected
credential backend, return secret material for the lifetime of one outbound request, and classify
failures. No message built here carries the secret or a Vault response body, so a caller may log
any failure it catches.
"""

import os

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import requests

from .inference_settings import (
    CREDENTIAL_REFERENCE_BACKEND,
    validate_credential_reference,
    validate_vault_settings,
)
from .inference_trust import InvalidInferenceConfiguration

# The deployment owns the token; the plugin never stores one.
VAULT_TOKEN_ENVIRONMENT_VARIABLE = "VAULT_TOKEN"

DEFAULT_CONNECT_TIMEOUT = 5
DEFAULT_READ_TIMEOUT = 60


class CredentialFailure(Exception):
    """A credential resolution that failed, carrying its typed category and no secret."""

    category = "credential_unavailable"


class CredentialUnavailable(CredentialFailure):
    """The credential store could not be reached, or held no answer for this reference."""

    category = "credential_unavailable"


class CredentialDenied(CredentialFailure):
    """The credential store refused the read."""

    category = "credential_denied"


class InvalidCredentialReference(CredentialFailure):
    """The reference is not a usable typed reference."""

    category = "invalid_credential_reference"


class InvalidSecretMaterial(CredentialFailure):
    """The store answered, but the named field holds nothing usable as a key."""

    category = "invalid_secret_material"


class InvalidCredentialConfiguration(CredentialFailure):
    """The deployment-owned credential settings cannot be used as given."""

    category = "invalid_configuration"


@dataclass(frozen=True)
class CredentialReference:
    """One typed Vault KV v2 reference: restricted configuration metadata, never secret material."""

    backend: str
    mount: str
    path: str
    field: str

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> "CredentialReference":
        """Return the validated reference, rejecting connection data and secret material."""
        try:
            validated = validate_credential_reference(mapping)
        except InvalidInferenceConfiguration as exc:
            raise InvalidCredentialReference(str(exc)) from exc
        return cls(
            backend=validated["backend"],
            mount=validated["mount"],
            path=validated["path"],
            field=validated["field"],
        )


class CredentialBackend(Protocol):
    """Resolve one typed reference into secret material for the lifetime of one request."""

    name: str

    def resolve(self, reference: CredentialReference) -> str:
        """Return the secret the reference names."""
        ...


class VaultKvV2CredentialBackend:
    """Read one named field from one KV v2 path, through Vault Proxy or a deployment token."""

    name = CREDENTIAL_REFERENCE_BACKEND

    def __init__(self, settings: Mapping[str, Any], session: requests.Session | None = None):
        try:
            self._settings = validate_vault_settings(settings)
        except InvalidInferenceConfiguration as exc:
            raise InvalidCredentialConfiguration(str(exc)) from exc
        self._session = session or requests.Session()

    def _headers(self) -> dict[str, str]:
        """Return the request headers, reading a token only when the deployment selected one."""
        headers = {"Accept": "application/json"}
        if namespace := self._settings.get("namespace"):
            headers["X-Vault-Namespace"] = namespace
        if self._settings.get("auth_method", "proxy") != "token":
            return headers
        token = os.environ.get(VAULT_TOKEN_ENVIRONMENT_VARIABLE, "")
        if not token:
            raise InvalidCredentialConfiguration(
                f"vault.auth_method is 'token' but {VAULT_TOKEN_ENVIRONMENT_VARIABLE} is not set in the "
                f"worker environment."
            )
        headers["X-Vault-Token"] = token
        return headers

    def _read(self, reference: CredentialReference) -> requests.Response:
        """Perform the one KV v2 read this reference names."""
        address = str(self._settings["address"]).rstrip("/")
        url = f"{address}/v1/{reference.mount}/data/{reference.path}"
        timeout = (
            self._settings.get("connect_timeout", DEFAULT_CONNECT_TIMEOUT),
            self._settings.get("read_timeout", DEFAULT_READ_TIMEOUT),
        )
        try:
            return self._session.get(
                url,
                headers=self._headers(),
                timeout=timeout,
                verify=self._settings.get("ca_bundle", True),
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            # The exception text can quote the request, so only its class is reported.
            raise CredentialUnavailable(
                f"The credential store at {address} could not be reached ({type(exc).__name__})."
            ) from None

    def resolve(self, reference: CredentialReference) -> str:
        """Return the secret the reference names, classifying every failure without quoting Vault."""
        if reference.backend != self.name:
            raise InvalidCredentialReference(f"This backend resolves '{self.name}' references only.")
        response = self._read(reference)
        if response.status_code in (401, 403):
            raise CredentialDenied(f"The credential store refused the read (HTTP {response.status_code}).")
        if response.status_code == 404:
            raise CredentialUnavailable("The credential store holds no secret at the referenced path.")
        if response.status_code >= 400:
            raise CredentialUnavailable(f"The credential store answered HTTP {response.status_code}.")
        try:
            envelope = response.json()
            data = envelope["data"]["data"]
        except (ValueError, KeyError, TypeError):
            raise CredentialUnavailable("The credential store answered with an unreadable KV v2 envelope.") from None
        if reference.field not in data:
            raise InvalidSecretMaterial("The referenced field is absent from the stored secret.")
        value = data[reference.field]
        if not isinstance(value, str):
            raise InvalidSecretMaterial("The referenced field does not hold a string.")
        if not value.strip():
            raise InvalidSecretMaterial("The referenced field is empty.")
        return value


def credential_backend_for(reference: CredentialReference, vault_settings: Mapping[str, Any]) -> CredentialBackend:
    """Return the credential backend that resolves this reference."""
    if reference.backend == CREDENTIAL_REFERENCE_BACKEND:
        return VaultKvV2CredentialBackend(vault_settings)
    raise InvalidCredentialReference(f"No credential backend resolves '{reference.backend}' references.")


__all__ = (
    "CredentialBackend",
    "CredentialDenied",
    "CredentialFailure",
    "CredentialReference",
    "CredentialUnavailable",
    "InvalidCredentialConfiguration",
    "InvalidCredentialReference",
    "InvalidSecretMaterial",
    "VAULT_TOKEN_ENVIRONMENT_VARIABLE",
    "VaultKvV2CredentialBackend",
    "credential_backend_for",
)
