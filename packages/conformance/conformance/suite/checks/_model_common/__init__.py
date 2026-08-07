"""Shared infrastructure for model-driven check series (BLDX-1575).

This is the neutral home for everything a ``mechanism=model`` checker needs, the
way ``_ast_common`` is the home for AST checkers.  The first consumer is the
``M`` (customer-facing metadata) series, but the pieces here are series-agnostic
so the next model-driven series needs no new plumbing.

What lives here
---------------

* :class:`TextVerdict` — the typed, structured response every model classifier
  returns.  No free-form dict parsing: the model is *forced* to return this
  shape (via tool-use), and it is validated on ingress.
* :class:`ModelClient` — the Protocol a classifier depends on.  Production code
  gets :class:`AnthropicModelClient`; tests inject a deterministic stub.
* :func:`get_client` / :func:`set_client_override` — client resolution.  When no
  API key is present (or the ``anthropic`` extra is not installed) the default
  client is ``None`` and callers **skip cleanly** — a model series must never
  turn a keyless ``detect`` run red.
* :func:`parse_yaml_directives` — line-based ``# conformance: ignore[...]``
  parsing for YAML/text surfaces (the AST directive parser is Python-only), so
  app owners suppress a model finding exactly the way they suppress any other.
* :func:`make_model_finding` — maps a verdict to a suppression-aware
  :class:`Finding` carrying the model's evidence span.

Determinism / audit posture
----------------------------

Model verdicts are non-deterministic by nature, so the model id is **pinned**
(:data:`MODEL_ID`) and ``temperature=0``; the rule records ``model_id`` +
``prompt_version`` (surfaced as ``atlan/modelId`` / ``atlan/promptVersion``) and
every finding carries the flagged span (``atlan/evidence``).  These make a
finding attributable and reviewable even though it cannot be perfectly
reproduced.
"""

from __future__ import annotations

import logging
import os
from typing import Protocol, TypeVar

from conformance.suite.checks._ast_common import (
    _IgnoreDirective,
    parse_ignore_directive,
)
from conformance.suite.schema.findings import Finding
from pydantic import BaseModel

logger = logging.getLogger("conformance.model")

# Pinned model id for every model-driven rule.  Recorded on findings for audit;
# bump deliberately (and bump the affected rules' ``prompt_version``) when the
# model changes so historical findings stay attributable.
MODEL_ID = "claude-haiku-4-5-20251001"

# Environment variable that carries the API key.  Absence → clean skip.
API_KEY_ENV = "ANTHROPIC_API_KEY"

_MAX_TOKENS = 1024


# ---------------------------------------------------------------------------
# Structured response
# ---------------------------------------------------------------------------


class TextVerdict(BaseModel):
    """A model's judgement about a single span of text.

    The classifier is forced to return exactly this shape (tool-use), so
    checkers never parse free-form model prose.
    """

    flagged: bool
    """``True`` if the text violates the rule the classifier was asked about."""

    evidence: str = ""
    """The exact offending span copied verbatim from the input (empty when not
    flagged).  Surfaced as ``atlan/evidence``."""

    explanation: str = ""
    """One-sentence, customer-neutral reason the span was flagged — becomes the
    finding message.  Empty when not flagged."""


T = TypeVar("T", bound=BaseModel)


# ---------------------------------------------------------------------------
# Client protocol + resolution
# ---------------------------------------------------------------------------


class ModelClient(Protocol):
    """The minimal classifier surface a model checker depends on.

    Kept tiny so tests can supply a deterministic stub without importing any
    model SDK.
    """

    def classify(self, *, system: str, user: str, schema: type[T]) -> T:
        """Return a validated ``schema`` instance for the given prompt."""
        ...


class AnthropicModelClient:
    """:class:`ModelClient` backed by the Anthropic Messages API (tool-forced).

    Imported lazily and only constructed when an API key is present, so the base
    conformance package stays free of any model dependency.
    """

    def __init__(self, *, model: str = MODEL_ID, api_key: str | None = None) -> None:
        # Lazy import of the optional 'model' extra; unresolved in the base
        # (LLM-free) type/CI env, hence the targeted ignore.
        from anthropic import Anthropic  # pyright: ignore[reportMissingImports]

        self._model = model
        self._client = Anthropic(api_key=api_key) if api_key else Anthropic()

    def classify(self, *, system: str, user: str, schema: type[T]) -> T:
        tool_name = "report_verdict"
        tool = {
            "name": tool_name,
            "description": "Report the structured verdict for the given text.",
            "input_schema": schema.model_json_schema(),
        }
        response = self._client.messages.create(
            model=self._model,
            max_tokens=_MAX_TOKENS,
            temperature=0,
            system=system,
            tools=[tool],
            tool_choice={"type": "tool", "name": tool_name},
            messages=[{"role": "user", "content": user}],
        )
        for block in response.content:
            if getattr(block, "type", None) == "tool_use":
                return schema.model_validate(block.input)
        raise ValueError("model returned no tool_use block")


