"""Signed, opaque model identities resolved from authorized metadata."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

from .settings import settings


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def scope_identity(scope: dict[str, Any]) -> dict[str, str]:
    return {
        "namespace": str(scope.get("namespace", "")),
        "deployment": str(scope.get("deployment", "")),
        "model_name": str(scope.get("model_name", "")),
        "serving_label": str(scope.get("serving_label", "")),
        "serving_value": str(scope.get("serving_value", "")),
    }


def sign_scope(scope: dict[str, Any]) -> str:
    payload = json.dumps(scope_identity(scope), sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(settings.scope_signing_key.encode("utf-8"), payload, hashlib.sha256).digest()
    return f"{_b64(payload)}.{_b64(signature)}"


def decode_scope_id(scope_id: str) -> dict[str, str]:
    try:
        encoded, encoded_signature = scope_id.split(".", 1)
        payload = _unb64(encoded)
        signature = _unb64(encoded_signature)
    except (ValueError, TypeError) as exc:
        raise ValueError("Invalid model identity.") from exc
    expected = hmac.new(settings.scope_signing_key.encode("utf-8"), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError("Invalid model identity signature.")
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError("Invalid model identity payload.") from exc
    if not isinstance(value, dict) or not all(isinstance(item, str) for item in value.values()):
        raise ValueError("Invalid model identity fields.")
    if not value.get("namespace") or not value.get("model_name") or not value.get("deployment"):
        raise ValueError("Model identity is incomplete.")
    return value
