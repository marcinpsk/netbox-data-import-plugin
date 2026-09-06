# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Resolve the active Inference Backend (specification 8.2).

The enabled database row is the active backend. The `inference_backend` setting is the whole-backend
fallback and acts only when no enabled row exists. The two sources are never merged field by field,
so a resolved backend always names the one source it came from.
"""

from dataclasses import dataclass
from typing import Any

from django.core.exceptions import ValidationError

from .inference_credentials import CredentialReference
from .inference_settings import (
    FILE_FALLBACK_KEY,
    FILE_FALLBACK_SETTING,
    ORIGIN_ALLOWLIST_SETTING,
    validate_credential_reference,
    validate_file_fallback,
)
from .inference_trust import InvalidInferenceConfiguration, validate_api_root

PLUGIN_NAME = "netbox_data_import"

SOURCE_DATABASE = "database"
SOURCE_FILE_FALLBACK = "file-fallback"


class NoActiveInferenceBackend(Exception):
    """No enabled database row exists and no file fallback is configured."""


def plugin_settings() -> dict[str, Any]:
    """Return this plugin's PLUGINS_CONFIG entry."""
    from django.conf import settings

    return dict(settings.PLUGINS_CONFIG.get(PLUGIN_NAME, {}))


def origin_allowlist() -> tuple[str, ...]:
    """Return the deployment's approved origins."""
    return tuple(plugin_settings().get(ORIGIN_ALLOWLIST_SETTING, ()))


def validate_backend_fields(api_root: str, authentication: str, credential_reference: Any) -> None:
    """Reject backend fields the deployment may not use, as a Django field-keyed ValidationError."""
    try:
        validate_credential_reference(credential_reference)
    except InvalidInferenceConfiguration as exc:
        raise ValidationError({"credential_reference": str(exc)}) from exc
    try:
        validate_api_root(api_root, allowlist=origin_allowlist(), authentication=authentication)
    except InvalidInferenceConfiguration as exc:
        raise ValidationError({"api_root": str(exc)}) from exc


@dataclass(frozen=True)
class ResolvedInferenceBackend:
    """One whole backend, from exactly one source."""

    backend_key: str
    display_name: str
    adapter_type: str
    api_root: str
    model: str
    authentication: str
    response_mode: str
    credential_reference: CredentialReference
    connect_timeout: int
    read_timeout: int
    source: str

    def metadata(self) -> dict[str, str]:
        """Return the backend metadata a job may record: no credential reference, no secret."""
        return {
            "backend_key": self.backend_key,
            "backend_source": self.source,
            "backend_adapter_type": self.adapter_type,
            "backend_model": self.model,
        }


def _from_row(row, allowlist) -> ResolvedInferenceBackend:
    """Return the resolved backend one enabled database row describes."""
    # A saved row outlives the allowlist that approved it, so spec 8.3 validates both sources alike.
    validate_api_root(row.api_root, allowlist=allowlist, authentication=row.authentication)
    return ResolvedInferenceBackend(
        backend_key=row.backend_key,
        display_name=row.display_name,
        adapter_type=row.adapter_type,
        api_root=row.api_root,
        model=row.model,
        authentication=row.authentication,
        response_mode=row.response_mode,
        credential_reference=CredentialReference.from_mapping(row.credential_reference),
        connect_timeout=row.connect_timeout,
        read_timeout=row.read_timeout,
        source=SOURCE_DATABASE,
    )


def _from_file_fallback(mapping, allowlist) -> ResolvedInferenceBackend:
    """Return the resolved backend the file fallback describes, under its fixed key."""
    validated = validate_file_fallback(mapping, allowlist)
    return ResolvedInferenceBackend(
        backend_key=FILE_FALLBACK_KEY,
        display_name=validated["display_name"],
        adapter_type=validated["adapter_type"],
        api_root=validated["api_root"],
        model=validated["model"],
        authentication=validated["authentication"],
        response_mode=validated["response_mode"],
        credential_reference=CredentialReference.from_mapping(validated["credential_reference"]),
        connect_timeout=validated["connect_timeout"],
        read_timeout=validated["read_timeout"],
        source=SOURCE_FILE_FALLBACK,
    )


def resolve_active_backend() -> ResolvedInferenceBackend:
    """Return the active backend, naming the one source it came from."""
    from .models import InferenceBackend

    row = InferenceBackend.objects.filter(enabled=True).first()
    if row is not None:
        return _from_row(row, origin_allowlist())
    config = plugin_settings()
    if FILE_FALLBACK_SETTING not in config:
        raise NoActiveInferenceBackend(
            "No Inference Backend row is enabled and no 'inference_backend' fallback is configured."
        )
    return _from_file_fallback(config[FILE_FALLBACK_SETTING], config.get(ORIGIN_ALLOWLIST_SETTING, ()))


__all__ = (
    "NoActiveInferenceBackend",
    "ResolvedInferenceBackend",
    "SOURCE_DATABASE",
    "SOURCE_FILE_FALLBACK",
    "origin_allowlist",
    "plugin_settings",
    "resolve_active_backend",
    "validate_backend_fields",
)