_client_override: ModelClient | None = None
_skip_logged = False


def set_client_override(client: ModelClient | None) -> None:
    """Install (or clear, with ``None``) a client used in place of the default.

    The seam tests use to inject a deterministic stub.  Always reset to ``None``
    in a fixture teardown so state does not leak between tests.
    """
    global _client_override
    _client_override = client


def get_client() -> ModelClient | None:
    """Resolve the active model client.

    Returns the test override when set; otherwise builds an
    :class:`AnthropicModelClient` when an API key is present and the ``anthropic``
    package is importable.  Returns ``None`` (logged once, at WARNING) when no
    client is available so callers skip cleanly instead of erroring.
    """
    global _skip_logged
    if _client_override is not None:
        return _client_override
    if not os.environ.get(API_KEY_ENV):
        if not _skip_logged:
            logger.warning(
                "model checks skipped: %s is not set (no API key available)",
                API_KEY_ENV,
            )
            _skip_logged = True
        return None
    try:
        return AnthropicModelClient()
    except ImportError:
        if not _skip_logged:
            logger.warning(
                "model checks skipped: the 'anthropic' package is not installed "
                "(install the conformance 'model' extra to enable them)",
            )
            _skip_logged = True
        return None


# ---------------------------------------------------------------------------
# Suppression + finding construction for text/YAML surfaces
# ---------------------------------------------------------------------------


def parse_yaml_directives(source: str) -> dict[int, _IgnoreDirective]:
    """Return ``{lineno: directive}`` for ``# conformance: ignore[...]`` comments.

    The AST directive parser (``_ast_common._parse_directives``) tokenises Python
    and cannot read a YAML file, so this does a simple line scan: any ``#`` on a
    line is treated as a comment and handed to the shared
    :func:`parse_ignore_directive` grammar.  Every directive is recorded as
    ``comment_only`` — YAML has no trailing-code-then-directive ambiguity — so a
    directive on the flagged line *or* the line directly above it suppresses.
    """
    directives: dict[int, _IgnoreDirective] = {}
    for lineno, line in enumerate(source.splitlines(), start=1):
        hash_idx = line.find("#")
        if hash_idx == -1:
            continue
        parsed = parse_ignore_directive(line[hash_idx:])
        if parsed is not None:
            directives[lineno] = parsed
    return directives


def make_model_finding(
    *,
    rule_id: str,
    file: str,
    line: int,
    column: int = 1,
    message: str,
    evidence: str,
    directives: dict[int, _IgnoreDirective],
) -> Finding:
    """Build a suppression-aware :class:`Finding` for a model verdict.

    A directive on the flagged ``line`` — or on the comment-only line directly
    above it — suppresses the finding when it names ``rule_id`` (or is a wildcard
    ``# conformance: ignore``).  Mirrors ``_ast_common.make_finding`` semantics
    for text surfaces, and attaches the model's ``evidence`` span.
    """
    suppressed = False
    justification: str | None = None
    for check_line in (line, line - 1):
        directive = directives.get(check_line)
        if directive is None:
            continue
        if check_line == line - 1 and not directive.comment_only:
            continue
        if directive.rule_ids is None or rule_id in directive.rule_ids:
            suppressed = True
            justification = directive.justification
            break
    return Finding(
        rule_id=rule_id,
        file=file,
        line=line,
        column=column,
        message=message,
        suppressed=suppressed,
        suppression_justification=justification,
        evidence=evidence,
    )


__all__ = [
    "API_KEY_ENV",
    "MODEL_ID",
    "AnthropicModelClient",
    "Finding",
    "ModelClient",
    "TextVerdict",
    "get_client",
    "make_model_finding",
    "parse_yaml_directives",
    "set_client_override",
]
