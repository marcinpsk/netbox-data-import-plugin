# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Shape of the three Inference Backend plugin settings, checked at startup (specification 8.2.1).

Shape is all this module decides. Credential resolution and network liveness belong to the worker
and fail at request time, so nothing here opens a socket.
"""

from collections.abc import Mapping, Sequence
from typing import Any

from .inference_trust import (
    InvalidInferenceConfiguration,
    is_local_endpoint,
    split_url as _split_url,
    validate_api_root,
    validate_origin,
)

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

# The InferenceBackend column choices, here because settings load before the app registry.
ADAPTER_TYPES = (("openai_compatible", "OpenAI compatible"),)
AUTHENTICATION_METHODS = (("bearer", "Bearer token"),)
RESPONSE_MODES = (
    ("prompt_json", "JSON asked for in the prompt"),
    ("json_object", "JSON object mode"),
    ("json_schema", "JSON schema mode"),
)

# The InferenceBackend column widths the fallback has to respect.
API_ROOT_MAX_LENGTH = 500
DISPLAY_NAME_MAX_LENGTH = 200
MODEL_MAX_LENGTH = 200

# PositiveIntegerField stores up to this. One second is the smallest timeout that can make a call.
TIMEOUT_MIN = 1
TIMEOUT_MAX = 2147483647

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
    unknown = sorted(str(key) for key in set(mapping) - set(VAULT_FIELDS))
    if unknown:
        raise InvalidInferenceConfiguration(f"Unknown '{VAULT_SETTING}' key(s): {', '.join(unknown)}.")
    if not mapping.get("address"):
        raise InvalidInferenceConfiguration(f"'{VAULT_SETTING}.address' is required.")
    _validate_vault_address(mapping["address"])
    namespace = mapping.get("namespace")
    if "namespace" in mapping and (not isinstance(namespace, str) or not namespace.strip()):
        raise InvalidInferenceConfiguration(
            f"'{VAULT_SETTING}.namespace' must be a non-empty string, got {type(namespace).__name__}."
        )
    bundle = mapping.get("ca_bundle")
    if "ca_bundle" in mapping and (not isinstance(bundle, str) or not bundle.strip()):
        # requests reads a bool here as "skip verification", which this setting must never mean.
        raise InvalidInferenceConfiguration(
            f"'{VAULT_SETTING}.ca_bundle' must be a path to a CA bundle, got {type(bundle).__name__}."
        )
    for field in ("connect_timeout", "read_timeout"):
        # Optional here, unlike the fallback: absent means the backend's own default deadline.
        if field in mapping:
            _validate_timeout(mapping, field, setting=VAULT_SETTING)
    method = mapping.get("auth_method", "proxy")
    if method not in VAULT_AUTH_METHODS:
        raise InvalidInferenceConfiguration(
            f"'{VAULT_SETTING}.auth_method' must be one of {', '.join(VAULT_AUTH_METHODS)}, got '{method}'."
        )
    return mapping


def _validate_vault_address(value: Any) -> None:
    """Reject a Vault address that carries a secret or that the read path would misassemble."""
    label = f"'{VAULT_SETTING}.address'"
    if not isinstance(value, str):
        raise InvalidInferenceConfiguration(f"{label} must be a string URL, got {type(value).__name__}.")
    for character in "?#":
        # A bare delimiter parses as an empty component, and the appended read path lands inside it.
        if character in value:
            raise InvalidInferenceConfiguration(
                f"{label} cannot contain '{character}', which would put the appended read path in "
                f"the query or fragment."
            )
    # Unquoted: the message is persisted, and an address can carry a token in its userinfo.
    parts = _split_url(value, f"{VAULT_SETTING}.address", quote_value=False)
    # The read sends a token to this address, so it follows the api_root rule for a bearer token.
    if parts.scheme.lower() != "https" and not is_local_endpoint(value):
        raise InvalidInferenceConfiguration(
            f"{label} must use https, unless it names a local endpoint, because the read sends a token to it."
        )


_UNSAFE_PATH_SEGMENTS = frozenset({"", ".", ".."})


def _require_text(value: Any, label: str) -> str:
    """Return the value as text, rejecting one that only looks valid after coercion.

    A number survives `str()` and reaches the Vault request as a path or a key name.
    """
    if not isinstance(value, str) or not value:
        raise InvalidInferenceConfiguration(f"'{label}' must be a non-empty string.")
    return value


def _validate_vault_path(value: Any, label: str, *, segments: bool) -> None:
    """Reject a Vault path value that could change the request it is interpolated into."""
    text = _require_text(value, label)
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
    unknown = sorted(str(key) for key in set(mapping) - set(CREDENTIAL_REFERENCE_FIELDS))
    if unknown:
        raise InvalidInferenceConfiguration(f"Unknown '{label}' key(s): {', '.join(unknown)}.")
    missing = [name for name in CREDENTIAL_REFERENCE_FIELDS if not mapping.get(name)]
    if missing:
        raise InvalidInferenceConfiguration(f"'{label}' is missing required key(s): {', '.join(missing)}.")
    if mapping["backend"] != CREDENTIAL_REFERENCE_BACKEND:
        # The supplied value is not quoted back: this message is persisted and may hold secret material.
        raise InvalidInferenceConfiguration(f"'{label}.backend' must be '{CREDENTIAL_REFERENCE_BACKEND}'.")
    _validate_vault_path(mapping["mount"], f"{label}.mount", segments=False)
    _validate_vault_path(mapping["path"], f"{label}.path", segments=True)
    _require_text(mapping["field"], f"{label}.field")
    return mapping


def _validate_choice(mapping: Mapping[str, Any], field: str, choices) -> None:
    """Reject a fallback value the matching InferenceBackend column would not accept."""
    allowed = [value for value, _label in choices]
    if mapping.get(field) not in allowed:
        raise InvalidInferenceConfiguration(
            f"'{FILE_FALLBACK_SETTING}.{field}' must be one of {', '.join(allowed)}, got '{mapping.get(field)}'."
        )


def _validate_text(mapping: Mapping[str, Any], field: str, max_length: int) -> None:
    """Reject fallback text the matching column could not store, or that names nothing."""
    value = mapping.get(field)
    label = f"'{FILE_FALLBACK_SETTING}.{field}'"
    if not isinstance(value, str) or not value.strip():
        raise InvalidInferenceConfiguration(f"{label} must be a non-empty string, got {value!r}.")
    if len(value) > max_length:
        raise InvalidInferenceConfiguration(f"{label} is longer than the {max_length} characters the column holds.")


def _validate_timeout(mapping: Mapping[str, Any], field: str, setting: str = FILE_FALLBACK_SETTING) -> None:
    """Reject a timeout the matching PositiveIntegerField would not accept."""
    value = mapping.get(field)
    # bool is an int subclass, and True would otherwise read as a one second timeout.
    if isinstance(value, bool) or not isinstance(value, int) or not TIMEOUT_MIN <= value <= TIMEOUT_MAX:
        raise InvalidInferenceConfiguration(
            f"'{setting}.{field}' must be a whole number of seconds between "
            f"{TIMEOUT_MIN} and {TIMEOUT_MAX}, got {value!r}."
        )


def _validate_fallback_fields(mapping: Mapping[str, Any]) -> None:
    """Apply the InferenceBackend column constraints the fallback bypasses by not being a row."""
    _validate_choice(mapping, "adapter_type", ADAPTER_TYPES)
    _validate_choice(mapping, "authentication", AUTHENTICATION_METHODS)
    _validate_choice(mapping, "response_mode", RESPONSE_MODES)
    _validate_text(mapping, "api_root", API_ROOT_MAX_LENGTH)
    _validate_text(mapping, "display_name", DISPLAY_NAME_MAX_LENGTH)
    _validate_text(mapping, "model", MODEL_MAX_LENGTH)
    _validate_timeout(mapping, "connect_timeout")
    _validate_timeout(mapping, "read_timeout")


def validate_file_fallback(value: Any, allowlist: Sequence[str]) -> Mapping[str, Any]:
    """Return the whole-backend fallback, rejecting a field set that is not exactly the row's."""
    mapping = _require_mapping(value, FILE_FALLBACK_SETTING)
    unknown = sorted(str(key) for key in set(mapping) - set(FILE_FALLBACK_FIELDS))
    if unknown:
        raise InvalidInferenceConfiguration(f"Unknown '{FILE_FALLBACK_SETTING}' key(s): {', '.join(unknown)}.")
    missing = [name for name in FILE_FALLBACK_FIELDS if name not in mapping]
    if missing:
        raise InvalidInferenceConfiguration(
            f"'{FILE_FALLBACK_SETTING}' is missing required key(s): {', '.join(missing)}."
        )
    _validate_fallback_fields(mapping)
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
