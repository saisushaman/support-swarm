"""Intent taxonomy, deterministic classification, and parameter extraction.

The keyword classifier is not a toy fallback -- it is the *reference*
implementation. When ``SWARM_LLM_ENABLED=true`` the Supervisor asks a model
instead, but the model's answer is validated against this same taxonomy, and
this classifier is what CI exercises. Determinism where it is cheap, models
where they add something.

Which intents may be resolved autonomously at all is declared here
(:data:`AUTONOMOUS_INTENTS`) rather than inferred at runtime: anything touching
subscription revenue or a suspected product bug goes to a human by design, not
because the classifier happened to be unsure.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum


class Intent(StrEnum):
    PASSWORD_RESET = "password_reset"
    BILLING_REFUND = "billing_refund"
    INVOICE_QUESTION = "invoice_question"
    ACCOUNT_ACCESS = "account_access"
    FEATURE_ACCESS = "feature_access"
    SUBSCRIPTION_CHANGE = "subscription_change"
    BUG_REPORT = "bug_report"
    UNKNOWN = "unknown"


#: Intents the swarm is permitted to close without a human in the loop.
AUTONOMOUS_INTENTS = frozenset(
    {
        Intent.PASSWORD_RESET,
        Intent.BILLING_REFUND,
        Intent.INVOICE_QUESTION,
        Intent.ACCOUNT_ACCESS,
        Intent.FEATURE_ACCESS,
    }
)

#: Which features each plan is allowed to have switched on autonomously.
#: A request outside this matrix is an upsell decision, so it goes to a human.
PLAN_FEATURES: dict[str, frozenset[str]] = {
    "free": frozenset({"dark_mode", "csv_export"}),
    "pro": frozenset({"dark_mode", "csv_export", "api_access", "sso", "audit_log"}),
    "enterprise": frozenset(
        {"dark_mode", "csv_export", "api_access", "sso", "audit_log", "custom_roles", "hipaa_mode"}
    ),
}

FEATURE_ALIASES: dict[str, str] = {
    "dark mode": "dark_mode",
    "csv export": "csv_export",
    "export to csv": "csv_export",
    "api access": "api_access",
    "api key": "api_access",
    "api keys": "api_access",
    "single sign on": "sso",
    "single sign-on": "sso",
    "sso": "sso",
    "saml": "sso",
    "audit log": "audit_log",
    "audit logs": "audit_log",
    "custom roles": "custom_roles",
    "hipaa": "hipaa_mode",
}

# (intent, weight, pattern)
_RULES: tuple[tuple[Intent, int, re.Pattern[str]], ...] = (
    (Intent.PASSWORD_RESET, 5, re.compile(r"\b(?:reset|forgot|forgotten|change)\b[^.\n]{0,20}\bpassword\b", re.I)),
    (Intent.PASSWORD_RESET, 4, re.compile(r"\bpassword\s+(?:reset|recovery)\b", re.I)),
    (Intent.PASSWORD_RESET, 3, re.compile(r"\bcan'?t\s+(?:remember|recall)\b[^.\n]{0,20}\bpassword\b", re.I)),
    (Intent.ACCOUNT_ACCESS, 5, re.compile(r"\b(?:locked out|lock(?:ed)? me out|can'?t (?:log ?in|sign ?in|access my account))\b", re.I)),
    (Intent.ACCOUNT_ACCESS, 4, re.compile(r"\b(?:account (?:compromised|hacked|breached)|someone (?:else )?(?:is )?(?:using|accessed))\b", re.I)),
    (Intent.ACCOUNT_ACCESS, 3, re.compile(r"\b(?:suspicious|unauthori[sz]ed)\b[^.\n]{0,20}\b(?:login|sign ?in|activity|access)\b", re.I)),
    (Intent.BILLING_REFUND, 5, re.compile(r"\brefund(?:ed|ing)?\b", re.I)),
    (Intent.BILLING_REFUND, 4, re.compile(r"\b(?:money back|charge ?back|reverse the charge|double[- ]?charged|charged twice)\b", re.I)),
    (Intent.BILLING_REFUND, 3, re.compile(r"\b(?:overcharged|billed twice|wrong amount)\b", re.I)),
    (Intent.INVOICE_QUESTION, 4, re.compile(r"\b(?:invoice|receipt|statement)\b[^.\n]{0,30}\b(?:question|explain|breakdown|copy|understand|what is)\b", re.I)),
    (Intent.INVOICE_QUESTION, 3, re.compile(r"\b(?:what (?:is|was) (?:this|the) charge|why was i (?:charged|billed))\b", re.I)),
    (Intent.INVOICE_QUESTION, 2, re.compile(r"\b(?:send|resend)\b[^.\n]{0,20}\b(?:invoice|receipt)\b", re.I)),
    (Intent.FEATURE_ACCESS, 4, re.compile(r"\b(?:enable|turn on|activate|switch on|give me access to|unlock)\b", re.I)),
    (Intent.FEATURE_ACCESS, 3, re.compile(r"\b(?:can'?t (?:see|find|use)|missing|no access to)\b[^.\n]{0,30}\b(?:feature|option|setting|api|sso|export)\b", re.I)),
    (Intent.SUBSCRIPTION_CHANGE, 5, re.compile(r"\b(?:cancel|downgrade|upgrade|terminate)\b[^.\n]{0,25}\b(?:subscription|plan|account|contract)\b", re.I)),
    (Intent.SUBSCRIPTION_CHANGE, 4, re.compile(r"\b(?:cancel my (?:subscription|plan|account)|switch to (?:the )?(?:free|pro|enterprise))\b", re.I)),
    (Intent.SUBSCRIPTION_CHANGE, 3, re.compile(r"\b(?:add|remove)\b[^.\n]{0,15}\bseats?\b", re.I)),
    (Intent.BUG_REPORT, 4, re.compile(r"\b(?:error|exception|stack ?trace|500|crash(?:ing|ed|es)?|broken|not working|bug)\b", re.I)),
    (Intent.BUG_REPORT, 3, re.compile(r"\b(?:data (?:loss|missing|corrupted)|wrong (?:results|numbers|totals))\b", re.I)),
)

#: Tie-break order, most specific first. Ties are common -- "can't log in, I
#: forgot my password" legitimately matches two intents -- and breaking them
#: alphabetically would let a broad catch-all beat an explicit request. Narrow,
#: explicitly-stated intents win; ACCOUNT_ACCESS and BUG_REPORT are the broad
#: buckets and go last.
_TIE_PRIORITY: tuple[Intent, ...] = (
    Intent.PASSWORD_RESET,
    Intent.BILLING_REFUND,
    Intent.SUBSCRIPTION_CHANGE,
    Intent.FEATURE_ACCESS,
    Intent.INVOICE_QUESTION,
    Intent.ACCOUNT_ACCESS,
    Intent.BUG_REPORT,
    Intent.UNKNOWN,
)

_AMOUNT = re.compile(r"\$\s?(\d{1,7}(?:,\d{3})*(?:\.\d{1,2})?)|\b(\d{1,7}(?:\.\d{1,2})?)\s*(?:usd|dollars?)\b", re.I)
_INVOICE_ID = re.compile(r"\b(inv_[A-Za-z0-9]{4,})\b")
_INVOICE_HASH = re.compile(r"\binvoice\s*#?\s*([A-Za-z0-9_-]{4,})\b", re.I)


@dataclass
class Classification:
    intent: Intent
    confidence: float
    scores: dict[str, int] = field(default_factory=dict)
    requested_amount_cents: int | None = None
    invoice_reference: str | None = None
    feature_key: str | None = None
    reasoning: str = ""

    @property
    def autonomous(self) -> bool:
        return self.intent in AUTONOMOUS_INTENTS

    def as_dict(self) -> dict:
        return {
            "intent": self.intent.value,
            "confidence": round(self.confidence, 3),
            "scores": self.scores,
            "requested_amount_cents": self.requested_amount_cents,
            "invoice_reference": self.invoice_reference,
            "feature_key": self.feature_key,
            "reasoning": self.reasoning,
            "autonomous": self.autonomous,
        }


def extract_amount_cents(text: str) -> int | None:
    """Pull a monetary amount out of free text, in cents."""
    match = _AMOUNT.search(text or "")
    if match is None:
        return None
    raw = (match.group(1) or match.group(2) or "").replace(",", "")
    if not raw:
        return None
    try:
        return round(float(raw) * 100)
    except ValueError:
        return None


def extract_invoice_reference(text: str) -> str | None:
    match = _INVOICE_ID.search(text or "")
    if match:
        return match.group(1)
    match = _INVOICE_HASH.search(text or "")
    return match.group(1) if match else None


def extract_feature_key(text: str) -> str | None:
    """Map a feature mentioned in prose onto a canonical entitlement key."""
    lowered = (text or "").lower()
    # Longest alias first so "single sign-on" beats a bare "sso" substring.
    for alias in sorted(FEATURE_ALIASES, key=len, reverse=True):
        if alias in lowered:
            return FEATURE_ALIASES[alias]
    for key in {k for keys in PLAN_FEATURES.values() for k in keys}:
        if key in lowered:
            return key
    return None


def classify(subject: str, body: str) -> Classification:
    """Score every intent against the ticket and take the winner."""
    text = f"{subject or ''}\n{body or ''}"
    scores: dict[Intent, int] = {}
    for intent, weight, pattern in _RULES:
        if pattern.search(text):
            scores[intent] = scores.get(intent, 0) + weight

    if not scores:
        return Classification(
            intent=Intent.UNKNOWN,
            confidence=0.0,
            reasoning="no intent signal matched",
        )

    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], _TIE_PRIORITY.index(kv[0])))
    best, best_score = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0
    total = sum(scores.values())
    # Confidence reflects both absolute signal and margin over the alternative.
    margin = (best_score - runner_up) / best_score if best_score else 0.0
    confidence = round(min(0.99, 0.45 * (best_score / total) + 0.35 * margin + 0.2), 3)

    return Classification(
        intent=best,
        confidence=confidence,
        scores={k.value: v for k, v in scores.items()},
        requested_amount_cents=extract_amount_cents(text),
        invoice_reference=extract_invoice_reference(text),
        feature_key=extract_feature_key(text),
        reasoning=f"matched {best.value} with score {best_score} of {total}",
    )


def coerce_intent(value: str | None) -> Intent:
    """Validate a model-supplied intent string against the taxonomy."""
    if not value:
        return Intent.UNKNOWN
    try:
        return Intent(value.strip().lower())
    except ValueError:
        return Intent.UNKNOWN


def plan_allows_feature(plan: str, feature_key: str) -> bool:
    return feature_key in PLAN_FEATURES.get((plan or "free").lower(), frozenset())
