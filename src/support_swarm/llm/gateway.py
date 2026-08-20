"""LLM access, funnelled through LiteLLM.

One choke point for every model call in the system. That buys us:

* per-role model routing (a cheap model triages, a strong one supervises);
* uniform timeouts, bounded retries and token accounting;
* prompt redaction, so PII does not leave the trust boundary by accident;
* a :class:`StubGateway` so the whole swarm is testable with no network.

LiteLLM itself is an optional dependency -- imported lazily -- so the base
install stays small and CI needs no provider credentials.
"""

from __future__ import annotations

import asyncio
import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

from ..config import get_settings
from ..core.logging import get_logger
from ..core.redaction import redact_text

log = get_logger(__name__)

Role = Literal["supervisor", "worker", "qa"]


@dataclass
class LLMResponse:
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def json(self, *, default: Any = None) -> Any:
        """Parse the response as JSON, tolerating fenced code blocks."""
        return extract_json(self.text, default=default)


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json(text: str, *, default: Any = None) -> Any:
    """Best-effort JSON extraction from model prose.

    Models wrap JSON in prose or fences more often than not; refusing to parse
    that is a reliability bug, not strictness.
    """
    if not text:
        return default
    candidates: list[str] = []
    fenced = _FENCE.search(text)
    if fenced:
        candidates.append(fenced.group(1))
    candidates.append(text)
    for chunk in candidates:
        chunk = chunk.strip()
        try:
            return json.loads(chunk)
        except (ValueError, TypeError):
            pass
        start, end = chunk.find("{"), chunk.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(chunk[start : end + 1])
            except (ValueError, TypeError):
                continue
    return default


class LLMGateway(ABC):
    @abstractmethod
    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        role: Role = "worker",
        max_tokens: int | None = None,
        temperature: float = 0.0,
    ) -> LLMResponse: ...

    async def aclose(self) -> None:  # pragma: no cover - trivial
        return None


class StubGateway(LLMGateway):
    """Deterministic gateway.

    Either pops scripted replies off a queue (tests) or echoes a structured
    refusal-safe default. Never makes a network call.
    """

    def __init__(self, scripted: list[str] | None = None, *, default: str = "{}") -> None:
        self.scripted = list(scripted or [])
        self.default = default
        self.calls: list[list[dict[str, str]]] = []

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        role: Role = "worker",
        max_tokens: int | None = None,
        temperature: float = 0.0,
    ) -> LLMResponse:
        self.calls.append([dict(m) for m in messages])
        text = self.scripted.pop(0) if self.scripted else self.default
        return LLMResponse(
            text=text,
            model=f"stub-{role}",
            prompt_tokens=sum(len(m.get("content", "")) // 4 for m in messages),
            completion_tokens=len(text) // 4,
        )


class LiteLLMGateway(LLMGateway):
    """Production gateway. Retries only on transient errors."""

    def __init__(self) -> None:
        try:
            import litellm  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - depends on extras
            raise RuntimeError(
                "SWARM_LLM_ENABLED=true requires LiteLLM. "
                "Install with: pip install 'support-swarm[llm]'"
            ) from exc
        litellm.drop_params = True
        litellm.suppress_debug_info = True
        self._litellm = litellm
        self.usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0}

    def _model_for(self, role: Role) -> str:
        s = get_settings()
        return {
            "supervisor": s.llm_model_supervisor,
            "worker": s.llm_model_worker,
            "qa": s.llm_model_qa,
        }[role]

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        role: Role = "worker",
        max_tokens: int | None = None,
        temperature: float = 0.0,
    ) -> LLMResponse:
        s = get_settings()
        model = self._model_for(role)
        payload = messages
        if s.llm_redact_prompts:
            payload = [
                {**m, "content": redact_text(m.get("content", ""))} for m in messages
            ]

        last_error: Exception | None = None
        for attempt in range(1, s.llm_max_retries + 2):
            try:
                response = await self._litellm.acompletion(
                    model=model,
                    messages=payload,
                    temperature=temperature,
                    max_tokens=max_tokens or s.llm_max_output_tokens,
                    timeout=s.llm_timeout_seconds,
                )
                return self._to_response(response, model)
            except Exception as exc:  # LiteLLM raises provider-specific types
                last_error = exc
                if not self._is_transient(exc) or attempt > s.llm_max_retries:
                    break
                await asyncio.sleep(0.5 * 2 ** (attempt - 1))

        log.error(
            "llm call failed",
            extra={"model": model, "role": role, "error": str(last_error)},
        )
        raise RuntimeError(f"llm_call_failed: {last_error!s}") from last_error

    @staticmethod
    def _is_transient(exc: Exception) -> bool:
        name = type(exc).__name__.lower()
        text = str(exc).lower()
        transient_markers = ("timeout", "ratelimit", "rate limit", "overloaded",
                            "503", "502", "500", "apiconnection", "internalserver")
        return any(m in name or m in text for m in transient_markers)

    def _to_response(self, response: Any, model: str) -> LLMResponse:
        try:
            text = response.choices[0].message.content or ""
        except (AttributeError, IndexError, KeyError):
            text = ""
        usage = getattr(response, "usage", None)
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        self.usage["prompt_tokens"] += prompt_tokens
        self.usage["completion_tokens"] += completion_tokens
        return LLMResponse(
            text=text,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )


_gateway: LLMGateway | None = None


def build_gateway() -> LLMGateway:
    return LiteLLMGateway() if get_settings().llm_enabled else StubGateway()


def get_gateway() -> LLMGateway:
    global _gateway
    if _gateway is None:
        _gateway = build_gateway()
    return _gateway


def set_gateway(gateway: LLMGateway | None) -> None:
    """Test hook / DI override."""
    global _gateway
    _gateway = gateway
