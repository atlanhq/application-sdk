"""Optimisation / recommendation rule definitions (O-series).

Below-the-prescription-bar recommendations and optimisations: things worth
nudging toward (performance, canonical-dependency choices) but not mandatory
enough to block a merge.  O-series rules are ``WARN`` by default; a rule that
earns mandatory status graduates into a category series or the P-series.
"""

from __future__ import annotations

from conformance.suite.schema.catalog import (
    RemediationKind,
    RemediationReference,
    RuleDefinition,
)
from conformance.suite.schema.disposition import (
    EnforcementTier,
    FixLocus,
    RuleMechanism,
    RuleScope,
)

RULES: tuple[RuleDefinition, ...] = (
    RuleDefinition(
        id="O001",
        canonical_reference=(
            "atlan-metabase-app app/utils.py — `write_jsonl` and `read_jsonl` "
            "serialise with `orjson.dumps` / `orjson.loads`, and the stdlib json "
            "module is imported nowhere under app/. orjson is a core SDK dependency, "
            "so there is no install cost to paying for the speed."
        ),
        scope=RuleScope.BOTH,
        name="OrjsonOverStdlibJson",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="canonical-dependency",
        autofixable=True,
        orthogonal_gate="tests",
        since="0.3.0",
        rationale=(
            "orjson is already a core SDK dependency — zero incremental cost — and on hot "
            "paths the ~10x throughput advantage compounds at fleet scale. WARN (not block) "
            "because orjson returns bytes not str and has a different option API, so each "
            "site needs human judgment before migrating."
        ),
        short_description="json.dumps()/json.loads() — prefer orjson (a core SDK dependency, ~10x faster)",
        full_description=(
            "``orjson`` is already a core dependency of the application SDK, so it is\n"
            "available to every app, and it is generally *at least* 10x faster than the\n"
            "stdlib ``json`` module.  Prefer ``orjson.dumps`` / ``orjson.loads`` for\n"
            "serialisation on any hot path.\n"
            "\n"
            "Only ``json.dumps`` and ``json.loads`` call sites are flagged, and only\n"
            "when ``json`` resolves to the stdlib module (an ``import json`` /\n"
            "``import json as …`` / ``from json import dumps|loads`` binding).  Bare\n"
            "``.json()`` attribute calls (e.g. ``response.json()``) are never flagged.\n"
            "``json.JSONDecodeError`` handling, ``json.dump``/``json.load`` (file-object\n"
            "APIs orjson does not provide), and custom ``JSONEncoder`` subclasses are\n"
            "out of scope.\n"
            "\n"
            "Autofixable per-site, not mechanically: ``orjson`` is not a drop-in\n"
            "replacement.  ``orjson.dumps``\n"
            "returns ``bytes`` (not ``str``), has no ``indent=`` / ``sort_keys=`` /\n"
            "``default=`` keyword surface (use ``option=orjson.OPT_INDENT_2 |\n"
            "orjson.OPT_SORT_KEYS`` and the ``default`` positional), and rejects some\n"
            "inputs stdlib accepts.  A blind ``json.``→``orjson.`` swap silently changes\n"
            "``str``→``bytes`` and breaks callers — each site needs human judgement.\n"
            "The encoded bytes also change on any call that does not already pass\n"
            '``separators=(",", ":")`` and ``ensure_ascii=False``: orjson is always\n'
            "compact and always writes non-ASCII as UTF-8, with no option for\n"
            "either.  The parsed value is identical, so tests that compare parsed\n"
            "JSON pass; a consumer that hashes, commits or byte-compares the output\n"
            "sees the difference.\n"
            "\n"
            "No orjson call reproduces stdlib's default bytes: there is no separators\n"
            "option, no ``ensure_ascii`` option, ``orjson.dumps`` cannot serialize an\n"
            "integer above 64 bits, and\n"
            'rewriting orjson\'s text corrupts string values that contain ``", "``.\n'
            "So a ``json.dumps`` whose output orjson cannot reproduce, and whose\n"
            "string is stored as one attribute or field value and hashed or\n"
            "byte-compared as text outside the app, stays on stdlib ``json`` behind\n"
            "a directive naming that attribute and where it is compared; see\n"
            "*Already correct when*.  A ``dumps`` that serializes a whole entity or\n"
            "document does not qualify.\n"
        ),
        terminal_state=(
            "A justified inline `# conformance: ignore[O001] <reason>` IS the correct "
            "end state for a `json.dumps` only when all three hold. (1) orjson cannot "
            "reproduce the call's output: the call does not already pass both "
            '`separators=(",", ":")` and `ensure_ascii=False`, or its input can hold an '
            "integer above 64 bits. A call that passes both on 64-bit-safe input is "
            "byte-identical to `orjson.dumps(...).decode()` and makes the swap. (2) The "
            "encoded string is stored as one attribute or field value, and a consumer "
            "outside the app hashes or byte-compares that value as text. (3) The reason "
            "names the attribute key or field and the location (repo and file:line) "
            "where that consumer hashes or compares it as text. A `dumps` that "
            "serializes a whole entity or document does not qualify, even when the "
            "document is later hashed: the publish app parses the document before it "
            "diffs it, and there is no single attribute key to cite, so that site makes "
            "the swap. A directive whose reason names no attribute key or no comparison "
            "location, or names a consumer that only parses the JSON or sits inside the "
            "app, is unremediated: make the swap."
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/optimizations.md#o001",
        remediation_reference=RemediationReference(
            kind=RemediationKind.PRESCRIPTION,
            target="programs/areas/optimizations.prose.md",
        ),
    ),
    RuleDefinition(
        id="O002",
        canonical_reference=(
            "atlan-metabase-app app/asset_mapper.py — `serialize_entity` encodes each "
            "asset through `entity_bytes` under the app's `ENTITY_ENVELOPE`, rather than "
            "through `.dict()`, then decodes that output to merge in the custom "
            "attributes pyatlan_v9 does not model."
        ),
        scope=RuleScope.APP,
        name="LegacyAssetSerialization",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="asset-mapper",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.8.0",
        rationale=(
            "The asset-mapper pattern serialises pyatlan assets to JSONL through the "
            "SDK's entity_bytes seam, which emits the nested-entity wire shape the "
            "platform expects. Serialising an asset with the pydantic .dict() "
            "method produces a flat dict that still needs hand-conversion and drifts "
            "from the SDK's recommended pipeline (BLDX-1492; docs/upgrade-guide-v3.md). "
            "WARN/recommendation because .dict() is name-anchored — it can also belong "
            "to a non-asset pydantic model — so the call needs a human glance."
        ),
        short_description=(
            "Asset serialised with .dict() — serialize through the SDK's entity_bytes"
        ),
        full_description=(
            "Flags a ``.dict()`` method call in a module that imports pyatlan asset\n"
            "models.  The asset-mapper pattern writes assets through\n"
            "``application_sdk.common.asset_serialization.entity_bytes`` — not the\n"
            "pydantic ``.dict()`` form (``docs/upgrade-guide-v3.md`` explicitly says\n"
            "'use the v9 serialisation API instead of .dict()').  Do not swap in\n"
            "``asset.to_nested_bytes()``: that bypasses the seam and trips P052.\n"
            "\n"
            "Legacy ``pyatlan.model.assets`` models: migrate the model to\n"
            "``pyatlan_v9.model.assets`` first (O004), then switch serialization.\n"
            "A v1 model handed to ``entity_bytes`` falls through to\n"
            "``model_dump()``, whose snake_case field names are not the Atlas wire\n"
            "shape, so the serialization switch alone emits malformed entities.\n"
            "The finding message says which case applies.\n"
            "\n"
            "Coverage limits (biased to low false-positives at WARN): only ``.dict()``\n"
            "is matched (not ``.json()``, which is overwhelmingly ``response.json()``\n"
            "on HTTP clients), and only in files that import asset models.  A\n"
            "``.dict()`` on a *non-asset* pydantic model in such a file is a known\n"
            "false-positive — suppress with ``# conformance: ignore[O002] <reason>``.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/optimizations.md#o002",
        remediation_reference=RemediationReference(
            kind=RemediationKind.SKILL,
            target="migrate-asset-modeling",
        ),
    ),
    RuleDefinition(
        id="O003",
        canonical_reference=(
            "atlan-metabase-app app/asset_mapper.py — `map_collection` is annotated `-> "
            "MetabaseCollection`, the pyatlan_v9 type it constructs and returns "
            "(`map_dashboard` and `map_bi_process` likewise), so a wrong asset type is a "
            "type error rather than a runtime surprise in the payload."
        ),
        scope=RuleScope.APP,
        name="UntypedAssetMapperReturn",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="asset-mapper",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.8.0",
        rationale=(
            "The asset-mapper pattern's value is end-to-end typing: a mapper function "
            "constructs a pyatlan asset and returns it, so the return annotation "
            "documents which asset it produces and lets pyright check the call site. "
            "A mapper that builds an asset but declares no return type loses that "
            "guarantee (BLDX-1492; reference app atlan-metabase-app). WARN/recommendation "
            "because adding the annotation is a safe, mechanical nudge."
        ),
        short_description=(
            "Function builds a pyatlan asset but has no return annotation — annotate "
            "it with the asset type"
        ),
        full_description=(
            "Flags a function that constructs a pyatlan asset (instantiates a class\n"
            "imported from ``pyatlan_v9.model.assets`` / ``pyatlan.model.assets``) and\n"
            "**returns that asset**, but carries no ``-> <Asset>`` return annotation.\n"
            "The asset-mapper pattern is typed end-to-end — each ``map_<entity>``\n"
            "function declares the pyatlan asset it produces (see ``atlan-metabase-app``).\n"
            "\n"
            "Keyed on actually returning the constructed asset (``return Table(...)`` or\n"
            "``asset = Table(...); ... return asset``), not just a ``map_`` name — so a\n"
            "helper that builds an asset as a side effect and returns something else\n"
            "(e.g. ``return record.id``) is not flagged, and the suggested annotation\n"
            "always matches the real return.  Suppress with\n"
            "``# conformance: ignore[O003] <reason>`` when an untyped return is\n"
            "intentional.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/optimizations.md#o003",
        remediation_reference=RemediationReference(
            kind=RemediationKind.SKILL,
            target="migrate-asset-modeling",
        ),
    ),
    RuleDefinition(
        id="O004",
        canonical_reference=(
            "atlan-mysql-app app/mysql.py — `from pyatlan_v9.model.assets import Column, "
            "Database, Procedure, Schema, Table, View`. The non-v9 pyatlan.model.assets "
            "path appears nowhere under the three reference apps' app/ directories."
        ),
        scope=RuleScope.APP,
        name="LegacyPyatlanAssetImport",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="asset-mapper",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.8.0",
        rationale=(
            "pyatlan_v9 is the SDK's go-forward asset surface for the asset-mapper "
            "pattern; the legacy pyatlan.model.assets classes are the memory-heavy "
            "DataFrame/transformer-era serialization path, kept only for connectors "
            "still on the built-in AtlasTransformer (which B001 steers off). pyatlan_v9 "
            "ships inside the existing pyatlan>=9 dependency, so the switch adds nothing "
            "to resolve. A below-the-bar recommendation (O-series, WARN): the v9 models "
            "differ in attributes and serialization (entity_bytes vs .dict()), so "
            "each site needs human judgement — never a blind name swap."
        ),
        short_description=(
            "Imports pyatlan.model.assets (non-v9) — prefer pyatlan_v9.model.assets"
        ),
        full_description=(
            "Flags app code that imports asset model classes from the legacy\n"
            "``pyatlan.model.assets`` package, in any of the three import forms:\n"
            "``from pyatlan.model.assets import X``, ``import pyatlan.model.assets``,\n"
            "or ``from pyatlan.model import assets``.  Detection is import-anchored\n"
            "(an asset class is only imported in order to construct it), so the rare\n"
            "fully-qualified ``pyatlan.model.assets.X(...)`` form with no matching\n"
            "import is out of scope.  New connectors should build assets from\n"
            "``pyatlan_v9.model.assets`` — the optimized v9 surface the asset-mapper\n"
            "pattern is built on (BLDX-1492; see\n"
            "``docs/guides/sql-application-guide.md`` and ``docs/upgrade-guide-v3.md``).\n"
            "\n"
            "Scope is deliberately narrow — only ``pyatlan.model.assets`` is matched,\n"
            "never the rest of ``pyatlan``: enums and helpers that legitimately have\n"
            "no v9 equivalent (e.g. ``from pyatlan.model.enums import\n"
            "AtlanConnectorType``) are out of scope.\n"
            "\n"
            "Not a mechanical rewrite: the v9 models are not a drop-in rename —\n"
            "attribute\n"
            "names and the serialization API differ (serialize through\n"
            "``entity_bytes`` rather than ``.dict()``), so each construction site\n"
            "needs review.\n"
            "Suppress with ``# conformance: ignore[O004] <reason>`` when a connector\n"
            "is intentionally pinned to the legacy ``AtlasTransformer`` surface.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/optimizations.md#o004",
        remediation_reference=RemediationReference(
            kind=RemediationKind.SKILL,
            target="migrate-asset-modeling",
        ),
    ),
    RuleDefinition(
        id="O006",
        canonical_reference=(
            "No reference app imports rocksdict. The SDK seam is "
            "application_sdk/common/spillable_dict.py — `SpillableDict`, which pickles "
            "values so a caller needs no hand-rolled serialize/deserialize step around the "
            "store."
        ),
        scope=RuleScope.APP,
        name="DirectRocksdictImport",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="canonical-dependency",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.18.0",
        rationale=(
            "SpillableDict (application_sdk.common.spillable_dict) already wraps "
            "rocksdict.Rdict as a MutableMapping and pickles values directly, so it "
            "carries none of the hand-rolled-serialization risk a from-scratch "
            "wrapper does. Two connectors (atlan-thoughtspot-app, "
            "atlan-aws-smus-app) independently hand-rolled the same RocksDB-backed "
            "DiskLookup with an asymmetric JSON serialize/deserialize step — put() "
            "special-cased str, get() unconditionally ran json.loads() — so a "
            "stored string that was also valid bare JSON (a numeric-looking name, "
            "'true', 'null') silently came back as int/bool/None instead of str "
            "(CNCT-80, CNCT-191). WARN (not block) because a from-scratch wrapper "
            "may have a deliberate reason (custom RocksDB Options, a key type "
            "outside str/int/float/bool/bytes) that needs a human glance before "
            "migrating."
        ),
        short_description=(
            "Imports rocksdict directly — prefer the SDK's SpillableDict "
            "(pickles values, no hand-rolled serialize/deserialize step)"
        ),
        full_description=(
            "Flags app code that imports the ``rocksdict`` package directly, in "
            "either import form: ``from rocksdict import Rdict`` or ``import "
            "rocksdict``.  Detection is import-anchored (a direct ``rocksdict`` "
            "import is the unambiguous signal — nothing else pulls that dependency "
            "in).\n"
            "\n"
            "The SDK ships ``application_sdk.common.spillable_dict.SpillableDict`` "
            "— a ``MutableMapping``-compatible, disk-backed dict built on the same "
            "``rocksdict.Rdict``, which pickles values directly rather than "
            "hand-rolling a serialize/deserialize step.  It exists specifically so "
            "connector apps stop reinventing this wrapper.\n"
            "\n"
            "Motivating incident: ``atlan-thoughtspot-app`` and "
            "``atlan-aws-smus-app`` each independently wrote a ``DiskLookup`` class "
            "directly on ``rocksdict.Rdict`` with the identical bug — a value's "
            "``str`` type was silently lost on a round-trip through JSON when the "
            "string happened to also be valid bare JSON.  Neither connector's "
            "hand-rolled wrapper was calling anything the SDK had a fleet-wide "
            "signal for at the time; this rule is that signal going forward.\n"
            "\n"
            "Not a mechanical rewrite: ``SpillableDict``'s key type is restricted to "
            "``str | int | float | bool | bytes`` and it has no equivalent to a "
            "custom ``rocksdict.Options`` tuning surface, so each call site needs "
            "review before migrating.  Suppress with ``# conformance: "
            "ignore[O006] <reason>`` when a from-scratch wrapper is deliberate "
            "(e.g. custom RocksDB tuning, or association-list output like "
            "``rocks_backed_dict.py``'s ``append_to_key`` that ``SpillableDict`` "
            "does not provide).\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/optimizations.md#o006",
        remediation_reference=RemediationReference(
            kind=RemediationKind.GUIDE,
            target="programs/areas/optimizations.prose.md",
        ),
    ),
    RuleDefinition(
        id="O005",
        canonical_reference=(
            "atlan-metabase-app app/connector.py — `MetabaseApp` declares `name = "
            '"metabase"` and atlan.yaml carries `name: metabase`; even the upload '
            "prefix built in `extract_metadata` spells the name out. The name is "
            "resolved once, at declaration; a `{app_name}` left in a plain string is a "
            "substitution nothing will ever perform."
        ),
        scope=RuleScope.BOTH,
        name="UnresolvedAppNamePlaceholder",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="dag-write-path",
        autofixable=True,
        orthogonal_gate="tests",
        since="0.18.0",
        rationale=(
            "A plain string literal carrying an unsubstituted '{app_name}' token "
            "freezes the literal token into whatever it's assigned to instead of the "
            "real app name — the exact shape that shipped a task queue no worker "
            "polls, hanging dbt:process to its 24h heartbeat backstop (CONNECT-183). "
            "The substitution was independently hand-rolled at least four times "
            "(Heracles, native-migration-app, atlan-local-marketplace-app "
            "CONNECT-191/#539, atlan-hightouch-app ARUN-1039), and one of those "
            "shipped a double prefix (DISTR-834). A canonical helper — "
            "application_sdk.common.task_queue (derive_task_queue, "
            "resolve_manifest_tokens) — lands in the SDK release that ships "
            "FND-195, so remediation then has a single target; this rule still "
            "checks the unresolved shape rather than a missing "
            "import, because the writers most worth catching are hand-authored "
            "templates that import nothing. WARN (not BLOCK): a template legitimately "
            "resolved by a caller in a different file cannot be seen from here, so "
            "each hit needs a human glance rather than an automatic fail."
        ),
        short_description=(
            "Hardcoded '{app_name}' left unsubstituted in a plain string literal"
        ),
        full_description=(
            "Flags a string ``ast.Constant`` containing the literal substring\n"
            "``{app_name}`` when the token can actually **reach a value** — including\n"
            'the pieces of an escaped-brace f-string: ``f"atlan-{{app_name}}-prod"``\n'
            "is *not* interpolated, its runtime value is the literal\n"
            "``atlan-{app_name}-prod``, so it freezes the token exactly like a plain\n"
            "literal. A token that only ever appears in prose or in a diagnostic\n"
            "cannot freeze into an identifier, and flagging it just teaches people\n"
            "to suppress the rule.\n"
            "\n"
            "Not flagged:\n"
            "\n"
            '* part of a *resolving* f-string (``f"...{app_name}..."`` interpolates\n'
            "  at parse time and no ``{app_name}`` text survives into its pieces),\n"
            "* the receiver of a ``.format(...)`` call whose keywords include\n"
            "  ``app_name=`` (a proper, already-resolving substitution site),\n"
            "* **documentation** — the value of any bare string expression statement.\n"
            "  This covers module/class/function docstrings *and* PEP 257 attribute\n"
            "  docstrings, which are not the first statement of their class body,\n"
            "* **diagnostic text** — inside the arguments of a logging call,\n"
            "  ``warnings.warn(...)``, or a ``raise``; reporting on the token requires\n"
            "  quoting it,\n"
            "* **a token sentinel or message constant** — bound to an ``ALL_CAPS``\n"
            "  name where the literal is exactly the token (the token's own\n"
            "  definition, e.g. ``APP_NAME_TOKEN``) or the name ends in a prose\n"
            "  segment (``_MESSAGE``, ``START_MESSAGE``, ``VALIDATION_RATIONALE``).\n"
            "\n"
            "The exclusions stay narrow: an ``ALL_CAPS`` name holding a real queue\n"
            'template (``TASK_QUEUE = "atlan-{app_name}-prod"``) is still flagged —\n'
            "and so is one whose name merely *contains* a prose fragment without a\n"
            "trailing boundary (``MESSAGE_QUEUE``, ``HELP_QUEUE``) — as are keyword\n"
            "arguments, values at any depth in a DAG literal, and returned templates.\n"
            "\n"
            "Detection is shape-anchored rather than import-anchored, because the\n"
            "writers most worth catching are hand-authored templates outside the SDK\n"
            "that import nothing at all. Remediation: an f-string or\n"
            "``.format(app_name=...)`` when the name is in scope; the shared\n"
            "``application_sdk.common.task_queue`` helper (``derive_task_queue`` /\n"
            "``resolve_manifest_tokens``) lands in the SDK release that ships\n"
            "FND-195 and is the canonical target once available.\n"
            "\n"
            "Not a mechanical rewrite: the correct fix depends on where\n"
            "``app_name`` is\n"
            "actually available in scope — sometimes an f-string is right, sometimes\n"
            "the value needs threading in from a caller first. Suppress with\n"
            "``# conformance: ignore[O005] <reason>`` for a template resolved by a\n"
            "caller in a different file than the one being scanned.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/optimizations.md#o005",
        remediation_reference=RemediationReference(
            kind=RemediationKind.PRESCRIPTION,
            target="programs/areas/optimizations.prose.md",
        ),
    ),
    RuleDefinition(
        id="O007",
        canonical_reference=(
            "application_sdk/storage/formats/__init__.py — the writers' "
            "`_upload_file` passes `retain_local_copy=` explicitly, and the JSON and "
            "Parquet writers default it to False, so a chunk uploaded inline leaves "
            "the worker's disk once it is in the store. No reference app calls "
            "upload_file directly; they hand files off through App.upload and "
            "App.upload_refs."
        ),
        scope=RuleScope.APP,
        name="UploadRetainsLocalCopy",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="worker-disk",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.51.0",
        rationale=(
            "upload_file keeps the local file after a successful upload unless the "
            "caller passes retain_local_copy=False (the keyword defaults to True), "
            "and even then it deletes only a file under TEMPORARY_PATH. A connector "
            "that uploads as it extracts therefore keeps every file it has already "
            "uploaded on the worker's disk, and a review of connector worker disks "
            "found exactly that growth on long runs. WARN, and a migration rather "
            "than an automatic edit, because the call site alone cannot say whether "
            "the file is still needed: a later read in the same task, or a "
            "FileReference built from it, breaks once the file is deleted, and "
            "retain_local_copy=False on a file outside TEMPORARY_PATH clears the "
            "finding while deleting nothing."
        ),
        short_description=(
            "upload_file() keeps the local file after the upload (retain_local_copy "
            "defaults to True), so uploaded files pile up on the worker's disk"
        ),
        full_description=(
            "Flags a call to the SDK's ``upload_file`` that omits\n"
            "``retain_local_copy`` or passes the literal ``True``.  ``upload_file``\n"
            "keeps the local file after a successful upload by default, and with\n"
            "``retain_local_copy=False`` deletes it only when the file sits under\n"
            "``TEMPORARY_PATH`` (``application_sdk/storage/ops.py``).  An app that\n"
            "uploads as it goes therefore leaves every uploaded file on the\n"
            "worker's disk for the rest of the run.\n"
            "\n"
            "Only calls that resolve to the SDK function are flagged.  The binding\n"
            "must come from an SDK module that exposes it —\n"
            "``application_sdk.storage``, ``application_sdk.storage.ops``, the\n"
            "deprecated ``application_sdk.common.incremental.marker`` alias, or a\n"
            "module that imports it at module level (``storage.batch``,\n"
            "``storage.cloud``, …) — as ``from … import upload_file [as x]``,\n"
            "through a module alias (``import application_sdk.storage.ops as ops``\n"
            "then ``ops.upload_file(...)``), or by the fully dotted path.  An app's\n"
            "own ``upload_file``, a client method of that name and the v2\n"
            "``ObjectStore.upload_file`` are never matched.\n"
            "``retain_local_copy=False`` is compliant; any other expression (a\n"
            "variable, a parameter, a conditional) and a ``**kwargs`` splat are left\n"
            "alone because the value is not known statically.\n"
            "\n"
            "Fix: when nothing reads the file after the upload, pass\n"
            "``retain_local_copy=False`` with the file under ``TEMPORARY_PATH`` —\n"
            "outside it the keyword deletes nothing — or delete the file right\n"
            "after the upload.  Not a mechanical rewrite: first confirm the same\n"
            "task does not read the file again and does not return a\n"
            "``FileReference`` built from it.  When the file must stay, suppress\n"
            "with ``# conformance: ignore[O007] <reason>`` naming what reads it and\n"
            "what removes it afterwards (see *Already correct when*).\n"
        ),
        rule_interactions=(
            "O008: retain_local_copy=False deletes only under TEMPORARY_PATH, so a "
            "file staged by a tempfile call with no dir= is not deleted by it. Move "
            "that scratch under TEMPORARY_PATH first, or delete the file explicitly."
        ),
        terminal_state=(
            "A justified inline `# conformance: ignore[O007] <reason>` IS the "
            "correct end state when the file is still needed after the upload (the "
            "same task reads it again, or returns a FileReference built from it) "
            "and the reason names that reader and what removes the file afterwards. "
            "A directive that names neither is unremediated: delete the file after "
            "the upload, or pass retain_local_copy=False with it under "
            "TEMPORARY_PATH."
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/optimizations.md#o007",
        remediation_reference=RemediationReference(
            kind=RemediationKind.GUIDE,
            target="programs/areas/optimizations.prose.md",
        ),
    ),
    RuleDefinition(
        id="O008",
        canonical_reference=(
            "application_sdk/storage/ops.py — `download_file` stages its partial "
            "file with `tempfile.mkstemp(dir=str(staging_dir), ...)` in a directory "
            "beside the destination, so the scratch sits on the same volume as the "
            "file it becomes. No reference app passes dir=: atlan-openapi-app's "
            "connector and atlan-metabase-app's `task_scratch_dir` call "
            "tempfile.mkdtemp with only a prefix, the shape this rule flags."
        ),
        scope=RuleScope.APP,
        name="TempFileOutsideWorkerVolume",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="worker-disk",
        autofixable=True,
        orthogonal_gate="tests",
        since="0.51.0",
        rationale=(
            "Worker disk is provisioned, measured and cleaned at TEMPORARY_PATH: "
            "apps mount their worker volume where it lives, upload_file deletes "
            "only under it, and the end-of-run cleanup_files task removes the run's "
            "directory beneath it. The stdlib tempfile constructors default to the "
            "system temp dir instead ($TMPDIR, else /tmp), and the SDK base image "
            "sets no TMPDIR, so unless an app mounts its volume at /tmp that scratch "
            "lands on the node's root disk. A review of connector worker disks found "
            "scratch there that volume metrics never saw, that disk sizing missed "
            "and that no cleanup removed. WARN: it is a placement and hygiene fix "
            "rather than a broken run, and the reference apps themselves still "
            "create scratch this way."
        ),
        short_description=(
            "tempfile scratch with no dir= lands in the system temp dir, outside "
            "TEMPORARY_PATH and the worker volume — pass dir= under TEMPORARY_PATH"
        ),
        full_description=(
            "Flags a stdlib ``tempfile`` call — ``mkdtemp``, ``mkstemp``,\n"
            "``NamedTemporaryFile``, ``TemporaryDirectory``, ``TemporaryFile`` or\n"
            "``SpooledTemporaryFile`` — that is given no directory: ``dir=``\n"
            "omitted, ``dir=None``, or ``dir=tempfile.gettempdir()`` (the same\n"
            "location spelled out).  A ``dir`` passed positionally is read at each\n"
            "constructor's real position.  Only stdlib bindings count (``import\n"
            "tempfile [as t]``, ``from tempfile import mkdtemp [as x]``), so an app\n"
            "helper of the same name is never matched; any other ``dir=``\n"
            "expression is compliant, and a ``*args`` / ``**kwargs`` splat that may\n"
            "supply it is left alone.\n"
            "\n"
            "Such scratch lands in the system temp dir (``$TMPDIR``, else\n"
            "``/tmp``).  The SDK base image sets no ``TMPDIR``, so unless the app\n"
            "mounts its worker volume at ``/tmp`` it lands on the node's root disk:\n"
            "worker-volume metrics and disk sizing never see it, and filling it\n"
            "starves the node rather than the app's volume.  Either way it sits\n"
            "outside ``TEMPORARY_PATH``, the tree the SDK's local cleanup works in\n"
            "(``upload_file`` deletes only under it; ``cleanup_files`` removes the\n"
            "run's directory beneath it).\n"
            "\n"
            "Fix: pass ``dir=`` a directory under ``TEMPORARY_PATH``\n"
            "(``application_sdk.constants``), creating it first with\n"
            "``os.makedirs(..., exist_ok=True)`` because ``tempfile`` does not create\n"
            "a missing ``dir``.  Autofixable per-site, not mechanically: reuse the\n"
            "app's own scratch-root helper when it already resolves under\n"
            "``TEMPORARY_PATH``, and keep the call's other arguments as they are.\n"
            "In an app that declares no worker volume, ``TEMPORARY_PATH`` itself\n"
            "sits on the container's disk, so there the fix buys cleanup reach and\n"
            "puts the scratch where a volume lands once the app declares one.\n"
            "\n"
            "Out of scope: ``tempfile.mktemp`` (deprecated) and paths assembled by\n"
            "hand from ``tempfile.gettempdir()``.  A process-wide\n"
            "``tempfile.tempdir`` or ``TMPDIR`` override is invisible to a per-file\n"
            "scan, so an app that points one under ``TEMPORARY_PATH`` still sees\n"
            "findings: pass ``dir=`` anyway, or suppress with\n"
            "``# conformance: ignore[O008] <reason>``.\n"
        ),
        rule_interactions=(
            "O007: upload_file(retain_local_copy=False) deletes only under "
            "TEMPORARY_PATH, so moving this scratch there is also what lets an "
            "upload clean up after itself."
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/optimizations.md#o008",
        remediation_reference=RemediationReference(
            kind=RemediationKind.PRESCRIPTION,
            target="programs/areas/optimizations.prose.md",
        ),
    ),
    RuleDefinition(
        id="O009",
        canonical_reference=(
            "atlan-mysql-app atlan.yaml — its `deploy:` block declares no `storage` "
            "or `storageWorker` entry, so there is no fixed-size worker volume for "
            "this rule to grade. No reference app declares a worker volume, so none "
            "shows the compliant entry: a small base `size` plus the "
            "`resize.topolvm.io/storage_limit`, `threshold` and `increase` "
            "annotations, the shape this rule's description spells out."
        ),
        fix_locus=FixLocus.PACKAGING,
        scope=RuleScope.APP,
        name="WorkerVolumeWithoutAutoresize",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="worker-disk",
        autofixable=False,
        orthogonal_gate="skip",
        since="0.51.0",
        rationale=(
            "A worker volume declared in atlan.yaml's deploy block without "
            "pvc-autoresizer annotations is a fixed-size PVC: it never grows, so it "
            "must be sized up front for the largest run any tenant makes, and every "
            "pod that mounts it gets that full size whether its own run needs it or "
            "not. A review of connector worker disks found a connector giving every "
            "worker a fixed volume more than ten times larger than its largest "
            "normal run used. The alternative is already in use across the fleet: "
            "a small base size plus the resize.topolvm.io/storage_limit, threshold "
            "and increase annotations, so only the pods that need more disk grow. "
            "WARN because a fixed size is a cost and headroom trade-off rather than "
            "a broken deploy, and the base size has to come from the app's own "
            "disk-usage numbers."
        ),
        short_description=(
            "atlan.yaml worker storage entry has no resize.topolvm.io/storage_limit "
            "annotation, so the volume is a fixed size sized for the largest tenant"
        ),
        full_description=(
            "Flags each worker storage entry in ``atlan.yaml``'s ``deploy:`` block\n"
            "— ``deploy.storage`` (mounted on every pod) and\n"
            "``deploy.storageWorker`` (worker pods only) — that carries no\n"
            "``resize.topolvm.io/storage_limit`` annotation.  The finding sits on\n"
            "the entry's ``-`` line and names its ``name``, ``mountPath`` and\n"
            "declared ``size``.  ``deploy.storageServer`` is not graded: it mounts\n"
            "on server pods only.\n"
            "\n"
            "Without a ``storage_limit`` the pvc-autoresizer never grows the PVC, so\n"
            "the volume is a fixed ``size``: it must cover the largest run any\n"
            "tenant makes, and every pod that mounts it gets the full size.  The\n"
            "compliant entry is a small base ``size`` plus the three annotations:\n"
            "\n"
            ".. code-block:: yaml\n"
            "\n"
            "    storage:\n"
            "      - name: local-data\n"
            "        mountPath: /app/local\n"
            "        size: 8Gi\n"
            "        annotations:\n"
            "          resize.topolvm.io/storage_limit: 192Gi\n"
            '          resize.topolvm.io/threshold: "50%"\n'
            "          resize.topolvm.io/increase: 8Gi\n"
            "\n"
            "Size the base from the app's per-pod disk peaks so that about 1% of\n"
            "pods need to grow (roughly the p99 peak), set ``storage_limit`` above\n"
            "the largest run observed with headroom, and pick ``threshold`` (the\n"
            "free space left when a resize fires) and ``increase`` (the growth\n"
            "step) so a growing pod stays ahead of its write rate.\n"
            "\n"
            "**Where to edit.**  ``atlan.yaml`` is graded because it is what the\n"
            "platform deploys, hand-written or generated.  When\n"
            "``contract/app.pkl`` emits it, the storage list comes from the\n"
            "contract's ``deploy`` → ``overrides`` mapping (the toolkit models no\n"
            "storage field), so make the change there and regenerate; never\n"
            "hand-edit a generated ``atlan.yaml``:\n"
            "\n"
            ".. code-block:: pkl\n"
            "\n"
            "    overrides {\n"
            '      ["storage"] = new Listing {\n'
            "        new Mapping {\n"
            '          ["name"] = "local-data"\n'
            '          ["mountPath"] = "/app/local"\n'
            '          ["size"] = "8Gi"\n'
            '          ["annotations"] = new Mapping {\n'
            '            ["resize.topolvm.io/storage_limit"] = "192Gi"\n'
            '            ["resize.topolvm.io/threshold"] = "50%"\n'
            '            ["resize.topolvm.io/increase"] = "8Gi"\n'
            "          }\n"
            "        }\n"
            "      }\n"
            "    }\n"
            "\n"
            "Otherwise ``atlan.yaml`` is hand-owned; edit it directly.\n"
            "\n"
            "Not a mechanical rewrite: the base and the limit come from the app's\n"
            "own disk-usage numbers, and the edit lands in deploy configuration\n"
            "rather than Python.  The scan is line-based (no YAML dependency) and\n"
            "reads block-style sequences in both layouts the fleet uses; a\n"
            "flow-style sequence, a YAML anchor or merge key, per-pool storage\n"
            "under ``workerPools`` and raw ``extraVolumes`` PVC templates are not\n"
            "graded.  Suppress with ``# conformance: ignore[O009] <reason>`` on the\n"
            "entry's ``-`` line or the line above it — possible only in a\n"
            "hand-written ``atlan.yaml``, since regeneration drops comments from a\n"
            "generated one.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/optimizations.md#o009",
        remediation_reference=RemediationReference(
            kind=RemediationKind.GUIDE,
            target="programs/areas/optimizations.prose.md",
        ),
    ),
)
