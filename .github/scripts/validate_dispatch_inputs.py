"""Fail a dispatch that omits the ``distinct_id`` correlation key.

Why this exists
---------------
``codex-/return-dispatch`` v3 accepted a ``distinct_id`` action input and injected
it into the dispatch payload for you. v4 removed the input and injects nothing,
so the key is now the caller's job to put in ``workflow-inputs``.

Nothing enforced that. When the v4 bump first landed (#2923) the key silently
vanished from every dispatch, receivers fell back to a coarser concurrency group,
overlapping dispatches cancelled each other, and the resulting ``cancelled``
conclusion red-flagged the job. It took a revert (#2939) to unpick. The invariant
was documented afterwards, but a description cannot fail a build — the next caller
added to ``e2e-apps`` would rediscover the same thing.

This is that missing gate: it runs before the dispatch and fails loudly, naming
the key, instead of letting a receiver-side default paper over the omission.

The receiver's own grouping expression is deliberately not modelled here. It
lives in another repo and moves independently; see
``docs/standards/connector-ci-e2e.md`` for the contract.

Re-keying a retry
-----------------
``--attempt N`` (N > 1) validates the payload and then prints it back with
``distinct_id`` suffixed ``-attemptN``, for e2e-apps' retry of a dispatched run
that failed transiently.

A retry MUST NOT reuse the first attempt's ``distinct_id``. The receiver echoes
it into a step name purely so return-dispatch can locate the run by scanning
step names, and keys a ``cancel-in-progress: true`` concurrency group on it. Reuse
therefore breaks the retry twice over: return-dispatch can match the *first*,
already-concluded run and re-report its failure, and the receiver may cancel one
attempt in favour of the other. Either way the retry burns ten minutes and
changes nothing — a retry that silently cannot succeed is worse than no retry.

Usage::

    python validate_dispatch_inputs.py --workflow-inputs "$WORKFLOW_INPUTS"
    python validate_dispatch_inputs.py --workflow-inputs "$WORKFLOW_INPUTS" --attempt 2
"""

from __future__ import annotations

import argparse
import json
import sys

REQUIRED_KEYS = ("distinct_id",)


class DispatchInputsError(ValueError):
    """A ``workflow-inputs`` payload that return-dispatch v4 would mis-dispatch."""


def validate(raw: str, required_keys: tuple[str, ...] = REQUIRED_KEYS) -> None:
    """Raise ``DispatchInputsError`` unless every required key carries a value.

    ``raw`` is the literal ``workflow-inputs`` string. It must be a JSON object;
    the dispatch API only accepts flat string values, so a present-but-empty
    value is treated as absent — an empty ``distinct_id`` correlates nothing and
    is exactly the failure mode this guards.
    """
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DispatchInputsError(
            f"workflow-inputs is not valid JSON ({exc.msg} at position {exc.pos}): {raw!r}"
        ) from exc

    if not isinstance(payload, dict):
        raise DispatchInputsError(
            f"workflow-inputs must be a JSON object, got {type(payload).__name__}: {raw!r}"
        )

    missing = [
        key for key in required_keys if not str(payload.get(key, "") or "").strip()
    ]
    if missing:
        raise DispatchInputsError(
            "workflow-inputs is missing a non-empty "
            + ", ".join(missing)
            + ". codex-/return-dispatch v4 no longer injects distinct_id, so every "
            "caller must pass it in workflow-inputs (set it to the dispatching SHA). "
            "See docs/standards/connector-ci-e2e.md."
        )


def rekey_for_attempt(raw: str, attempt: int) -> str:
    """Return ``raw`` with ``distinct_id`` suffixed for a retry attempt.

    ``attempt <= 1`` returns the payload unchanged, so the first dispatch keeps
    the caller's SHA verbatim and only a genuine retry diverges. The payload is
    validated first: re-keying a payload with no ``distinct_id`` would invent a
    correlation key ("-attempt2") that correlates nothing.
    """
    validate(raw)
    payload = json.loads(raw)
    if attempt > 1:
        payload["distinct_id"] = f"{payload['distinct_id']}-attempt{attempt}"
    return json.dumps(payload)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workflow-inputs",
        required=True,
        help="The literal workflow-inputs JSON object passed to the dispatch.",
    )
    parser.add_argument(
        "--attempt",
        type=int,
        default=1,
        help="Dispatch attempt number. >1 prints the payload back with distinct_id "
        "suffixed so the retry cannot collide with the first attempt's run.",
    )
    args = parser.parse_args()
    try:
        rekeyed = rekey_for_attempt(args.workflow_inputs, args.attempt)
    except DispatchInputsError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    if args.attempt > 1:
        print(rekeyed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
