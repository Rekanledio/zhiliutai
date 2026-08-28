from __future__ import annotations

import re
from collections.abc import Mapping


REDACTED = "[REDACTED]"

_ASSIGNED_SECRET = re.compile(
    r"(?i)(?<![A-Za-z0-9_-])(?:api[_ -]?key|authorization|bearer|access[_ -]?token|"
    r"refresh[_ -]?token|password|secret|token|cookie|set[-_ ]?cookie)"
    r"(?![A-Za-z0-9_-])\s*[\"']?\s*[:=]"
    r"\s*[\"']?\s*(?:bearer\s+)?[^\s,;\]}\"']+"
)
_BEARER_TOKEN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
_PROVIDER_KEY = re.compile(r"(?i)\b(?:sk|rk|pk)-[A-Za-z0-9][A-Za-z0-9._-]{6,}\b")
_QUOTED_WINDOWS_ABSOLUTE = re.compile(
    r'''(?i)(["'])(?:[A-Za-z]:[\\/]|\\\\)[^"'<>\r\n]*\1'''
)
_WINDOWS_ABSOLUTE = re.compile(
    r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|\\\\)[^\r\n\"'<>;,\]}]+"
)
_UNIX_ABSOLUTE = re.compile(
    r"(?<![A-Za-z0-9])/(?:Users|home|tmp|var|mnt|data|workspace|private)/[^\r\n\"'<>;,\]}]+"
)

_SENSITIVE_ERROR_KEYS = {
    "input",
    "raw_input",
    "request_body",
    "body",
    "response",
    "response_body",
    "upstream_response",
    "headers",
    "authorization",
    "api_key",
    "apikey",
    "token",
    "secret",
    "password",
    "cookie",
    "cookies",
    "set_cookie",
}


def redact_sensitive_text(value: str) -> str:
    """Redact credentials and absolute local paths from public text."""

    redacted = _ASSIGNED_SECRET.sub(REDACTED, value)
    redacted = _BEARER_TOKEN.sub(REDACTED, redacted)
    redacted = _PROVIDER_KEY.sub(REDACTED, redacted)
    redacted = _QUOTED_WINDOWS_ABSOLUTE.sub(
        lambda match: match.group(1) + REDACTED + match.group(1), redacted
    )
    redacted = _WINDOWS_ABSOLUTE.sub(REDACTED, redacted)
    return _UNIX_ABSOLUTE.sub(REDACTED, redacted)


def redact_sensitive_value(value: object) -> object:
    """Recursively redact values before they enter an API/audit payload."""

    if isinstance(value, str):
        return redact_sensitive_text(value)
    if isinstance(value, Mapping):
        return {
            key: REDACTED
            if isinstance(key, str)
            and key.casefold().replace("-", "_") in _SENSITIVE_ERROR_KEYS
            else redact_sensitive_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive_value(item) for item in value)
    if isinstance(value, bytes):
        return REDACTED
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_sensitive_text(str(value))


def redact_error_details(value: object) -> object:
    """Make validation details safe without echoing the rejected raw input."""

    if isinstance(value, Mapping):
        result: dict[object, object] = {}
        for key, item in value.items():
            normalized = key.casefold().replace("-", "_") if isinstance(key, str) else ""
            if normalized in _SENSITIVE_ERROR_KEYS:
                continue
            result[key] = redact_error_details(item)
        return result
    if isinstance(value, list):
        return [redact_error_details(item) for item in value]
    if isinstance(value, tuple):
        return [redact_error_details(item) for item in value]
    if isinstance(value, str):
        return redact_sensitive_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_sensitive_text(str(value))
