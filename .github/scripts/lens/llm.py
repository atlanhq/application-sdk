"""The one way lens talks to a model: LiteLLM's OpenAI-compatible
`/chat/completions`, with the spend cap enforced here in code.

Every call is priced BEFORE it is sent (prompt estimate × input price +
`max_tokens` × output price) and refused if it could take the run past its
budget; the actual cost LiteLLM reports is then booked. A prompt cannot talk
its way past this, and neither can a model that keeps calling tools.

Retries are for transport weather only — 5xx, a rate-limit 429, a timeout —
and are bounded. A budget-exceeded 429 is deterministic and is never retried:
the September incident that re-dispatched into a spent key, hour after hour,
is the case this exists to make impossible.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any


class LLMError(RuntimeError):
    """A broken request or an exhausted retry budget."""


class BudgetExhausted(LLMError):
    """This run (or the gateway key) cannot afford the next call."""


def estimate_tokens(text: str) -> int:
    # ~3.5 chars/token for code-heavy English; deliberately pessimistic so the
    # pre-call estimate errs toward refusing, not overspending.
    return int(len(text) / 3.5) + 1


@dataclass
class Price:
    input_per_mtok: float
    cached_input_per_mtok: float
    output_per_mtok: float


@dataclass
class Ledger:
    """Spend for one PR across every call in every round. Serialised into the
    PR's state marker so round N sees what rounds 1..N-1 already spent."""

    cap_usd: float
    spent_usd: float = 0.0
    reserved_usd: float = 0.0
    calls: int = 0
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    by_stage: dict[str, float] = field(default_factory=dict)

    _lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False
    )

    @property
    def remaining(self) -> float:
        return max(self.cap_usd - self.spent_usd - self.reserved_usd, 0.0)

    def reserve(self, amount: float) -> bool:
        """Hold `amount` against the cap for an in-flight call. Concurrent
        bundles each reserve their worst case first, so together they can
        never overshoot the cap (open-code-review allows an overrun of up to
        its concurrency; lens does not)."""
        with self._lock:
            if amount > self.cap_usd - self.spent_usd - self.reserved_usd:
                return False
            self.reserved_usd += amount
            return True

    def release(self, amount: float) -> None:
        with self._lock:
            self.reserved_usd = max(self.reserved_usd - amount, 0.0)

    def book(self, stage: str, cost: float, usage: dict[str, Any]) -> None:
        with self._lock:
            self._book(stage, cost, usage)

    def _book(self, stage: str, cost: float, usage: dict[str, Any]) -> None:
        self.spent_usd += cost
        self.calls += 1
        self.input_tokens += int(usage.get("prompt_tokens") or 0)
        self.output_tokens += int(usage.get("completion_tokens") or 0)
        details = usage.get("prompt_tokens_details") or {}
        self.cached_tokens += int(details.get("cached_tokens") or 0)
        self.by_stage[stage] = self.by_stage.get(stage, 0.0) + cost

    @property
    def cache_hit_rate(self) -> float:
        return self.cached_tokens / self.input_tokens if self.input_tokens else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "cap_usd": self.cap_usd,
            "spent_usd": round(self.spent_usd, 6),
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "cached_tokens": self.cached_tokens,
            "output_tokens": self.output_tokens,
            "by_stage": {k: round(v, 6) for k, v in self.by_stage.items()},
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any], cap_usd: float) -> "Ledger":
        return cls(
            cap_usd=cap_usd,
            spent_usd=float(d.get("spent_usd", 0.0)),
            calls=int(d.get("calls", 0)),
            input_tokens=int(d.get("input_tokens", 0)),
            cached_tokens=int(d.get("cached_tokens", 0)),
            output_tokens=int(d.get("output_tokens", 0)),
            by_stage=dict(d.get("by_stage", {})),
        )


@dataclass
class Completion:
    content: str
    tool_calls: list[dict[str, Any]]
    usage: dict[str, Any]
    cost: float
    finish_reason: str
    message: dict[str, Any]


_BUDGET_MARKERS = (
    "budget",
    "spend cap",
    "exceeded your",
    "max_budget",
    "insufficient_quota",
)


def _is_budget_error(body: str) -> bool:
    low = body.lower()
    return any(m in low for m in _BUDGET_MARKERS)


