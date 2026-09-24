"""Guard against a tolerated exception silently masking a real failure.

Proposal for BLDX-1625 — see the PR description for open questions. Not yet
adopted anywhere; nothing in the SDK or fleet calls this today.

Context: at least six connector apps (thoughtspot, gcs, atlan-iceberg-app,
atlan-soda-app, atlan-adls-app, atlan-databricks-app) independently hit and
separately hand-patched the same defect — a broad ``except Exception`` meant
to tolerate one known-benign outcome (a missing table, an empty prefix) was
also catching everything else, including a rate limit or an auth failure,
and reporting all of it as the same empty/``None`` result. A caller cannot
tell "the source genuinely has nothing" from "something broke and we hid
it" — and reporting the latter as the former is how a transient throttle
gets published as a truncated, asset-deleting crawl.
"""

from __future__ import annotations


def reraise_unless_tolerated(
    exc: BaseException,
    *,
    tolerated: tuple[type[BaseException], ...],
) -> None:
    """Re-raise ``exc`` unless its type is explicitly declared tolerable here.

    Call this as the first line of an ``except Exception as exc:`` block that
    is about to degrade to an empty/``None`` result. ``tolerated`` is an
    allowlist, not a denylist: the caller names exactly the outcomes that are
    known-benign at this call site (e.g. a catalog's own not-found type), and
    everything else — including a typed ``AppError`` subclass the SDK or a
    lower layer already raised, such as ``RateLimitedError`` or ``AuthError``
    — escapes unchanged. A no-op when ``exc`` matches ``tolerated``; the
    caller's existing tolerance logic runs as before.
    """
    if isinstance(exc, tolerated):
        return
    raise exc
