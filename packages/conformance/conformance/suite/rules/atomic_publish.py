"""Atomic-publish rule definition (P050, CONNECT-1126).

A file that another coroutine, activity or process may read must never be
written in place: an ``os.open`` with ``O_TRUNC`` empties the destination the
moment it opens, and every byte written after that is visible mid-flight.  A
reader that arrives during the write sees a truncated or zero-filled file at
the artifact's real name — the exact mechanism behind a production RCA where
two activities of one run shared a ``FileReference.local_path`` and a JSONL
parser failed at line 1 column 1 on NUL bytes.

The SDK's own doctrine (FND-318, ``common/atomic.py``) is temp file →
``os.replace``: the destination either does not exist or holds a complete
file.  The sanctioned spellings are the ``common.atomic`` helpers
(``atomic_write`` / ``atomic_path`` / ``atomic_copy``) or an explicit staging
file inside ``PARTIAL_DIRNAME`` published with ``os.replace``.  This rule
grades the raw-fd spelling that bypasses all of them: an ``os.open`` whose
flags carry ``O_TRUNC`` in a scope that never publishes via ``os.replace`` /
``os.rename``.

Scope
-----
``P050`` is ``sdk``-scoped: the transfer and writer seams live in the SDK, and
consumer apps are steered to ``FileReference`` / SDK writers by the storage-seam
rules (P008–P012) rather than to raw descriptors.

Ceiling
-------
The rule pins the raw-fd shape only.  ``open(path, "wb")`` and
``Path.write_bytes`` also write in place, but those spellings are dominated by
sanctioned uses (the ``common.atomic`` staging internals, append-mode writers
that cannot be atomic, tooling output), so grading them would bury the signal
in suppressions.  Widen only with a corpus survey in hand.

Rule-id stability (non-migration policy)
----------------------------------------
Rule ids are a permanent public contract — each is exposed in SARIF
``help_uri`` and referenced by inline ``# conformance: ignore[Pxxx]``
suppressions.  An id therefore **never migrates and never changes**.
"""

from __future__ import annotations

from conformance.suite.schema.catalog import RuleDefinition
from conformance.suite.schema.disposition import (
    EnforcementTier,
    FixLocus,
    RuleMechanism,
    RuleScope,
)

RULES: tuple[RuleDefinition, ...] = (
    RuleDefinition(
        id="P050",
        fix_locus=FixLocus.SDK,
        scope=RuleScope.SDK,
        name="NonAtomicDestinationWrite",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="storage-atomicity",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.25.0",
        rationale=(
            "os.open with O_TRUNC truncates the destination at open time and "
            "streams bytes into it in place, so any concurrent reader — another "
            "activity materialising the same FileReference.local_path, a parser "
            "already holding the path — observes a truncated or zero-filled "
            "file at the artifact's real name. A production RCA traced a JSONL "
            "parse failure at char 0 to exactly this: two concurrent downloads "
            "of one shared local_path, both reporting success. Write to a "
            "staging file (PARTIAL_DIRNAME, or the common.atomic helpers) and "
            "publish with os.replace, so the destination only ever holds a "
            "complete file (CONNECT-1126)."
        ),
        short_description=(
            "os.open(..., O_TRUNC) writes a destination in place with no "
            "os.replace publish in scope"
        ),
        full_description=(
            "An ``os.open`` call whose flags include ``O_TRUNC``, in a function\n"
            "that never calls ``os.replace`` / ``os.rename``.  The destination\n"
            "is truncated the moment the descriptor opens and filled in place,\n"
            "so a concurrent reader of the same path sees a partial file at the\n"
            "artifact's real name — indistinguishable from a complete one until\n"
            "a parser fails on it much later.\n"
            "\n"
            "``FileReference.local_path`` is a deterministic function of\n"
            "(run, stage, entity), so concurrent activities of one run share\n"
            "destinations by construction; an in-place write at such a path is\n"
            "reader-visible corruption waiting for a schedule to trigger it\n"
            "(CONNECT-1126).\n"
            "\n"
            "The sanctioned pattern is the FND-318 doctrine: write to a staging\n"
            "file — the ``common.atomic`` helpers, or an explicit temp inside\n"
            "``PARTIAL_DIRNAME`` — and publish with ``os.replace``, which is\n"
            "atomic on POSIX and Windows.  An ``os.open`` + ``O_TRUNC`` whose\n"
            "enclosing function publishes via ``os.replace`` / ``os.rename`` is\n"
            "recognised as that pattern and passes.\n"
            "\n"
            "Land as ``WARN``: a justified inline ``# conformance: ignore[P050]\n"
            "<reason>`` records any single-consumer exception (e.g. a signal\n"
            "handler's diagnostic dump) and stays visible in SARIF.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/prescriptions.md#p050",
    ),
)
