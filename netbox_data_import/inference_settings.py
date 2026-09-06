# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Shape of the three Inference Backend plugin settings, checked at startup (specification 8.2.1).

Shape is all this module decides. Credential resolution and network liveness belong to the worker
and fail at request time, so nothing here opens a socket.
"""

from collections.abc import Mapping, Sequence
from typing import Any

from .inference_trust import InvalidInferenceConfiguration, validate_api_root, validate_origin

ORIGIN_ALLOWLIST_SETTING = "inference_backend_origin_allowlist"
FILE_FALLBACK_SETTING = "inference_backend"
VAULT_SETTING = "vault"

# The fallback is one whole backend, so its key cannot be chosen per deployment.
FILE_FALLBACK_KEY = "file-fallback"

# The InferenceBackend row fields minus the backend key and enabled.
FILE_FALLBACK_FIELDS = (
    "display_name",
    "adapter_type",
    "api_root",
    "model",
    "authentication",
    "response_mode",
    "credential_reference",
    "connect_timeout",
    "read_timeout",
)

VAULT_AUTH_METHODS = ("proxy", "token")
VAULT_FIELDS = ("address", "auth_method", "namespace", "ca_bundle", "connect_timeout", "read_timeout")

# Named so a deployment that sets one is told why, rather than having it silently ignored.
VAULT_FORBIDDEN_FIELDS = ("mount", "token", "role_id", "secret_id", "verify", "tls_skip_verify")

CREDENTIAL_REFERENCE_FIELDS = ("backend", "mount", "path", "field")
CREDENTIAL_REFERENCE_BACKEND = "vault_kv_v2"


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    """Return *value* as a mapping, or reject it."""
    if not isinstance(value, Mapping):
        raise InvalidInferenceConfiguration(f"'{label}' must be a mapping, got {type(value).__name__}.")
    return value


def validate_origin_allowlist(value: Any) -> tuple[str, ...]:
    """Return the allowlist origins, rejecting anything that is not one exact origin."""
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise InvalidInferenceConfiguration(
            f"'{ORIGIN_ALLOWLIST_SETTING}' must be a list of origin strings, got {type(value).__name__}."
        )
    return tuple(validate_origin(entry, setting=ORIGIN_ALLOWLIST_SETTING) for entry in value)


def validate_vault_settings(value: Any) -> Mapping[str, Any]:
    """Return the vault connection mapping, rejecting secret material and the KV v2 mount."""
    mapping = _require_mapping(value, VAULT_SETTING)
    for name in VAULT_FORBIDDEN_FIELDS:
        if name in mapping:
            raise InvalidInferenceConfiguration(
                f"'{VAULT_SETTING}.{name}' is not accepted. Connection and machine identity data belong to the "
                f"deployment, and the KV v2 mount belongs to the credential reference."
            )
    unknown = sorted(set(mapping) - set(VAULT_FIELDS))
    if unknown:
        raise InvalidInferenceConfiguration(f"Unknown '{VAULT_SETTING}' key(s): {', '.join(unknown)}.")
    if not mapping.get("address"):
        raise InvalidInferenceConfiguration(f"'{VAULT_SETTING}.address' is required.")
    bundle = mapping.get("ca_bundle")
    if "ca_bundle" in mapping and (not isinstance(bundle, str) or not bundle.strip()):
        # requests reads a bool here as "skip verification", which this setting must never mean.
        raise InvalidInferenceConfiguration(
            f"'{VAULT_SETTING}.ca_bundle' must be a path to a CA bundle, got {type(bundle).__name__}."
        )
    method = mapping.get("auth_method", "proxy")
    if method not in VAULT_AUTH_METHODS:
        raise InvalidInferenceConfiguration(
            f"'{VAULT_SETTING}.auth_method' must be one of {', '.join(VAULT_AUTH_METHODS)}, got '{method}'."
        )
    return mapping


_UNSAFE_PATH_SEGMENTS = frozenset({"", ".", ".."})


def _validate_vault_path(value: Any, label: str, *, segments: bool) -> None:
    """Reject a Vault path value that could change the request it is interpolated into."""
    text = str(value)
    for character in "?#%":
        if character in text:
            raise InvalidInferenceConfiguration(f"'{label}' cannot contain '{character}'.")
    if not segments and "/" in text:
        raise InvalidInferenceConfiguration(f"'{label}' names one path segment, so it cannot contain '/'.")
    if any(part in _UNSAFE_PATH_SEGMENTS for part in text.split("/")):
        raise InvalidInferenceConfiguration(f"'{label}' cannot hold an empty, '.' or '..' path segment.")


def validate_credential_reference(value: Any, label: str = "credential_reference") -> Mapping[str, Any]:
    """Return the typed Vault KV v2 reference, rejecting connection data and secret material."""
    mapping = _require_mapping(value, label)
    unknown = sorted(set(mapping) - set(CREDENTIAL_REFERENCE_FIELDS))
    if unknown:
        raise InvalidInferenceConfiguration(f"Unknown '{label}' key(s): {', '.join(unknown)}.")
    missing = [name for name in CREDENTIAL_REFERENCE_FIELDS if not mapping.get(name)]
    if missing:
        raise InvalidInferenceConfiguration(f"'{label}' is missing required key(s): {', '.join(missing)}.")
    if mapping["backend"] != CREDENTIAL_REFERENCE_BACKEND:
        raise InvalidInferenceConfiguration(
            f"'{label}.backend' must be '{CREDENTIAL_REFERENCE_BACKEND}', got '{mapping['backend']}'."
        )
    _validate_vault_path(mapping["mount"], f"{label}.mount", segments=False)
    _validate_vault_path(mapping["path"], f"{label}.path", segments=True)
    return mapping


def validate_file_fallback(value: Any, allowlist: Sequence[str]) -> Mapping[str, Any]:
    """Return the whole-backend fallback, rejecting a field set that is not exactly the row's."""
    mapping = _require_mapping(value, FILE_FALLBACK_SETTING)
    unknown = sorted(set(mapping) - set(FILE_FALLBACK_FIELDS))
    if unknown:
        raise InvalidInferenceConfiguration(f"Unknown '{FILE_FALLBACK_SETTING}' key(s): {', '.join(unknown)}.")
    missing = [name for name in FILE_FALLBACK_FIELDS if name not in mapping]
    if missing:
        raise InvalidInferenceConfiguration(
            f"'{FILE_FALLBACK_SETTING}' is missing required key(s): {', '.join(missing)}."
        )
    validate_credential_reference(mapping["credential_reference"], f"{FILE_FALLBACK_SETTING}.credential_reference")
    validate_api_root(
        mapping["api_root"],
        allowlist=allowlist,
        authentication=mapping.get("authentication", "bearer"),
        setting=f"{FILE_FALLBACK_SETTING}.api_root",
    )
    return mapping


def validate_plugin_settings(user_config: Mapping[str, Any]) -> None:
    """Reject a malformed Inference Backend configuration before the application serves a request."""
    allowlist = validate_origin_allowlist(user_config.get(ORIGIN_ALLOWLIST_SETTING, ()))
    if VAULT_SETTING in user_config:
        validate_vault_settings(user_config[VAULT_SETTING])
    if FILE_FALLBACK_SETTING in user_config:
        validate_file_fallback(user_config[FILE_FALLBACK_SETTING], allowlist)


__all__ = (
    "CREDENTIAL_REFERENCE_BACKEND",
    "CREDENTIAL_REFERENCE_FIELDS",
    "FILE_FALLBACK_FIELDS",
    "FILE_FALLBACK_KEY",
    "FILE_FALLBACK_SETTING",
    "InvalidInferenceConfiguration",
    "ORIGIN_ALLOWLIST_SETTING",
    "VAULT_AUTH_METHODS",
    "VAULT_SETTING",
    "validate_credential_reference",
    "validate_file_fallback",
    "validate_origin_allowlist",
    "validate_plugin_settings",
    "validate_vault_settings",
)
