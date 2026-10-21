"""Credential resolution and secret masking."""

from __future__ import annotations

import logging
import os
import re

_ENV_REF = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")
_registered: set[str] = set()
_KEY_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)(api[_-]?key|password|passwd|secret|token)(\"?\s*[:=]\s*\"?)([^\s\"',]+)"),
]


class MissingCredential(Exception):
    pass


def resolve_env_refs(value: str) -> str:
    """Replace ${NAME} with the environment variable's value; the resolved value is registered for masking."""

    def sub(m: re.Match[str]) -> str:
        name = m.group(1)
        v = os.environ.get(name)
        if v is None:
            raise MissingCredential(f"environment variable {name} is not set")
        register_secret(v)
        return v

    return _ENV_REF.sub(sub, value)


def register_secret(value: str) -> None:
    if value and len(value) >= 4:
        _registered.add(value)


def mask(text: str) -> str:
    if not text:
        return text
    for s in sorted(_registered, key=len, reverse=True):
        text = text.replace(s, "****")
    text = _KEY_PATTERNS[0].sub("sk-ant-****", text)
    return _KEY_PATTERNS[1].sub(lambda m: f"{m.group(1)}{m.group(2)}****", text)


class SecretMaskingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = mask(str(record.getMessage()))
        record.args = ()
        return True
