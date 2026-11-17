"""凭证脱敏与不可信输入隔离工具（用于持久化前与模型网络请求前）。"""

from __future__ import annotations

import re

_REDACTION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"),
        "[REDACTED_PRIVATE_KEY]",
    ),
    (
        re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{12,}"),
        "Bearer [REDACTED_TOKEN]",
    ),
    (
        re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"),
        "[REDACTED_API_KEY]",
    ),
    (
        re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"),
        "[REDACTED_GH_TOKEN]",
    ),
    (
        re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
        "[REDACTED_GH_TOKEN]",
    ),
    (
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        "[REDACTED_AWS_KEY]",
    ),
    (
        re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"),
        "[REDACTED_SLACK_TOKEN]",
    ),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|apikey|secret|password|passwd|access_token|auth_token|token)\b"
            r"(\s*[:=]\s*['\"]?)([^\s'\",;]{8,})(['\"]?)"
        ),
        r"\1\2[REDACTED]\4",
    ),
)


def redact_secrets(text: str) -> str:
    """尽最大努力脱敏可识别的凭证模式（不保证覆盖任意自定义秘密）。"""
    if not text:
        return text
    result = text
    for pattern, replacement in _REDACTION_PATTERNS:
        result = pattern.sub(replacement, result)
    return result
