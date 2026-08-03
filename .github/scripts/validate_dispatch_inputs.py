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

Usage::

    python validate_dispatch_inputs.py --workflow-inputs "$WORKFLOW_INPUTS"
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workflow-inputs",
        required=True,
        help="The literal workflow-inputs JSON object passed to the dispatch.",
    )
    args = parser.parse_args()
    try:
        validate(args.workflow_inputs)
    except DispatchInputsError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
