"""PII redaction.

Applied at two boundaries:
  1. before anything is written to a log or audit record;
  2. before untrusted text is handed to an LLM (when ``llm_redact_prompts``).

The goal is not perfect DLP, it is making the common leaks (cards, secrets,
government ids) structurally hard.
"""

from __future__ import annotations

import re
from typing import Any

_EMAIL = re.compile(r"\b[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
_PHONE = re.compile(r"(?<!\d)(?:\+?\d{1,3}[\s.-]?)?(?:\(?\d{3}\)?[\s.-]?)\d{3}[\s.-]?\d{4}(?!\d)")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_CARD_CANDIDATE = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b")
_BEARER = re.compile(r"\b[Bb]earer\s+[A-Za-z0-9._\-]{12,}")
_SECRET = re.compile(
    r"\b(?:sk|pk|rk|ghp|gho|github_pat|xox[abposr]|AKIA|ASIA)[-_A-Za-z0-9]{10,}\b"
)
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S
)

SENSITIVE_KEYS = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "api_key",
        "apikey",
        "authorization",
        "card_number",
        "cvv",
        "ssn",
        "provider_token",
        "reset_token",
    }
)

MASK = "[REDACTED]"


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = ord(ch) - 48
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def _mask_email(m: re.Match[str]) -> str:
    local, _, domain = m.group(0).partition("@")
    head = local[0] if local else "*"
    return f"{head}{'*' * max(len(local) - 1, 1)}@{domain}"


def _mask_card(m: re.Match[str]) -> str:
    raw = m.group(0)
    digits = re.sub(r"\D", "", raw)
    if not (13 <= len(digits) <= 19) or not _luhn_ok(digits):
        return raw  # order numbers / ids should survive untouched
    return f"[CARD_****{digits[-4:]}]"


def redact_text(text: str) -> str:
    """Mask sensitive substrings in free text."""
    if not text:
        return text
    out = _PRIVATE_KEY.sub("[PRIVATE_KEY]", text)
    out = _SECRET.sub(MASK, out)
    out = _BEARER.sub("Bearer " + MASK, out)
    out = _CARD_CANDIDATE.sub(_mask_card, out)
    out = _IBAN.sub("[IBAN]", out)
    out = _SSN.sub("[SSN]", out)
    out = _PHONE.sub("[PHONE]", out)
    out = _EMAIL.sub(_mask_email, out)
    return out


def redact(value: Any) -> Any:
    """Recursively redact a JSON-ish structure.

    Keys named in :data:`SENSITIVE_KEYS` are dropped wholesale rather than
    pattern-matched, because a short password would otherwise slip through.
    """
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {
            k: (MASK if str(k).lower() in SENSITIVE_KEYS else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value
