"""Pinned log-message bodies for the SDK's structured outcome events.

An *outcome event* is a log line whose **message body is a contract**: dashboards,
alert rules and connector-pulse queries match on the exact string, so rewording one
silently empties every panel keyed off it. This module is the single place those
strings are written down.

Emitting a queryable event is a three-part contract, and all three edits are
required or the fields silently never reach OTLP:

1. a pinned name constant — **here**, and it is the log message body;
2. an attribute-key constant in
   :mod:`application_sdk.observability.logger_adaptor`, shared with the emitter so
   a rename is one edit;
3. entries in that module's ``_KNOWN_EXTRA_KEYS`` allowlist, which is what gates
   kwargs into the emitted ``LogRecord``'s attributes.

Why a separate module rather than ``logger_adaptor``: importing ``logger_adaptor``
pulls loguru and the OTLP exporter stack, and a caller that only needs to *name* an
event (a test, a validator, a conformance rule) should not pay for that. This file
imports nothing.

Nothing here may ever be reworded. A new event is a new constant; a renamed event
is a new constant plus a migration for every consumer of the old string.
"""

from __future__ import annotations

from typing import Final

# ── Preflight gate (execution/_temporal/preflight_gate.py) ──────────────────

#: The gate's per-run verdict row. Re-exported unchanged from
#: ``preflight_gate.PREFLIGHT_OUTCOME_EVENT``.
PREFLIGHT_OUTCOME_EVENT: Final = "Preflight gate outcome"

#: The boot-time posture row, emitted once per gate-registered app at worker
#: build — the denominator the outcome events cannot supply. Re-exported
#: unchanged from ``preflight_gate.PREFLIGHT_POSTURE_EVENT``.
PREFLIGHT_POSTURE_EVENT: Final = "Preflight gate posture"

#: The interactive-surface sibling of the gate's outcome row (FND-901): one row
#: per ``Handler.preflight_check`` verdict reached outside a gated run — the
#: HTTP setup form and the SDR test-connection activity, distinguished by the
#: ``preflight_surface`` attribute. Deliberately a distinct body from
#: ``PREFLIGHT_OUTCOME_EVENT`` so gate dashboards (run counts, posture
#: denominators) are not polluted by setup-time checks.
PREFLIGHT_CHECK_EVENT: Final = "Preflight check outcome"

# ── Validation (validation/, app/base.py) ───────────────────────────────────

#: Transformed-asset (NDJSON x pyatlan_v9 model) validation outcome, emitted from
#: the upload activity. Re-exported unchanged from
#: ``app.base.ASSET_VALIDATION_EVENT``. ADR-0020 folds this check into the
#: artifact wrapper as one format x source cell; the name is preserved verbatim
#: through that move because shipped dashboards key off it.
ASSET_VALIDATION_EVENT: Final = "Transformed-asset validation outcome"

#: Generic artifact validation outcome (ADR-0020): one row per artifact hand-off,
#: whatever its format and whichever schema source declared it. Emitted for every
#: hand-off including the negative outcomes — a check that reports nothing is
#: indistinguishable from a check that passed.
ARTIFACT_VALIDATION_EVENT: Final = "Artifact validation outcome"

#: The boot-time artifact-validation posture row (ADR-0020), emitted once per
#: registered app at worker build — soft and disabled apps included, because that
#: is the denominator the outcome events cannot supply. An app whose artifacts
#: never reach a hand-off emits no outcome row at all, so posture drift and
#: adoption are invisible from outcomes alone.
ARTIFACT_VALIDATION_POSTURE_EVENT: Final = "Artifact validation posture"


#: Every pinned event body, for tests and introspection. Membership here is the
#: definition of "this string is a contract".
OUTCOME_EVENT_NAMES: Final[frozenset[str]] = frozenset(
    {
        PREFLIGHT_OUTCOME_EVENT,
        PREFLIGHT_POSTURE_EVENT,
        PREFLIGHT_CHECK_EVENT,
        ASSET_VALIDATION_EVENT,
        ARTIFACT_VALIDATION_EVENT,
        ARTIFACT_VALIDATION_POSTURE_EVENT,
    }
)

__all__ = [
    "ARTIFACT_VALIDATION_EVENT",
    "ARTIFACT_VALIDATION_POSTURE_EVENT",
    "ASSET_VALIDATION_EVENT",
    "OUTCOME_EVENT_NAMES",
    "PREFLIGHT_CHECK_EVENT",
    "PREFLIGHT_OUTCOME_EVENT",
    "PREFLIGHT_POSTURE_EVENT",
]