class Client:
    def __init__(
        self,
        *,
        model: str,
        price: Price,
        ledger: Ledger,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout_s: float = 120.0,
        max_retries: int = 2,
        reasoning_effort: str | None = None,
        transport: Any = None,
    ) -> None:
        self.model = model
        self.price = price
        self.ledger = ledger
        self.base_url = (base_url or os.environ.get("LITELLM_BASE_URL") or "").rstrip(
            "/"
        )
        self.api_key = (
            api_key
            or os.environ.get("LENS_LITELLM_KEY")
            or os.environ.get("LITELLM_API_KEY")
            or ""
        )
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.reasoning_effort = reasoning_effort
        self._transport = transport or self._http
        self.send_cache_key = True

    # ---- pricing -------------------------------------------------------
    def worst_case_cost(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        max_tokens: int,
    ) -> float:
        prompt = json.dumps(messages) + (json.dumps(tools) if tools else "")
        return (
            estimate_tokens(prompt) * self.price.input_per_mtok
            + max_tokens * self.price.output_per_mtok
        ) / 1e6

    def actual_cost(self, usage: dict[str, Any], reported: float | None) -> float:
        if reported is not None and reported > 0:
            return reported
        prompt = int(usage.get("prompt_tokens") or 0)
        cached = int(
            (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
        )
        out = int(usage.get("completion_tokens") or 0)
        return (
            (prompt - cached) * self.price.input_per_mtok
            + cached * self.price.cached_input_per_mtok
            + out * self.price.output_per_mtok
        ) / 1e6

    # ---- transport -----------------------------------------------------
    def _http(self, body: dict[str, Any]) -> tuple[int, dict[str, str], str]:
        if not self.base_url or not self.api_key:
            raise LLMError(
                "LiteLLM is not configured: set LITELLM_BASE_URL and LENS_LITELLM_KEY."
            )
        url = self.base_url + (
            "/chat/completions"
            if self.base_url.endswith("/v1")
            else "/v1/chat/completions"
        )
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:  # noqa: S310 - fixed https base from config
                return (
                    resp.status,
                    dict(resp.headers),
                    resp.read().decode("utf-8", "replace"),
                )
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers or {}), e.read().decode("utf-8", "replace")

    # ---- the call ------------------------------------------------------
    def complete(
        self,
        stage: str,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int,
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
        temperature: float
        | None = None,  # omitted: reasoning models reject non-default values
        tool_choice: Any = "auto",
        cache_key: str | None = None,
    ) -> Completion:
        worst = self.worst_case_cost(messages, tools, max_tokens)
        if not self.ledger.reserve(worst):
            raise BudgetExhausted(
                f"{stage}: worst case ${worst:.4f} exceeds the ${self.ledger.remaining:.4f} left of the ${self.ledger.cap_usd:.2f} cap"
            )
        try:
            return self._complete(
                stage,
                messages,
                max_tokens,
                tools,
                response_format,
                temperature,
                tool_choice,
                cache_key,
            )
        finally:
            self.ledger.release(worst)

    def _complete(
        self,
        stage,
        messages,
        max_tokens,
        tools,
        response_format,
        temperature,
        tool_choice,
        cache_key,
    ) -> Completion:  # noqa: ANN001
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if temperature is not None:
            body["temperature"] = temperature
        if tools:
            body["tools"] = tools
            body["tool_choice"] = tool_choice
        if response_format:
            body["response_format"] = response_format
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        if cache_key and self.send_cache_key:
            # Routes requests sharing a prefix to the same cache (OpenAI `prompt_cache_key`).
            body["prompt_cache_key"] = cache_key

        last = ""
        for attempt in range(self.max_retries + 1):
            try:
                status, headers, text = self._transport(body)
            except (TimeoutError, urllib.error.URLError, OSError) as e:
                status, headers, text = 0, {}, f"transport: {e}"
            if status == 200:
                data = json.loads(text)
                choice = (data.get("choices") or [{}])[0]
                msg = choice.get("message") or {}
                usage = data.get("usage") or {}
                reported = None
                for k, v in headers.items():
                    if k.lower() == "x-litellm-response-cost":
                        try:
                            reported = float(v)
                        except ValueError:
                            pass
                cost = self.actual_cost(usage, reported)
                self.ledger.book(stage, cost, usage)
                return Completion(
                    content=msg.get("content") or "",
                    tool_calls=msg.get("tool_calls") or [],
                    usage=usage,
                    cost=cost,
                    finish_reason=choice.get("finish_reason") or "",
                    message=msg,
                )
            last = f"HTTP {status}: {text[:300]}"
            if (
                status == 400
                and "prompt_cache_key" in body
                and "prompt_cache_key" in text
            ):
                # A gateway that does not pass the parameter through: drop it for the rest of the run.
                self.send_cache_key = False
                body.pop("prompt_cache_key")
                continue
            if status in (400, 429) and _is_budget_error(text):
                raise BudgetExhausted(f"{stage}: gateway budget exhausted ({last})")
            retryable = status == 0 or status == 429 or status >= 500
            if not retryable or attempt == self.max_retries:
                break
            time.sleep(2 ** (attempt + 1))
        raise LLMError(f"{stage}: {last}")
