"""Persistence-seam rule definitions (P048/P049, CONNECT-1275).

The object-store layout for a connection's cross-run state is owned by the SDK.
``application_sdk.common.incremental.helpers.get_persistent_s3_prefix`` is the
single function that answers "where does this connection's persistent state
live?", and ``application_sdk.common.incremental.marker`` builds the incremental
marker on top of it.  An app that assembles that prefix itself has forked the
answer, and the two copies drift.

Why this rule exists
--------------------
CONNECT-1136: an app's miner derived the connection directory from
``connection_qualified_name`` itself instead of calling the SDK helper.  The two
implementations disagreed on three points, and only one of them was visible:

* which segment is the connection id — the app took the first numeric segment
  after stripping a leading ``default``, the SDK takes ``parts[-1]``;
* what to do when that segment is not numeric — **the SDK warns and proceeds,
  the app raised**;
* unsafe characters — the app slugged them to ``<slug>-<sha256>``, putting its
  marker in a different directory from the crawler's for exactly the names the
  two were meant to share.

The second is what broke production.  A tenant that provisions connections
programmatically gets name-based qualified names (``default/<connector>/<name>``)
rather than epoch-based ones, and the app hard-failed on input the SDK
deliberately degrades on.  Nothing in the app's own tests covered it — every
fixture used an epoch — because the divergence is only observable on input the
app author never had a reason to write down.

That is the general shape this rule targets: **strictness drift**, not path
drift.  An app that re-derives an SDK-owned invariant will agree with the SDK on
the inputs its authors thought of and disagree on the ones they did not, and the
disagreement surfaces in one tenant, in production, long after review.

Two rules, two axes
-------------------
The fork has a structural half and a behavioural half, and an app can have
either without the other, so neither rule subsumes the other:

* ``P048`` — the app spells the SDK-owned layout out itself.  This is where the
  fleet's accumulated drift shows up.
* ``P049`` — the app parses ``connection_qualified_name`` itself and *raises*
  where the SDK warns.  This is the half that actually broke production.

"Re-implements an SDK helper" is not statically decidable in general.  What *is*
decidable is the fingerprint each half leaves: for ``P048``, app code assembling
the segment sequence ``persistent-artifacts/apps/<app>/connection``; for
``P049``, a ``raise`` reachable from a function that splits the qualified name
apart itself and does not hand that parse to the seam.

``P048`` matched the bare ``persistent-artifacts`` root segment first.  Measured
across the fleet that fired 65 times, of which **one** was the connection layout
— the rest were paths the SDK helper cannot produce and does not own, so their
only available action was a suppression in someone else's repo.  A rule whose
prescribed remedy does not apply teaches people to reach for
``# conformance: ignore`` reflexively.  Matching the layout instead collapses the
fleet to 5 sites, every one of which hand-builds the SDK's own connection
directory.

Both were validated against the CONNECT-1136 before/after.  The pre-fix module
fires both (a ``"/".join([...])`` assembling the connection layout, and a
``stable_marker_key`` that splits the qualified name and raises); the post-fix
module fires neither, because it derives the prefix from
``get_persistent_s3_prefix`` and its remaining ``raise`` wraps the SDK's own
error.  Across all connector apps, ``P048`` finds 5 sites and ``P049`` finds
none.

Scope
-----
Both are ``app``.  The SDK *is* the implementation of this layout and the owner
of this parse — its own modules spell the prefix out and decide what is fatal by
definition, so running these on the SDK would flag the source of truth.  The
runner drops out-of-scope findings automatically (``runner._rule_in_scope``), so
no in-check guard is needed.

Tier
----
The two land differently, and the fleet measurement is the reason.

``P048`` lands at ``WARN``.  Its 5 findings are real — each builds the SDK's own
connection directory by hand, one of them alongside a local re-derivation of the
connection id — but they are existing sites in other repos that need a migration
onto the seam, not a merge block on this PR.  Promotion to ``BLOCK`` belongs in a
later, evidence-based pass once the fleet is at zero unsuppressed.

``P049`` lands at ``BLOCK``, against the usual convention for a new rule.  That
convention exists so a new rule does not turn the fleet red overnight; ``P049``
finds zero violations across every connector app today, so there is no fleet to
redden and nothing to grandfather.  It is a recurrence guard for a production
incident, and blocking is what the RCA asked for.

P-ids are a permanent public contract (see ``prescriptions.py``).
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
        id="P048",
        fix_locus=FixLocus.APP,
        scope=RuleScope.APP,
        name="AppDerivedPersistentArtifactPrefix",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="persistence-seam",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.24.0",
        rationale=(
            "The persistent-artifacts object-store layout for a connection is owned by "
            "application_sdk.common.incremental.helpers.get_persistent_s3_prefix. An app "
            "that assembles the prefix itself forks the answer to 'where does this "
            "connection's state live?', and the copies drift on inputs the app's own "
            "fixtures never cover — most dangerously on strictness, where the SDK warns "
            "and proceeds but the app raises. CONNECT-1136 is that failure: a miner "
            "hard-failed on name-based connection qualified names that the crawler "
            "accepted, breaking a tenant that provisions connections programmatically."
        ),
        short_description=(
            "App builds the connection-scoped persistent-artifacts layout itself "
            "instead of deriving it from the SDK's get_persistent_s3_prefix"
        ),
        full_description=(
            "App code assembles the connection-scoped layout\n"
            "``persistent-artifacts/apps/<app>/connection/…`` itself rather than asking\n"
            "the SDK where a connection's persistent state lives.\n"
            "\n"
            "Use the SDK seam instead:\n"
            "\n"
            "* ``get_persistent_s3_prefix(connection_qualified_name, app_name)`` —\n"
            "  the connection-scoped prefix\n"
            "  (``persistent-artifacts/apps/{app}/connection/{connection_id}``);\n"
            "* ``fetch_marker_from_storage`` / ``persist_marker_to_storage``\n"
            "  (``application_sdk.common.incremental.marker``) — the incremental\n"
            "  marker read/write built on that prefix.\n"
            "\n"
            "Deriving the prefix locally is not merely duplication: the two copies\n"
            "agree on the inputs the app author thought of and diverge on the ones\n"
            "they did not.  In CONNECT-1136 an app's miner took the first numeric\n"
            "segment of the qualified name where the SDK takes the last, and **raised**\n"
            "where the SDK warns and proceeds.  Connections whose qualified name ends\n"
            "in a word rather than an epoch crawled fine and mined not at all, in one\n"
            "tenant, with every test passing.\n"
            "\n"
            "The match is the segment *sequence* the helper produces, not the\n"
            "``persistent-artifacts`` root: a path that diverges at the fourth segment\n"
            "(``state/``, ``workflows/``, ``skills``) or the second (the Argo\n"
            "``{cqn}/parquet/...`` layout) is one the helper cannot produce and does\n"
            "not own, so flagging it would prescribe a remedy that does not apply.\n"
            "\n"
            "The path is assembled across the whole expression — ``str.join``,\n"
            "f-strings and ``+`` concatenation — with runtime pieces standing in as a\n"
            "wildcard segment.  That matters: the CONNECT-1136 defect built its key\n"
            "from six separate constants joined by ``/``, and the only literal in that\n"
            "file carrying the whole layout was its *docstring*.  A check testing one\n"
            "literal at a time would have missed the defect it exists for.  Segments\n"
            "are matched exactly (``persistent-artifacts-backup`` does not match), and\n"
            "docstrings, comments and bare string statements are never flagged.\n"
            "\n"
            "Unlike ``P049`` there is no seam-import gate: a module that imports the\n"
            "seam and *still* hand-rolls the connection layout is exactly a finding\n"
            "worth making, and four of the five fleet sites are in such a module.\n"
            "\n"
            "Land as ``WARN``: apps that write under this prefix on paths the SDK\n"
            "helper does not model (Argo-layout compatibility, e.g.\n"
            "``persistent-artifacts/{cqn}/parquet/markers/{phase}``) record that with a\n"
            "justified ``# conformance: ignore[P048] <reason>``, which stays visible in\n"
            "SARIF and turns a silent fork into a reviewed decision.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/prescriptions.md#p048",
    ),
    RuleDefinition(
        id="P049",
        fix_locus=FixLocus.APP,
        scope=RuleScope.APP,
        name="StrictConnectionQualifiedNameParse",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="persistence-seam",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.24.0",
        rationale=(
            "extract_epoch_id_from_qualified_name deliberately warns and proceeds when "
            "a connection qualified name's last segment is not an epoch, because such "
            "connections crawl fine and must not be failed on. An app that parses the "
            "same value itself and raises is strictly more brittle than the SDK on "
            "input the SDK accepts, and the divergence is invisible to the app's own "
            "tests — every fixture uses an epoch, because the author had no reason to "
            "write down the case they did not know about. This is the precise "
            "CONNECT-1136 failure: a miner rejected name-based connection qualified "
            "names that the crawler accepted, so one tenant's connections crawled and "
            "never mined, with the whole suite green. Customer impact: the customer's "
            "workflow fails at its first step with a parse error naming a connection "
            "qualified name they never chose and cannot change, on every run, while "
            "the same connection crawls normally — so the failure looks arbitrary, no "
            "watermark is ever written, and query-based lineage and popularity go "
            "silently missing for as long as it takes someone to notice."
        ),
        short_description=(
            "App parses connection_qualified_name itself and raises, where the SDK "
            "warns and proceeds"
        ),
        full_description=(
            "A function takes a ``connection_qualified_name``, calls ``.split(...)`` on\n"
            "a value derived from it, and can ``raise`` out of its own body — while\n"
            "that function does not itself call ``get_persistent_s3_prefix`` or\n"
            "``extract_epoch_id_from_qualified_name``.\n"
            "\n"
            "The app has taken over a decision the SDK already makes, and made it\n"
            "stricter.  ``extract_epoch_id_from_qualified_name`` logs a warning and\n"
            "returns the segment when it is not numeric; the connection is usable and\n"
            "the crawler proceeds.  An app that raises on the same input fails only for\n"
            "connections named rather than epoch-stamped — which is a property of how a\n"
            "given tenant provisions connections, not of anything under test.\n"
            "\n"
            "Derive the value through the SDK instead and let it decide what is fatal::\n"
            "\n"
            "    from application_sdk.common.incremental.helpers import (\n"
            "        get_persistent_s3_prefix,\n"
            "    )\n"
            "\n"
            "    prefix = get_persistent_s3_prefix(connection_qualified_name, app_name)\n"
            "\n"
            "Raising a typed app error *around* the SDK call is correct and not\n"
            "flagged: a function that derives the value through one of those two\n"
            "helpers is delegating, so one that catches the SDK's error and re-raises\n"
            "its own stays silent.\n"
            "\n"
            "Only those two symbols count as delegation.  The marker helpers\n"
            "(``fetch_marker_from_storage``, ``persist_marker_to_storage``,\n"
            "``create_next_marker``, ``process_marker_timestamp``) take an\n"
            "already-derived prefix and leave the parse to their caller, so reading a\n"
            "marker through the SDK while still splitting the qualified name by hand\n"
            "is a finding — that half-migrated shape is the likeliest recurrence.\n"
            "\n"
            "Delegation is judged **per function**, not per module.  A module-level\n"
            "gate reads as delegation today, when almost no app module imports the\n"
            "seam — but the point of P048 and the published seam is that they all\n"
            "should, so such a gate would go blind exactly as adoption succeeds, and\n"
            "'module imports the seam and one function still hand-rolls a strict\n"
            "parse' is the likeliest shape of the next recurrence.\n"
            "\n"
            "This rule is a **heuristic**.  The ``.split`` receiver must trace\n"
            "syntactically to the parameter (through ``str()``, ``.strip()``,\n"
            "subscripting), so a function that splits some other string is not caught;\n"
            "a name rebound through an intermediate local is an accepted\n"
            "false-negative.  ``raise`` inside a nested ``def`` or ``lambda`` belongs to\n"
            "that scope, not the enclosing one.  Only ``connection_qualified_name`` is\n"
            "matched — table, column and asset qualified names have different owners\n"
            "and different segment semantics.\n"
            "\n"
            "Lands at ``BLOCK`` rather than ``WARN``, against the usual convention for\n"
            "a new rule.  The convention exists so a new rule does not turn the fleet\n"
            "red overnight; this one finds **zero** violations across all connector\n"
            "apps today (the one historical instance is fixed), so there is no fleet to\n"
            "redden and nothing to grandfather.  It is a recurrence guard for a\n"
            "production incident, and blocking is what the RCA asked for.  A deliberate\n"
            "stricter contract is still expressible with a justified\n"
            "``# conformance: ignore[P049] <reason>``.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/prescriptions.md#p049",
    ),
)
