"""Optimisation / recommendation rule definitions (O-series).

Below-the-prescription-bar recommendations and optimisations: things worth
nudging toward (performance, canonical-dependency choices) but not mandatory
enough to block a merge.  O-series rules are ``WARN`` by default; a rule that
earns mandatory status graduates into a category series or the P-series.

O007 and O008 are vacant: they were drafted as worker-disk recommendations and
graduated to the mandatory P060 (UploadedFileNotDeleted) and P061
(TempFileOutsideWorkerVolume) before any release, so no suppression ever cited
them.  Those O-ids stay vacant.
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
            "returns ``bytes`` (not ``str``) and has no ``indent=`` / ``sort_keys=``\n"
            "keyword surface (use ``option=orjson.OPT_INDENT_2 |\n"
            "orjson.OPT_SORT_KEYS`` for those); ``default=`` may be passed by keyword\n"
            "or positionally.  It rejects some inputs stdlib accepts.  A blind\n"
            "``json.``→``orjson.`` swap silently changes\n"
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
        autofixable=True,
        orthogonal_gate="skip",
        since="0.51.0",
        rationale=(
            "A worker volume declared in atlan.yaml's deploy block without "
            "pvc-autoresizer annotations is a fixed-size PVC: it never grows, so it "
            "must be sized up front for the largest run any tenant makes, and every "
            "pod that mounts it gets that full size whether its own run needs it or "
            "not. A review of connector worker disks found a connector giving every "
            "worker a fixed volume more than ten times larger than its largest "
            "normal run used. The connector on the fleet's smallest worker disk "
            "declares 8Gi that grows by 8Gi when half of it is used, up to 192Gi "
            "(atlan-snowflake-app contract/app.pkl). WARN because a fixed size is a "
            "cost and headroom trade-off rather than a broken deploy: the "
            "annotations can be added mechanically, but how far the base can come "
            "down depends on the app's own disk use."
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
            "tenant makes, and every pod that mounts it gets the full size.\n"
            "\n"
            "**Fix, in two steps.**  The first is mechanical: add whichever of the\n"
            "three annotations is missing, keep any already present, and leave the\n"
            "declared ``size`` alone — ``storage_limit: 192Gi`` (twice the declared\n"
            "size when that is already 192Gi or more, since a limit at or below the\n"
            'base never lets the volume grow), ``threshold: "50%"`` and\n'
            "``increase: 8Gi``:\n"
            "\n"
            ".. code-block:: yaml\n"
            "\n"
            "    storage:\n"
            "      - name: local-data\n"
            "        mountPath: /app/local\n"
            "        size: 100Gi\n"
            "        annotations:\n"
            "          resize.topolvm.io/storage_limit: 192Gi\n"
            '          resize.topolvm.io/threshold: "50%"\n'
            "          resize.topolvm.io/increase: 8Gi\n"
            "\n"
            "The second is not: once the app is clean on P060–P062 and O010, lower\n"
            "the base toward ``8Gi`` from each pod's 30-day disk peak, so that about\n"
            "1% of pods need to grow (roughly the p99 peak).\n"
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
            '          ["size"] = "100Gi"\n'
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
            "The scan is line-based (no YAML dependency) and reads block-style\n"
            "sequences in both layouts the fleet uses; a flow-style sequence, a\n"
            "YAML anchor or merge key, per-pool storage under ``workerPools`` and\n"
            "raw ``extraVolumes`` PVC templates are not graded.  Suppress with\n"
            "``# conformance: ignore[O009] <reason>`` on the entry's ``-`` line or\n"
            "the line above it — possible only in a hand-written ``atlan.yaml``,\n"
            "since regeneration drops comments from a generated one.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/optimizations.md#o009",
        remediation_reference=RemediationReference(
            kind=RemediationKind.PRESCRIPTION,
            target="programs/areas/optimizations.prose.md",
        ),
    ),
    RuleDefinition(
        id="O010",
        canonical_reference=(
            "application_sdk/storage/transfer.py — `_upload_from_store` copies a "
            "single object between stores through a temp file it unlinks in a "
            "`finally`, and the SDK's cross-store hand-off runs it per object, so "
            "moving a prefix from one store to another never stages the whole "
            "prefix on the worker. No reference app calls download_prefix."
        ),
        scope=RuleScope.APP,
        name="WholePrefixStagedOnDisk",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="worker-disk",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.51.0",
        rationale=(
            "download_prefix followed by upload_prefix of the same directory puts "
            "every object under the prefix on the worker's disk before any of it "
            "leaves, so the step's disk need grows with the largest prefix any "
            "tenant has instead of with a batch. Moving data across stores through "
            "the worker is expected — a self-deployed runtime hands data from the "
            "customer's deployment store to Atlan's upstream store — but staging "
            "the whole prefix to do it is not needed. The connector on the fleet's "
            "smallest worker disk writes and ships one bounded slice at a time "
            "(256 MiB of decoded input per slice, uploaded with upload_prefix and "
            "removed before the next: atlan-snowflake-app "
            "app/pipeline/crawler/duckdb_transform.py). WARN because some steps "
            "need every file at once (a merge, a baseline build), and only the "
            "code's owner can say which."
        ),
        short_description=(
            "download_prefix() then upload_prefix() of the same local tree stages a "
            "whole prefix on the worker's disk — move it in bounded batches"
        ),
        full_description=(
            "Flags an SDK ``upload_prefix`` call whose local directory was filled\n"
            "by a ``download_prefix`` earlier in the same function — the same\n"
            "directory, one under it, or one holding it — so every object under the\n"
            "prefix sits on the worker's disk at once.  The finding sits on the\n"
            "``upload_prefix`` call and names the ``download_prefix`` line.  Calls\n"
            "resolve through import bindings, like P060.\n"
            "\n"
            "Not flagged: moving data across stores, which the SDK expects; a\n"
            "per-file move (``download_file`` → ``upload_file`` → delete); the\n"
            "SDK's own cross-store hand-off (``App.upload`` / ``App.upload_refs``\n"
            "and the ``application_sdk.storage.transfer`` helpers behind them,\n"
            "which move one object at a time); and a loop that downloads, uploads\n"
            "and removes its local batch in the same iteration, which is bounded\n"
            "by the batch.\n"
            "\n"
            "Fix, in order of preference:\n"
            "\n"
            "1. **Bounded batches.**  List the keys, group them into batches capped\n"
            "   by bytes (256 MiB slices are a proven size), and per batch download,\n"
            "   upload — the bulk helpers keep their ``max_concurrency=4`` within\n"
            "   each batch — and delete the batch before the next.\n"
            "2. **A bounded pipeline.**  Move single files with about 4 in flight,\n"
            "   each download → upload → delete, matching the bulk helpers'\n"
            "   ``max_concurrency=4`` rather than a strictly serial loop (up to 4x\n"
            "   slower on many small files).  It overlaps download and upload and\n"
            "   holds only a few files on disk.\n"
            "\n"
            "Only when both calls provably use the same store (both omit\n"
            "``store=``, or pass the same expression) is a copy inside the store\n"
            "worth asking about; the message says when.  Not a mechanical rewrite:\n"
            "a step that needs every file at once (a merge, a baseline build) is a\n"
            "deliberate staging — suppress with\n"
            "``# conformance: ignore[O010] <reason>`` naming why.\n"
        ),
        rule_interactions=(
            "P060: removing the staged directory after the upload clears P060 but "
            "not this rule, which stays until the move is bounded."
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/optimizations.md#o010",
        remediation_reference=RemediationReference(
            kind=RemediationKind.GUIDE,
            target="programs/areas/optimizations.prose.md",
        ),
    ),
)
