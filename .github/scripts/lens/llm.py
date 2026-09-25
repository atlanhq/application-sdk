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
import re
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


class FatalRequestError(LLMError):
    """lens sent something the gateway will always refuse (bad key, alias,
    request shape or size). The whole run stops; nothing is retried."""


def prompt_view(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Messages as they count toward size estimates: without the raw Responses
    output items (`_items`), whose encrypted reasoning is a large opaque blob
    that would otherwise trip the context ceiling and inflate reservations.
    The same turn's text and tool calls are still counted via content/tool_calls."""
    return [{k: v for k, v in m.items() if k != "_items"} for m in messages]


def assistant_turn(comp: "Completion") -> dict[str, Any]:
    """The assistant message to append after a completion — carrying the raw
    Responses items when there are any, so reasoning survives across turns."""
    msg: dict[str, Any] = {"role": "assistant", "content": comp.content or None}
    if comp.tool_calls:
        msg["tool_calls"] = comp.tool_calls
    if comp.message.get("_items"):
        msg["_items"] = comp.message["_items"]
    return msg


def estimate_tokens(text: str) -> int:
    # ~3.5 chars/token for code-heavy English; deliberately pessimistic so the
    # pre-call estimate errs toward refusing, not overspending.
    return int(len(text) / 3.5) + 1


@dataclass
class Price:
    input_per_mtok: float
    cached_input_per_mtok: float
    output_per_mtok: float
    cache_write_per_mtok: float | None = None  # default: 1.25x input (OpenAI, GPT-5.6+)

    @property
    def prompt_ceiling(self) -> float:
        """The most a prompt token can cost: an uncached write, never less than input."""
        return max(
            self.input_per_mtok, self.cache_write_per_mtok or self.input_per_mtok * 1.25
        )


@dataclass
class Ledger:
    """Spend for one PR across every call in every round. Serialised into the
    PR's state marker so round N sees what rounds 1..N-1 already spent."""

    cap_usd: float
    spent_usd: float = 0.0
    reserved_usd: float = 0.0
    failed_requests: int = 0
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
        never overshoot the cap, whatever the concurrency."""
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
            "failed_requests": self.failed_requests,
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
            failed_requests=int(d.get("failed_requests", 0)),
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
    """One model endpoint, shared by every stage and bundle of a run.

    Failed requests cost money and time too, so the client is built to make
    as few as possible:

    - `preflight()` checks the key and model with the gateway's free
      endpoints before the first completion — a wrong alias or a spent key
      stops the run at zero requests, instead of failing N parallel calls.
    - A circuit breaker is shared by all threads: after
      `max_consecutive_failures` failed attempts in a row anywhere in the
      run, every later call is refused without being sent.
    - 429s wait for the gateway's `Retry-After` (capped), not a blind backoff.
    - A 400 that names a parameter the gateway does not accept is learnt
      once: that parameter is dropped (or `tool_choice: required` downgraded)
      for the rest of the run, so it fails one request, not every request.
    - A budget-exceeded response is never retried.
    """

    # Optional request parameters the client may drop if the gateway rejects them.
    DROPPABLE = ("prompt_cache_key", "reasoning_effort", "temperature")
    # Client errors: the request, key or alias is wrong. Never retried; they stop the run.
    FATAL = frozenset({400, 401, 403, 404, 405, 413, 422})

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
        max_consecutive_failures: int = 3,
        max_retry_after_s: float = 30.0,
        reasoning_effort: str | None = None,
        api: str = "chat",
        transport: Any = None,
        meta_transport: Any = None,
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
        self.max_consecutive_failures = max_consecutive_failures
        self.max_retry_after_s = max_retry_after_s
        self.reasoning_effort = reasoning_effort
        # "responses" is the only API on which reasoning models reason AND call tools
        # (Chat Completions requires reasoning_effort "none" with tools). `fell_back`
        # records a one-time switch to chat when the gateway has no /v1/responses.
        self.api = api
        self.fell_back = ""
        self.diagnostics: list[str] = []  # what answered an unexpected preflight status
        self._transport = transport or self._http
        self._meta = meta_transport or self._http_get
        self.unsupported: set[str] = set()
        self.required_tool_choice_ok = True
        self._fail_streak = 0
        self._breaker_lock = threading.Lock()

    @property
    def send_cache_key(self) -> bool:
        return "prompt_cache_key" not in self.unsupported

    # ---- pricing -------------------------------------------------------
    def worst_case_cost(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        max_tokens: int,
    ) -> float:
        prompt = json.dumps(prompt_view(messages)) + (
            json.dumps(tools) if tools else ""
        )
        return (
            estimate_tokens(prompt) * self.price.prompt_ceiling
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
    def _root(self) -> str:
        if not self.base_url or not self.api_key:
            raise LLMError(
                "LiteLLM is not configured: set LITELLM_BASE_URL and LENS_LITELLM_KEY."
            )
        return self.base_url.removesuffix("/v1")

    def _http(self, body: dict[str, Any]) -> tuple[int, dict[str, str], str]:
        req = urllib.request.Request(
            self._root()
            + ("/v1/responses" if self.api == "responses" else "/v1/chat/completions"),
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

    def _http_get(self, path: str) -> tuple[int, str]:
        req = urllib.request.Request(
            self._root() + path, headers={"Authorization": f"Bearer {self.api_key}"}
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 - fixed https base from config
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    # ---- preflight: zero-token checks ------------------------------------
    def _diagnose(self, path: str, status: int, text: str) -> None:
        """Record what answered an unexpected preflight status, so a CI failure
        explains itself: LiteLLM's JSON error, or an HTML page from an edge
        (e.g. Cloudflare) in front of it. Never the key or the base URL; any
        key-shaped token in the body is redacted."""
        if status in (200, 401):
            return
        low = text.lower()
        if "cloudflare" in low or "cf-ray" in low:
            who = "a Cloudflare page (edge block, not LiteLLM)"
        elif "<html" in low:
            who = "an HTML page (an edge or proxy, not LiteLLM)"
        else:
            who = "the gateway's JSON"
        snippet = re.sub(r"sk-[A-Za-z0-9._-]+", "sk-…", " ".join(text.split()))[:160]
        self.diagnostics.append(
            f"preflight {path}: HTTP {status} from {who}: {snippet}"
        )

    def preflight(self, min_budget_usd: float) -> str | None:
        """None when the run may start; otherwise the reason it must not.

        Uses only the gateway's metadata endpoints — no completion, no tokens.
        A check the gateway cannot answer (older LiteLLM, no permission) is
        skipped rather than failed: preflight exists to avoid wasted requests,
        not to add a new way for lens to break.

        Only a 401 means the key itself is bad. A 403 on a METADATA route means
        this key may not call that route (LiteLLM keys can be scoped to the
        completion routes) — observed on the first live run, with a key that
        does serve completions. That check is skipped; if completions are
        forbidden too, the first real call fails fast with the gateway's text."""
        try:
            status, text = self._meta("/v1/models")
        except (LLMError, TimeoutError, urllib.error.URLError, OSError) as e:
            return f"gateway unreachable: {e}"
        self._diagnose("/v1/models", status, text)
        if status == 401:
            return f"the LiteLLM key was rejected (HTTP 401: {text[:160]})"
        if status == 200:
            try:
                ids = {m.get("id") for m in json.loads(text).get("data", [])}
            except (ValueError, AttributeError):
                ids = set()
            if ids and self.model not in ids:
                return f"model {self.model!r} is not available to this key"
        try:
            status, text = self._meta("/key/info")
        except (TimeoutError, urllib.error.URLError, OSError):
            return None
        self._diagnose("/key/info", status, text)
        if status == 200:
            try:
                info = json.loads(text).get("info") or {}
            except (ValueError, AttributeError):
                return None
            budget, spend = info.get("max_budget"), info.get("spend")
            if (
                budget is not None
                and spend is not None
                and float(budget) - float(spend) < min_budget_usd
            ):
                return f"the gateway key has ${float(budget) - float(spend):.2f} of budget left (needs ${min_budget_usd:.2f})"
        return None

    # ---- breaker -------------------------------------------------------
    def _record(self, ok: bool) -> None:
        with self._breaker_lock:
            self._fail_streak = 0 if ok else self._fail_streak + 1
            if not ok:
                self.ledger.failed_requests += 1

    def _trip(self) -> None:
        with self._breaker_lock:
            self._fail_streak = max(self._fail_streak, self.max_consecutive_failures)

    def _breaker_open(self) -> bool:
        with self._breaker_lock:
            return self._fail_streak >= self.max_consecutive_failures

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
        if self._breaker_open():
            raise LLMError(
                f"{stage}: not sent — {self._fail_streak} consecutive failed requests; stopping this run"
            )
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

    def _body(
        self,
        messages,
        max_tokens,
        tools,
        response_format,
        temperature,
        tool_choice,
        cache_key,
    ) -> dict[str, Any]:  # noqa: ANN001
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if temperature is not None and "temperature" not in self.unsupported:
            body["temperature"] = temperature
        if tools:
            body["tools"] = tools
            body["tool_choice"] = (
                "auto"
                if tool_choice == "required" and not self.required_tool_choice_ok
                else tool_choice
            )
        if response_format:
            body["response_format"] = response_format
        if self.reasoning_effort and "reasoning_effort" not in self.unsupported:
            body["reasoning_effort"] = self.reasoning_effort
        if cache_key and self.send_cache_key:
            # Routes requests sharing a prefix to the same cache (OpenAI `prompt_cache_key`).
            body["prompt_cache_key"] = cache_key
        return body

    def _learn_rejection(self, body: dict[str, Any], text: str) -> bool:
        """A 400 naming a parameter we can live without: stop sending it. True if learnt."""
        low = text.lower()
        if (
            "reasoning" in body
            and "reasoning" in low
            and "reasoning_effort" not in body
        ):
            self.unsupported.update({"reasoning", "include"})
            body.pop("reasoning")
            body.pop("include", None)
            return True
        if "include" in body and "include" in low:
            self.unsupported.add("include")
            body.pop("include")
            return True
        for p in self.DROPPABLE:
            if p in body and p in low:
                self.unsupported.add(p)
                body.pop(p)
                return True
        if body.get("tool_choice") == "required" and "tool_choice" in low:
            self.required_tool_choice_ok = False
            body["tool_choice"] = "auto"
            return True
        return False

    def _retry_after(self, headers: dict[str, str], attempt: int) -> float:
        for k, v in headers.items():
            if k.lower() == "retry-after":
                try:
                    return min(max(float(v), 0.0), self.max_retry_after_s)
                except ValueError:
                    break
        return float(2 ** (attempt + 1))

    # ---- Responses API ---------------------------------------------------
    @staticmethod
    def _to_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Chat-shaped messages as Responses `input` items. An assistant turn the
        Responses API produced carries its raw output items (`_items`) — including
        the encrypted reasoning — and those are replayed verbatim, so the model's
        reasoning survives across tool turns instead of restarting each turn."""
        out: list[dict[str, Any]] = []
        for m in messages:
            role = m.get("role")
            if role == "tool":
                out.append(
                    {
                        "type": "function_call_output",
                        "call_id": m.get("tool_call_id") or "",
                        "output": m.get("content") or "",
                    }
                )
            elif role == "assistant" and m.get("_items"):
                out.extend(m["_items"])
            elif role == "assistant":
                if m.get("content"):
                    out.append({"role": "assistant", "content": m["content"]})
                for tc in m.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    out.append(
                        {
                            "type": "function_call",
                            "call_id": tc.get("id") or "",
                            "name": fn.get("name") or "",
                            "arguments": fn.get("arguments") or "{}",
                        }
                    )
            else:
                out.append({"role": role or "user", "content": m.get("content") or ""})
        return out

    def _responses_body(
        self, messages, max_tokens, tools, temperature, tool_choice, cache_key
    ) -> dict[str, Any]:  # noqa: ANN001
        body: dict[str, Any] = {
            "model": self.model,
            "input": self._to_input(messages),
            "max_output_tokens": max_tokens,
            "store": False,  # stateless: nothing kept server-side; reasoning rides back encrypted
        }
        if tools:
            body["tools"] = [{"type": "function", **t["function"]} for t in tools]
            body["tool_choice"] = (
                "auto"
                if tool_choice == "required" and not self.required_tool_choice_ok
                else tool_choice
            )
        if self.reasoning_effort and "reasoning" not in self.unsupported:
            body["reasoning"] = {"effort": self.reasoning_effort}
            if "include" not in self.unsupported:
                body["include"] = ["reasoning.encrypted_content"]
        if temperature is not None and "temperature" not in self.unsupported:
            body["temperature"] = temperature
        if cache_key and self.send_cache_key:
            body["prompt_cache_key"] = cache_key
        return body

    @staticmethod
    def _parse_responses(
        data: dict[str, Any],
    ) -> tuple[str, list[dict[str, Any]], dict[str, Any], dict[str, Any], str]:
        items = data.get("output") or []
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for it in items:
            if it.get("type") == "message":
                for c in it.get("content") or []:
                    if c.get("type") in ("output_text", "text"):
                        text_parts.append(c.get("text") or "")
            elif it.get("type") == "function_call":
                tool_calls.append(
                    {
                        "id": it.get("call_id") or it.get("id") or "",
                        "type": "function",
                        "function": {
                            "name": it.get("name") or "",
                            "arguments": it.get("arguments") or "{}",
                        },
                    }
                )
        u = data.get("usage") or {}
        usage = {
            "prompt_tokens": u.get("input_tokens", 0),
            "completion_tokens": u.get("output_tokens", 0),
            "prompt_tokens_details": {
                "cached_tokens": (u.get("input_tokens_details") or {}).get(
                    "cached_tokens", 0
                )
            },
            "reasoning_tokens": (u.get("output_tokens_details") or {}).get(
                "reasoning_tokens", 0
            ),
        }
        content = "".join(text_parts)
        message = {
            "role": "assistant",
            "content": content,
            "tool_calls": tool_calls,
            "_items": items,
        }
        return content, tool_calls, usage, message, str(data.get("status") or "")

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
        def build() -> dict[str, Any]:
            if self.api == "responses":
                return self._responses_body(
                    messages, max_tokens, tools, temperature, tool_choice, cache_key
                )
            return self._body(
                [{k: v for k, v in m.items() if k != "_items"} for m in messages],
                max_tokens,
                tools,
                response_format,
                temperature,
                tool_choice,
                cache_key,
            )

        body = build()
        last = ""
        attempt = 0
        learnt = 0
        while True:
            try:
                status, headers, text = self._transport(body)
            except (TimeoutError, urllib.error.URLError, OSError) as e:
                status, headers, text = 0, {}, f"transport: {e}"
            if status == 200:
                self._record(True)
                data = json.loads(text)
                if self.api == "responses":
                    _, _, usage, msg, finish = self._parse_responses(data)
                    choice = {"message": msg, "finish_reason": finish}
                else:
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
            self._record(False)
            last = f"HTTP {status}: {text[:300]}"
            if self.api == "responses" and status in (404, 405, 501):
                # The gateway has no /v1/responses: switch once, for the whole run, to chat —
                # where luna can call tools only without reasoning — and say so.
                self.api = "chat"
                self.fell_back = f"/v1/responses unavailable (HTTP {status}); used chat completions with reasoning off"
                self.reasoning_effort = "none"
                with self._breaker_lock:
                    self._fail_streak = max(
                        self._fail_streak - 1, 0
                    )  # a routing miss, not an outage
                body = build()
                continue
            if status in (400, 429) and _is_budget_error(text):
                raise BudgetExhausted(f"{stage}: gateway budget exhausted ({last})")
            if (
                status == 400
                and learnt < len(self.DROPPABLE) + 1
                and self._learn_rejection(body, text)
            ):
                learnt += 1
                continue  # a rejected optional parameter is not transport weather: no attempt consumed
            if status in self.FATAL:
                # Our side is wrong (key, alias, request shape, context size). Every other
                # bundle would send the same thing and fail the same way: open the breaker
                # for the whole run and stop now, without a single retry.
                self._trip()
                raise FatalRequestError(
                    f"{stage}: {last} — stopping the run (a request lens got wrong; retrying cannot help)"
                )
            retryable = status == 0 or status == 429 or status >= 500
            if not retryable or attempt >= self.max_retries or self._breaker_open():
                break
            time.sleep(self._retry_after(headers, attempt))
            attempt += 1
        raise LLMError(f"{stage}: {last}")
