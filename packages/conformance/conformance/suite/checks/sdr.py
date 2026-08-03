"""P-series SDR-readiness checks (P029, P030, P037, P038, P039, P041).

Cross-artifact checks that gate on ``self_deployed_runtime: true`` in
``atlan.yaml`` and verify six structural invariants:

* ``P029`` — an agent extraction manifest under ``app/generated/`` must surface
  ``agent_json`` AND ``extraction_method`` at the TOP LEVEL of
  ``dag.extract.inputs.args`` (that is the shape the platform reads to derive the
  agent queue + credential-routing spec at dispatch). Nesting them only under
  ``args.metadata`` strands the agent extraction on the cloud queue
  (atlan-tableau-app / atlan-snowflake-app); omitting them entirely is the
  MSSQL silent-zero-asset regression (atlan-mssql-app#177). Non-agent
  entrypoints (miner/QI, ``clean`` — no ``{{agent-json}}`` placeholder) are exempt.

* ``P030`` — at least one Python source file (outside ``tests/``) must contain
  a ``self.upload(`` call so the ``ENABLE_ATLAN_UPLOAD`` path is reachable.
  Without it extraction "passes" but no assets transfer to the Atlan tenant
  bucket in SDR deployments.  Delegating to SDK ``SqlApp.run()`` does NOT
  satisfy this — ``run()`` persists to the deployment store only, while the
  publish stage reads the tenant bucket.  A defined custom
  ``upload_to_atlan`` bridge counts as an upload path, but a bridge whose
  body performs no storage/store-transfer call is a **no-op stub** and is
  flagged specifically (the shape fleet remediation found behind a real
  silent-zero-asset publish).  Only applies to apps that actually have a
  publish stage: an app whose ``contract/app.pkl`` sets
  ``pipeline.publish = null`` compiles to a ``manifest.json`` with no
  ``dag.publish`` node, and has nowhere for ``self.upload()`` to hand
  extracted assets off to — P030 is skipped for those apps.

* ``P037`` — an SDR app that resolves source credentials with a *custom*,
  GUID-only path (a hand-rolled vault read + ``resolve_credential_raw`` or a
  bare ``CredentialRef(credential_guid=...)`` construction) but NEVER routes
  through an agent-aware resolver entry point (``CredentialRef.resolve`` /
  ``CredentialRef.from_workflow_args`` / an ``agent_spec``-carrying ref).  Its
  manifest can be P029-clean and ``agent_json`` forwarded, yet the connector
  code ignores it and resolves strictly by ``credential_guid`` — so in agent
  (SDR) mode credentials never resolve and it writes zero assets
  (observed for a table-format connector in fleet testing).  WARN.

* ``P038`` — an SDR app that roots its object-store output path/prefix
  (``artifacts/apps/<identity>/...``) from a *workflow-input* ``application_name``
  field (contract default ``""``) instead of the SDK app identity
  (``APPLICATION_NAME`` / ``self._app_name``).  Because AE forwards only
  manifest-declared args, that field stays empty and artifacts land under a
  mis-rooted path (``artifacts/apps//workflows/...`` — empty app segment), so
  ``self.upload()`` succeeds but 0 assets publish (observed for a document-store
  connector in fleet testing).
  P030 passes these apps (upload IS called); P038 catches the wrong-root case.
  WARN.

* ``P039`` — an SDR app whose generated manifest declares ``{{agent-json}}`` at
  the extract-args top level (so P029 passes), but whose generated extract-input
  contract model (``AppInputContract`` in a generated ``_input.py``) subclasses
  the bare ``Input`` base, declares no ``agent_json`` field, and rejects extra
  fields — so Pydantic silently DROPS the forwarded ``agent_json`` and the
  extract input resolves no credentials (``PipelineContractError`` / 0 assets;
  observed for a BI connector in fleet testing).  Contracts that subclass the SDK
  ``*ExtractionInput`` family (which declares ``agent_json``) or set
  ``allow_unbounded_fields=True`` / ``extra="allow"`` are exempt.  WARN.

* ``P041`` — an SDR app that unconditionally sets
  ``preflight_gate_mode = "hard"``.  In agent mode, preflight checks that
  depend on non-secret config (a database name for a schema-existence check)
  fail spuriously because customers rightly mirror only secrets into the
  secret store — a hard gate then aborts the whole workflow (observed for a
  query-engine connector in fleet testing).  A run-mode-differentiated
  conditional (``"soft" if ENABLE_ATLAN_UPLOAD else "hard"``) is not
  flagged.  WARN (suggest-only).

P026–P028 are reserved by a concurrent PR (GetattrOnTypedContractField,
AppStateAsCrossTaskChannel, ManualQualifiedNameFString — PR #2417).

All are APP-scoped: the SDK itself never declares ``self_deployed_runtime``
and is always skipped.

``scan_path`` is a no-op — SDR checks are cross-artifact and require the full
path list plus the repo root.  The runner must call ``scan_all``.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

from conformance.suite.checks._ast_common import discover, make_cli_main
from conformance.suite.schema.findings import Finding

SERIES = "P"
RULE_P029 = "P029"
RULE_P030 = "P030"
RULE_P037 = "P037"
RULE_P038 = "P038"
RULE_P039 = "P039"
RULE_P041 = "P041"

_SDR_FLAG_RE = re.compile(
    r"^self_deployed_runtime:\s*(true|false)\b",
    re.MULTILINE | re.IGNORECASE,
)

__all__ = ["SERIES", "discover", "main", "scan_all", "scan_path"]


def _is_sdr_app(root: Path) -> bool:
    """Return True when atlan.yaml declares self_deployed_runtime: true."""
    atlan_yaml = root / "atlan.yaml"
    if not atlan_yaml.is_file():
        return False
    try:
        text = atlan_yaml.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    m = _SDR_FLAG_RE.search(text)
    return m is not None and m.group(1).lower() == "true"


def _discover_manifests(root: Path) -> list[Path]:
    """All app/generated/manifest.json files: single-entrypoint or per-entrypoint."""
    generated = root / "app" / "generated"
    if not generated.is_dir():
        return []

    manifests: list[Path] = []
    root_manifest = generated / "manifest.json"
    if root_manifest.is_file():
        manifests.append(root_manifest)
    else:
        for child in sorted(generated.iterdir()):
            if child.is_dir():
                m = child / "manifest.json"
                if m.is_file():
                    manifests.append(m)
    return manifests


def _app_has_publish_stage(manifests: list[Path]) -> bool:
    """True unless every manifest structurally opts out of a publish stage.

    ``contract/app.pkl``'s ``pipeline.publish = null`` (an app-level opt-out of
    the publish pipeline stage) compiles to a manifest.json whose ``dag`` has
    no ``publish`` node. An app with no publish stage has nowhere for
    ``self.upload()`` to hand extracted assets off to, so P030 does not apply.

    No manifests at all (not yet generated) or an unparseable manifest means
    we cannot establish the opt-out, so this defaults to True (P030 still
    applies) rather than silently exempting an app we can't actually inspect.
    """
    if not manifests:
        return True
    for manifest_path in manifests:
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return True
        if (data.get("dag") or {}).get("publish") is not None:
            return True
    return False


#: Placeholder the Argo/AE template substitutes with the credential-routing spec
#: at dispatch. Its presence anywhere in an extract node's args marks that node as
#: an *agent* extraction (subject to the routing contract) vs. a miner/QI or
#: "clean" entrypoint that never resolves source credentials.
_AGENT_JSON_PLACEHOLDER = "{{agent-json}}"

#: Fields the platform reads at the extract-args TOP LEVEL to derive the agent
#: task queue (atlan-<agent-name>) and fill the credential-routing spec. Nesting
#: them only under args.metadata means the platform can't see them.
_REQUIRED_TOP_LEVEL_AGENT_FIELDS = ("agent_json", "extraction_method")


def _carries_agent_routing(obj: object) -> bool:
    """Whether the ``{{agent-json}}`` routing placeholder appears anywhere in
    ``obj`` (any depth) — the signal that an extract node is an agent extraction.
    """
    if isinstance(obj, dict):
        return any(_carries_agent_routing(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_carries_agent_routing(x) for x in obj)
    return obj == _AGENT_JSON_PLACEHOLDER


def _check_p029(manifests: list[Path], root: Path) -> list[Finding]:
    """P029: an SDR app's agent extraction manifests must surface ``agent_json``
    AND ``extraction_method`` at the TOP LEVEL of ``dag.extract.inputs.args``.

    Two failure modes are caught:

    * *misplaced / partial* — an agent-capable manifest (its extract args carry
      the ``{{agent-json}}`` placeholder) that nests the fields only under
      ``args.metadata`` or omits ``extraction_method``. The platform can't derive
      the agent queue or the credential-routing spec from them, so the agent
      extraction strands on the cloud queue / runs with no credentials
      (atlan-tableau-app, atlan-snowflake-app).
    * *missing entirely* — an SDR app whose generated manifests declare NO agent
      routing at all (the atlan-mssql-app#177 silent-zero-asset regression).

    A non-agent entrypoint (miner/QI, ``clean``) whose extract args carry no
    ``{{agent-json}}`` placeholder is exempt — it never resolves source
    credentials, so the contract does not apply to it.
    """
    findings: list[Finding] = []
    agent_capable_seen = False
    parsed_any = False
    for manifest_path in manifests:
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        parsed_any = True

        try:
            rel = manifest_path.relative_to(root)
        except ValueError:
            rel = manifest_path
        rel_str = str(rel)

        dag = data.get("dag", {})
        extract = dag.get("extract", {})
        inputs = extract.get("inputs", {})
        args = inputs.get("args", {})
        if not isinstance(args, dict):
            args = {}

        # Exempt non-agent entrypoints (miner/QI, clean) — no routing placeholder.
        if not _carries_agent_routing(args):
            continue
        agent_capable_seen = True

        missing = [f for f in _REQUIRED_TOP_LEVEL_AGENT_FIELDS if f not in args]
        if missing:
            findings.append(
                Finding(
                    rule_id=RULE_P029,
                    file=rel_str,
                    line=1,
                    column=1,
                    message=(
                        f"{rel_str}: manifest.json must surface {missing} at the TOP "
                        "LEVEL of dag.extract.inputs.args. The platform (Heracles/AE) "
                        "derives the agent task queue and fills the credential-routing "
                        "spec from these top-level fields at dispatch; nesting them "
                        "only under args.metadata (or omitting extraction_method) "
                        "strands the agent extraction on the cloud queue / leaves it "
                        "with no credentials — the workflow reports 'success' but "
                        "writes zero assets. Surface them at the args top level in "
                        "contract/app.pkl (keep under metadata too if the connector "
                        "reads there) and regenerate."
                    ),
                )
            )

    # App-level: an SDR app with at least one parseable manifest but NO
    # agent-capable extraction node declares no agent routing anywhere — the
    # missing-slot regression (#177). Gated on parsed_any so an unparseable
    # manifest (which we skip) doesn't masquerade as "no agent routing".
    if parsed_any and not agent_capable_seen:
        findings.append(
            Finding(
                rule_id=RULE_P029,
                file="app/generated/",
                line=1,
                column=1,
                message=(
                    "No generated manifest.json under app/generated/ declares agent "
                    "routing (the {{agent-json}} slot in dag.extract.inputs.args). "
                    "In SDR mode the extraction agent then receives no credentials "
                    "and writes zero assets — a silent failure invisible to "
                    "status-only pipelines (atlan-mssql-app#177). Add agent_json + "
                    "extraction_method to the extract inputs in contract/app.pkl and "
                    "regenerate."
                ),
            )
        )

    return findings


#: Method names an app uses for a custom deployment-store → tenant-bucket
#: transfer bridge in place of the SDK-standard ``self.upload()``.
_UPLOAD_BRIDGE_METHOD_NAMES = frozenset({"upload_to_atlan"})

#: Verbs that, as a whole ``_``-separated token of the callee name, indicate a
#: real storage/store transfer (ObjectStore/StorageClient uploads, file copies,
#: put-object calls, store-to-store migrations).
#:
#: Matched on token boundaries, never as a bare substring: ``put`` is a
#: substring of ``compute_summary``, ``output_stats`` and ``get_inputs``, and
#: accepting those would let a genuine no-op stub containing any of them clear
#: the finding — the exact silent-zero class this rule exists to catch.
#: Verbs that mean "move bytes to a store" on their own, whatever the receiver.
_UNAMBIGUOUS_TRANSFER_VERBS = frozenset({"upload"})

#: Verbs that are storage-shaped only in context.  ``put_metric_data``,
#: ``migrate_schema``, ``copy_on_write``, ``copy_paste``, ``transfer_encoding``
#: and ``push_job`` are all compound names carrying one of these and none moves
#: bytes anywhere — so a compound name must NOT clear the finding on its own.
#: These require the CALL to name a recognised store (receiver or a keyword
#: argument), at any token count.
_AMBIGUOUS_TRANSFER_VERBS = frozenset(
    {"copy", "transfer", "migrate", "put", "push", "sync", "write", "send"}
)

_STORAGE_TRANSFER_VERBS = _UNAMBIGUOUS_TRANSFER_VERBS | _AMBIGUOUS_TRANSFER_VERBS

#: A *bare* verb (``put``, ``copy``, ``sync``, …) says nothing on its own —
#: ``self._metrics.put(...)``, ``buffer.copy()`` and ``requests.put(url)`` are
#: not transfers.  For those the receiver has to name a recognised store.
_STORE_RECEIVER_MARKERS = (
    "store",
    "storage",
    "bucket",
    "objectstore",
    "s3",
    "gcs",
    "blob",
    "container",
)


def _names_a_store(identifier: str) -> bool:
    """Whether one identifier segment names a store.

    Whole-segment or suffix match, never a bare substring: ``_object_store`` and
    ``upstream_store`` are stores, ``_store_of_names`` is a registry that merely
    starts with the word.
    """
    seg = identifier.strip("_").lower()
    return any(
        seg == marker or seg.endswith(marker) for marker in _STORE_RECEIVER_MARKERS
    )


def _call_names_a_store(node: ast.Call) -> bool:
    """Whether the call references a recognised store — by receiver or argument.

    The receiver chain is the usual signal (``self._object_store.upload_prefix``).
    Keyword arguments matter too, because the SDK's storage helpers are called
    as bare functions: every green fleet bridge does
    ``upload_file(key, tmp, store=upstream_store)`` /
    ``storage_upload_file(..., store=...)``, which has no receiver at all.
    """
    parts: list[str] = []
    cur: ast.AST | None = node.func
    while cur is not None:
        if isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        elif isinstance(cur, ast.Name):
            parts.append(cur.id)
            cur = None
        elif isinstance(cur, ast.Call):
            cur = cur.func
        else:
            cur = None
    # Drop the callee itself — `upload_file` must not vouch for itself.
    receiver_parts = parts[1:] if len(parts) > 1 else []
    if any(_names_a_store(part) for part in receiver_parts):
        return True

    for kw in node.keywords:
        if kw.arg and _names_a_store(kw.arg):
            return True
        value = kw.value
        if isinstance(value, ast.Name) and _names_a_store(value.id):
            return True
        if isinstance(value, ast.Attribute) and _names_a_store(value.attr):
            return True
    return False


def _is_storage_transfer_call(node: ast.Call) -> bool:
    """Whether *node* is a real storage/store-transfer call.

    Only the bare ``upload(...)`` / ``self.upload(...)`` — the SDK-standard
    bridge call — clears on the name alone.  **Every** other name carrying a
    transfer verb, compound or not, must additionally have the call reference a
    store (see :func:`_call_names_a_store`).

    That matters in both directions.  Clearing on any compound name containing a
    verb let ``self._log.upload_metrics(...)``, ``self._telemetry.upload_stats()``,
    ``put_metric_data``, ``migrate_schema``, ``copy_on_write``, ``copy_paste``,
    ``transfer_encoding`` and ``push_job`` all pass a no-op stub.  Requiring a
    *receiver* alone would misreport the green fleet bridges, which call the SDK
    storage helpers as bare functions — ``upload_file(key, tmp, store=...)`` has
    no receiver at all — which is why the keyword arguments count too.
    """
    func = node.func
    if isinstance(func, ast.Attribute):
        name = func.attr
    elif isinstance(func, ast.Name):
        name = func.id
    else:
        return False

    tokens = set(t for t in name.lower().split("_") if t)
    if not (tokens & (_UNAMBIGUOUS_TRANSFER_VERBS | _AMBIGUOUS_TRANSFER_VERBS)):
        return False
    if tokens == _UNAMBIGUOUS_TRANSFER_VERBS:
        return True  # the bare `upload(...)` / `self.upload(...)` SDK call
    # A COMPOUND name carrying `upload` proves nothing on its own either:
    # `self._log.upload_metrics(...)` and `self._telemetry.upload_stats()` are
    # not transfers. Everything but the bare name goes through the receiver.
    # An ambiguous verb says nothing on its own at ANY token count: compound
    # names like `put_metric_data`, `migrate_schema`, `copy_on_write`,
    # `copy_paste`, `transfer_encoding` and `push_job` are not storage. The
    # receiver has to name a store.
    return _call_names_a_store(node)


def _is_abstract_body(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether the body is a pure declaration (abstract / placeholder).

    ``raise NotImplementedError``, ``pass`` and ``...`` (with an optional
    docstring) declare the contract for a subclass to implement; grading them as
    no-op stubs puts a permanent spurious finding on every connector using the
    declare-abstract/implement-in-subclass idiom.  They are excluded from the
    bridge list entirely and fall through to the generic absence check, which
    still fires correctly on an app carrying *only* the abstract stub.
    """
    body = list(node.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
    ):
        if isinstance(body[0].value.value, str):
            body = body[1:]  # drop the docstring
    if not body:
        return True
    for stmt in body:
        if isinstance(stmt, ast.Pass):
            continue
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            if stmt.value.value is Ellipsis:
                continue
        if isinstance(stmt, ast.Raise):
            exc = stmt.exc
            if isinstance(exc, ast.Call):
                exc = exc.func
            if isinstance(exc, ast.Name) and exc.id == "NotImplementedError":
                continue
            if isinstance(exc, ast.Attribute) and exc.attr == "NotImplementedError":
                continue
        return False
    return True


def _self_delegate_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Names of ``self.x(...)`` / ``cls.x(...)`` calls made in *node*'s body."""
    out: set[str] = set()
    for inner in ast.walk(node):
        if (
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Attribute)
            and isinstance(inner.func.value, ast.Name)
            and inner.func.value.id in ("self", "cls")
        ):
            out.add(inner.func.attr)
    return out


#: How many ``self.x()`` hops to follow inside one class before giving up.
#: A real bridge that fans out through a couple of private helpers is ordinary;
#: unbounded recursion on a hostile file is not.
_MAX_DELEGATION_DEPTH = 4


def _delegates_to_transfer(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    table: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
) -> bool:
    """Whether *node* reaches a storage transfer through same-class delegation.

    Resolved transitively (depth-capped, cycle-safe) so a bridge that fans out
    ``upload_to_atlan`` -> ``self._helper1()`` -> ``self._helper2()`` ->
    ``self._object_store.upload_prefix()`` is graded on what it actually does.

    An unresolvable callee — a helper inherited from a base class in another
    file — is deliberately NOT assumed to transfer: treating every
    ``self.x(...)`` as delegation would let ``compute_summary`` /
    ``output_stats`` / ``get_inputs`` clear the finding, reopening the false
    negative the token match exists to close. That limit is documented on
    :func:`_find_upload_bridges`.
    """
    seen: set[str] = {node.name}
    frontier = [(node, 0)]
    while frontier:
        current, depth = frontier.pop()
        if depth >= _MAX_DELEGATION_DEPTH:
            continue
        for helper_name in _self_delegate_names(current):
            if helper_name in seen:
                continue
            seen.add(helper_name)
            helper = table.get(helper_name)
            if helper is None:
                continue
            if any(
                isinstance(inner, ast.Call) and _is_storage_transfer_call(inner)
                for inner in ast.walk(helper)
            ):
                return True
            frontier.append((helper, depth + 1))
    return False


def _find_upload_bridges(
    paths: list[Path], root: Path
) -> list[tuple[str, int, str, bool]]:
    """Locate custom upload-bridge method definitions across the app source.

    Returns ``(rel_file, lineno, name, is_noop)`` per defined bridge method,
    where ``is_noop`` is True when neither the method body nor a same-class
    method it delegates to performs a storage/store-transfer call — the no-op
    stub shape.

    Abstract declarations (``raise NotImplementedError`` / ``pass`` / ``...``)
    are not bridges and are skipped entirely.  Delegation is resolved one level
    into the same class, so a bridge whose body is
    ``await self._relay_to_tenant_bucket(prefix)`` is graded on what the helper
    actually does rather than on whether its *name* happens to contain a verb.

    Known limit: a helper inherited from a base class in another file cannot be
    resolved, and the bridge then reads as a no-op stub.  Assuming the opposite
    would let any ``self.x(...)`` clear the finding — including
    ``self.compute_summary()`` and ``self.get_inputs()`` — which is the false
    negative this rule exists to prevent, and the standard inline directive
    carries the author's justification.
    """
    bridges: list[tuple[str, int, str, bool]] = []
    for path in paths:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, ValueError):
            continue
        try:
            rel = str(path.relative_to(root))
        except ValueError:
            rel = str(path)

        # Method tables per class, for the one-level delegation lookback.
        siblings: dict[int, dict[str, ast.FunctionDef | ast.AsyncFunctionDef]] = {}
        for cls in ast.walk(tree):
            if not isinstance(cls, ast.ClassDef):
                continue
            table: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
            for item in cls.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    table[item.name] = item
            for item in cls.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    siblings[id(item)] = table

        for node in ast.walk(tree):
            if not (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in _UPLOAD_BRIDGE_METHOD_NAMES
            ):
                continue
            if _is_abstract_body(node):
                continue

            has_transfer = any(
                isinstance(inner, ast.Call) and _is_storage_transfer_call(inner)
                for inner in ast.walk(node)
            )
            if not has_transfer:
                has_transfer = _delegates_to_transfer(node, siblings.get(id(node), {}))

            bridges.append((rel, node.lineno, node.name, not has_transfer))
    return bridges


def _has_self_upload_call(paths: list[Path]) -> bool:
    """Whether any app source file makes a real ``self.upload(...)`` call.

    Detected on the AST, not as text: a raw ``"self.upload(" in text`` search
    cannot tell a call from a *mention*, so a comment or docstring naming
    ``self.upload(`` would clear the absence finding.  The population this rule
    targets is precisely where such a comment is likely — fleet remediation
    found a stub whose comment claimed the publish stage owned the transfer.
    """
    for path in paths:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, ValueError):
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "upload"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "self"
            ):
                return True
    return False


def _check_p030(paths: list[Path], root: Path) -> list[Finding]:
    """P030: the tenant-bucket upload path must be structurally reachable.

    Two shapes are checked:

    * **No upload at all** — no source file makes a real ``self.upload(...)``
      call (matched on the AST, so a comment or docstring mentioning it does
      not count) and no custom upload-bridge method (``upload_to_atlan``) is
      defined.  The ENABLE_ATLAN_UPLOAD path is unreachable; delegating to SDK
      ``SqlApp.run()`` does NOT count (it persists to the deployment store
      only — publish reads the tenant bucket).
    * **No-op bridge stub** — an ``upload_to_atlan`` method IS defined but
      neither its body nor a same-class method it delegates to performs a
      storage/store-transfer call.  The stub looks like a bridge at review time
      but moves no bytes; fleet remediation found one whose comment claimed the
      publish stage owned the transfer.  Abstract declarations
      (``raise NotImplementedError``/``pass``/``...``) are not stubs and are
      excluded — a subclass may well implement the real thing.

    An app with ``self.upload(`` somewhere is not flagged for absence, but a
    no-op stub alongside it is still flagged (the stub is dead weight that
    masks the real transfer path).  A bridge whose body DOES transfer is
    reported with the standard absence finding only when ``self.upload(`` is
    also missing — the message directs the reviewer to prove it with a green
    full-DAG e2e before treating the finding as a false positive.
    """
    has_self_upload = _has_self_upload_call(paths)
    bridges = _find_upload_bridges(paths, root)
    findings: list[Finding] = []

    # No-op stubs are flagged regardless of self.upload presence: a stub that
    # moves no bytes is either the (real) silent-zero-asset bug or dead code
    # masking the real transfer path.
    for rel, lineno, name, is_noop in bridges:
        if not is_noop:
            continue
        findings.append(
            Finding(
                rule_id=RULE_P030,
                file=rel,
                line=lineno,
                column=1,
                message=(
                    f"{rel}:{lineno}: '{name}' is defined but its body performs no "
                    "storage/store-transfer call — a no-op stub. In SDR mode the "
                    "extracted assets never reach the Atlan tenant bucket, so the "
                    "workflow reports 'success' with 0 assets published. Implement a "
                    "key-preserving deployment-store→tenant-bucket transfer (preserve "
                    "the workflows/{workflow_id}/{run_id}/ key layout) or call "
                    "self.upload(...), and verify with a green full-DAG e2e proving "
                    "assets land in Atlas."
                ),
            )
        )

    if has_self_upload or bridges:
        # With self.upload present there is no absence.  With a bridge present
        # the story is already told: a transferring bridge is the documented
        # false-positive shape (whether it preserves the key layout end-to-end
        # is beyond static reach — the full-DAG e2e is the arbiter, see rule
        # docs), and a no-op stub already carries its own, sharper finding
        # above — a second app-level absence finding would double-report it.
        return findings

    findings.append(
        Finding(
            rule_id=RULE_P030,
            file="atlan.yaml",
            line=1,
            column=1,
            message=(
                "No self.upload() call (and no custom upload_to_atlan transfer "
                "bridge) found in any app source file. In SDR mode "
                "ENABLE_ATLAN_UPLOAD gates whether extracted assets are transferred "
                "to the Atlan tenant bucket — if the gate is structurally unreachable "
                "the workflow completes with status 'success' but no assets land in "
                "the bucket. Delegating to SDK SqlApp.run() does NOT satisfy this: "
                "run() persists to the deployment store only, while publish reads "
                "the tenant bucket. Add await self.upload(...) to the entrypoint or "
                "run() method (or wire a key-preserving upload_to_atlan bridge into "
                "every entrypoint), and never mark this finding a false positive "
                "without a green full-DAG e2e proving assets land in Atlas."
            ),
        )
    )
    return findings


# ── P037: agent_json ignored by a custom GUID-only credential path ──────────

#: SDK entry points that consume the full workflow input/args and therefore pick
#: up ``agent_json`` (the agent-mode credential-routing spec).  ``resolve`` is
#: additionally gated on the ``CredentialRef`` receiver; the others are
#: distinctive enough on the method name alone.
_AGENT_AWARE_RESOLVER_ATTRS = frozenset(
    {"from_workflow_args", "resolve_agent_credential", "resolve_agent_json"}
)


def _classify_credential_calls(tree: ast.AST) -> tuple[tuple[int, str] | None, bool]:
    """Scan one module AST for the two P037 signals.

    Returns ``(custom_site, agent_aware)`` where:

    * ``custom_site`` is the ``(lineno, callee)`` of the first *custom* credential
      resolution call — a bare ``CredentialRef(...)`` construction or a
      ``resolve_credential_raw(...)`` call — or ``None`` if there is none.
    * ``agent_aware`` is True if any *agent-aware* resolver entry point is called
      (``CredentialRef.resolve`` / ``CredentialRef.from_workflow_args`` /
      ``resolve_agent_credential`` / ``resolve_agent_json``) or a
      ``CredentialRef(...)`` is built with an ``agent_spec``/``agent_json`` kwarg.

    Using the AST (not text) keeps docstring/comment mentions of
    ``CredentialRef(...)`` from registering as real resolution calls.
    """
    custom_site: tuple[int, str] | None = None
    agent_aware = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Direct constructor: CredentialRef(...)
        if isinstance(func, ast.Name) and func.id == "CredentialRef":
            if custom_site is None:
                custom_site = (node.lineno, "CredentialRef(...)")
            if any(kw.arg in ("agent_spec", "agent_json") for kw in node.keywords):
                agent_aware = True
        elif isinstance(func, ast.Attribute):
            attr = func.attr
            if attr == "resolve_credential_raw":
                if custom_site is None:
                    custom_site = (node.lineno, "resolve_credential_raw(...)")
            elif attr in _AGENT_AWARE_RESOLVER_ATTRS:
                agent_aware = True
            elif (
                attr == "resolve"
                and isinstance(func.value, ast.Name)
                and func.value.id == "CredentialRef"
            ):
                agent_aware = True
    return custom_site, agent_aware


def _check_p037(paths: list[Path], root: Path) -> list[Finding]:
    """P037: custom GUID-only credential resolution that bypasses agent routing.

    App-level (a single finding).  Fires when the app performs custom credential
    resolution somewhere (a bare ``CredentialRef(...)`` construction or a
    ``resolve_credential_raw`` call) but NEVER routes through an agent-aware
    resolver entry point.  Such an app resolves strictly by ``credential_guid``,
    so ``agent_json`` is ignored and agent-mediated credentials never resolve in
    SDR mode — a silent zero-asset failure.

    Apps that rely on the SDK's transparent resolution (they build no
    ``CredentialRef`` and call no ``resolve_credential_raw``) are not gated in.
    Apps that DO call an agent-aware resolver (via ``CredentialRef.resolve`` or
    ``CredentialRef.from_workflow_args``) are exempt even when they also keep a
    GUID fallback.
    """
    first_custom: tuple[str, int, str] | None = None
    agent_aware_anywhere = False
    for path in paths:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, ValueError):
            continue
        custom_site, agent_aware = _classify_credential_calls(tree)
        if agent_aware:
            agent_aware_anywhere = True
        if custom_site is not None and first_custom is None:
            try:
                rel = str(path.relative_to(root))
            except ValueError:
                rel = str(path)
            first_custom = (rel, custom_site[0], custom_site[1])

    if first_custom is None or agent_aware_anywhere:
        return []

    rel, line, callee = first_custom
    return [
        Finding(
            rule_id=RULE_P037,
            file=rel,
            line=line,
            column=1,
            message=(
                f"{rel}:{line}: credentials are resolved via a custom GUID-only path "
                f"({callee}) and no agent-aware resolver "
                "(CredentialRef.resolve / CredentialRef.from_workflow_args) is called "
                "anywhere in the app. The manifest may forward agent_json, but code "
                "that resolves strictly by credential_guid ignores it, so in SDR "
                "(agent) mode credentials never resolve and the workflow writes zero "
                "assets while reporting 'success'. Route "
                "credential resolution through CredentialRef.resolve(input) or "
                "CredentialRef.from_workflow_args(workflow_args) — which consume "
                "agent_json and pick the agent vs. GUID route — keeping the direct "
                "credential_guid path only as a fallback."
            ),
        )
    ]


# ── P038: object-store prefix mis-rooted from an empty-defaulting input ──────

#: Object-store root convention. An ``artifacts/apps/{identity}/...`` prefix
#: whose ``{identity}`` segment comes from a workflow-input ``application_name``
#: field (contract default ``""``) mis-roots to ``artifacts/apps//...``.
_OBJECT_STORE_ROOT_LITERAL = "artifacts/apps"

#: The workflow-input field whose empty default causes the mis-rooting.
_APP_IDENTITY_INPUT_FIELD = "application_name"


def _is_app_name_input_read(node: ast.AST) -> bool:
    """Whether *node* reads the ``application_name`` field off a workflow input.

    Matches the three shapes an app uses to pull the field off ``input`` /
    ``input_data`` / ``workflow_args``:

    * ``<obj>.get("application_name", ...)``  (dict access with a default)
    * ``<obj>["application_name"]``           (dict subscript)
    * ``<name>.application_name``             (attribute access)

    The SDK app-identity constant is the bare ``Name`` ``APPLICATION_NAME`` — it
    is never spelled ``.application_name`` — so this never matches the correct
    rooting source.
    """
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == _APP_IDENTITY_INPUT_FIELD
    ):
        return True
    if isinstance(node, ast.Subscript):
        sl = node.slice
        if isinstance(sl, ast.Constant) and sl.value == _APP_IDENTITY_INPUT_FIELD:
            return True
    return bool(
        isinstance(node, ast.Attribute)
        and node.attr == _APP_IDENTITY_INPUT_FIELD
        and isinstance(node.value, ast.Name)
    )


def _joinedstr_literal(js: ast.JoinedStr) -> str:
    """The concatenation of the constant (literal) segments of an f-string."""
    return "".join(
        v.value
        for v in js.values
        if isinstance(v, ast.Constant) and isinstance(v.value, str)
    )


def _check_p038(paths: list[Path], root: Path) -> list[Finding]:
    """P038: object-store prefix rooted from an empty-defaulting input field.

    Flags an object-store path f-string (its literal segments contain
    ``artifacts/apps``) whose interpolated app-identity segment is derived from a
    workflow-input ``application_name`` read — directly, or via a local variable
    bound to one within the same function — rather than the SDK ``APPLICATION_NAME``
    constant / ``self._app_name``.

    Scope is per-function so taint does not leak between unrelated defs. Findings
    are de-duplicated by ``(file, line)``.
    """
    findings: list[Finding] = []
    seen: set[tuple[str, int]] = set()
    for path in paths:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, ValueError):
            continue
        try:
            rel = str(path.relative_to(root))
        except ValueError:
            rel = str(path)

        for fn in [
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]:
            # Locals bound to an application_name input read.
            tainted: set[str] = set()
            for n in ast.walk(fn):
                if (
                    isinstance(n, ast.Assign)
                    and len(n.targets) == 1
                    and isinstance(n.targets[0], ast.Name)
                    and _is_app_name_input_read(n.value)
                ):
                    tainted.add(n.targets[0].id)

            for js in [n for n in ast.walk(fn) if isinstance(n, ast.JoinedStr)]:
                if _OBJECT_STORE_ROOT_LITERAL not in _joinedstr_literal(js):
                    continue
                for fv in js.values:
                    if not isinstance(fv, ast.FormattedValue):
                        continue
                    val = fv.value
                    if (
                        isinstance(val, ast.Name) and val.id in tainted
                    ) or _is_app_name_input_read(val):
                        key = (rel, js.lineno)
                        if key in seen:
                            continue
                        seen.add(key)
                        findings.append(
                            Finding(
                                rule_id=RULE_P038,
                                file=rel,
                                line=js.lineno,
                                column=1,
                                message=(
                                    f"{rel}:{js.lineno}: object-store output path "
                                    "('artifacts/apps/...') is rooted from the "
                                    "workflow-input 'application_name' field, whose "
                                    "contract default is '' — AE forwards only "
                                    "manifest-declared args, so it stays empty and "
                                    "artifacts land under a mis-rooted path "
                                    "('artifacts/apps//workflows/...', empty app "
                                    "segment). self.upload() then succeeds but 0 "
                                    "assets publish. Root the "
                                    "prefix from the SDK app identity — APPLICATION_NAME "
                                    "/ self._app_name (or WORKFLOW_OUTPUT_PATH_TEMPLATE) "
                                    "— not from an empty-defaulting workflow arg."
                                ),
                            )
                        )
    return findings


# ── P039: agent_json silently dropped by the generated input contract ───────

#: The class name the Pkl generator emits for the extract-input contract model.
_GENERATED_INPUT_CLASS = "AppInputContract"


def _manifest_carries_agent_routing(manifest_path: Path) -> bool:
    """Whether a manifest's extract args carry the ``{{agent-json}}`` placeholder."""
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    dag = data.get("dag", {})
    args = dag.get("extract", {}).get("inputs", {}).get("args", {})
    return isinstance(args, dict) and _carries_agent_routing(args)


def _class_base_names(node: ast.ClassDef) -> list[str]:
    """Simple names of a class's bases (``Name.id`` / ``Attribute.attr``)."""
    names: list[str] = []
    for base in node.bases:
        if isinstance(base, ast.Name):
            names.append(base.id)
        elif isinstance(base, ast.Attribute):
            names.append(base.attr)
    return names


def _class_allows_extra_fields(node: ast.ClassDef) -> bool:
    """Whether the model opts out of Pydantic payload-safety (accepts extra keys).

    Recognises the three shapes an app uses: the ``allow_unbounded_fields=True``
    class keyword (the SDK's opt-out), a Pydantic ``extra="allow"`` class keyword,
    a ``model_config`` set to ``ConfigDict(extra="allow")`` / ``{"extra": "allow"}``,
    or a nested ``class Config: extra = "allow"``.
    """
    for kw in node.keywords:
        if (
            kw.arg == "allow_unbounded_fields"
            and isinstance(kw.value, ast.Constant)
            and kw.value.value is True
        ):
            return True
        if (
            kw.arg == "extra"
            and isinstance(kw.value, ast.Constant)
            and kw.value.value == "allow"
        ):
            return True
    for stmt in node.body:
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            target = stmt.targets[0] if isinstance(stmt, ast.Assign) else stmt.target
            if (
                isinstance(target, ast.Name)
                and target.id == "model_config"
                and stmt.value is not None
            ):
                value = stmt.value
                if isinstance(value, ast.Call):
                    for kw in value.keywords:
                        if (
                            kw.arg == "extra"
                            and isinstance(kw.value, ast.Constant)
                            and kw.value.value == "allow"
                        ):
                            return True
                if isinstance(value, ast.Dict):
                    for key, val in zip(value.keys, value.values):
                        if (
                            isinstance(key, ast.Constant)
                            and key.value == "extra"
                            and isinstance(val, ast.Constant)
                            and val.value == "allow"
                        ):
                            return True
        if isinstance(stmt, ast.ClassDef) and stmt.name == "Config":
            for inner in stmt.body:
                if isinstance(inner, ast.Assign):
                    for t in inner.targets:
                        if (
                            isinstance(t, ast.Name)
                            and t.id == "extra"
                            and isinstance(inner.value, ast.Constant)
                            and inner.value.value == "allow"
                        ):
                            return True
    return False


def _class_declares_agent_json(node: ast.ClassDef) -> bool:
    """Whether the class body declares an ``agent_json`` field."""
    for stmt in node.body:
        if (
            isinstance(stmt, ast.AnnAssign)
            and isinstance(stmt.target, ast.Name)
            and stmt.target.id == "agent_json"
        ):
            return True
        if isinstance(stmt, ast.Assign):
            for t in stmt.targets:
                if isinstance(t, ast.Name) and t.id == "agent_json":
                    return True
    return False


def _generated_input_contract_findings(path: Path, rel: str) -> list[tuple[int, bool]]:
    """Analyse the ``AppInputContract`` class in a generated ``_input.py``.

    Returns ``(lineno, unsafe)`` for each ``AppInputContract`` definition, where
    ``unsafe`` is True iff the model subclasses the bare ``Input`` base (not the
    ``*ExtractionInput`` family, which declares ``agent_json``), declares no
    ``agent_json`` field of its own, and does not accept extra fields — the shape
    that silently drops a forwarded ``agent_json``.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return []
    results: list[tuple[int, bool]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != _GENERATED_INPUT_CLASS:
            continue
        bases = _class_base_names(node)
        # The *ExtractionInput family (SDK templates) declares agent_json → safe.
        in_extraction_family = any(b.endswith("ExtractionInput") for b in bases)
        extends_bare_input = "Input" in bases
        safe = (
            in_extraction_family
            or _class_declares_agent_json(node)
            or _class_allows_extra_fields(node)
        )
        unsafe = extends_bare_input and not safe
        results.append((node.lineno, unsafe))
    return results


def _check_p039(manifests: list[Path], root: Path) -> list[Finding]:
    """P039: agent_json dropped by a closed generated extract-input contract.

    Precondition: at least one generated manifest declares agent routing (the
    ``{{agent-json}}`` placeholder in the extract args) — i.e. it is P029-clean on
    the manifest side. Fires when the generated extract-input contract model
    (``AppInputContract`` in a generated ``_input.py``) subclasses the bare
    ``Input`` base, declares no ``agent_json`` field, and rejects extra fields:
    Pydantic then silently drops the forwarded ``agent_json`` and the extract
    input resolves no credentials (``PipelineContractError`` / 0 assets;
    observed for a BI connector in fleet testing).

    Apps whose contract subclasses the SDK ``*ExtractionInput`` family (which
    declares ``agent_json``) or that set ``allow_unbounded_fields=True`` /
    ``extra="allow"`` are not flagged.
    """
    agent_manifests = [m for m in manifests if _manifest_carries_agent_routing(m)]
    if not agent_manifests:
        return []

    findings: list[Finding] = []
    inspected: set[Path] = set()
    for manifest_path in agent_manifests:
        sibling = manifest_path.parent / "_input.py"
        candidates = (
            [sibling]
            if sibling.is_file()
            else sorted((root / "app").rglob("_input.py"))
        )
        for input_py in candidates:
            if input_py in inspected:
                continue
            inspected.add(input_py)
            try:
                rel = str(input_py.relative_to(root))
            except ValueError:
                rel = str(input_py)
            for lineno, unsafe in _generated_input_contract_findings(input_py, rel):
                if not unsafe:
                    continue
                findings.append(
                    Finding(
                        rule_id=RULE_P039,
                        file=rel,
                        line=lineno,
                        column=1,
                        message=(
                            f"{rel}:{lineno}: the generated extract-input contract "
                            "'AppInputContract' subclasses the bare Input base, "
                            "declares no agent_json field, and rejects extra fields — "
                            "so the agent_json the platform forwards in SDR (agent) "
                            "mode is silently dropped by Pydantic. The extract input's "
                            "credential_ref is then None and extraction fails with "
                            "PipelineContractError / 0 assets, even though the manifest "
                            "declares {{agent-json}}. Declare "
                            "agent_json on the contract (in contract/app.pkl, "
                            "regenerated), subclass the SDK ExtractionInput family "
                            "(which declares it), or set allow_unbounded_fields=True."
                        ),
                    )
                )
    return findings


# ── P041: hard preflight gate in an SDR app ──────────────────────────────────

#: The SDK ``App`` class attribute that opts preflight verdicts into aborting
#: the run (``"hard"``) instead of logging-and-proceeding (``"soft"``).
_PREFLIGHT_GATE_MODE_ATTR = "preflight_gate_mode"


def _is_unconditional_hard(
    value: ast.AST, constants: dict[str, object] | None = None
) -> bool:
    """Whether *value* yields ``"hard"`` on the always-on / default path.

    Semantic rather than structural: exempting every non-``Constant`` node lets
    an *inverted* ternary (``"hard" if ENABLE_ATLAN_UPLOAD else "soft"`` — hard
    exactly when upload is on), a both-arms-hard ternary, and an env default of
    ``"hard"`` all escape, which are the very postures the rule audits.

    Safe is ``"hard"`` in the *else* arm only — the documented run-mode split
    ``"soft" if ENABLE_ATLAN_UPLOAD else "hard"``.  A nested conditional in an
    env-var default (``os.environ.get(VAR, "soft" if ... else "hard")``) is the
    same posture spelled through the override and stays silent too.
    """
    if isinstance(value, ast.Constant):
        return value.value == "hard"
    if isinstance(value, ast.IfExp):
        # Hard in the true arm (or in both) is unconditional-in-effect; hard
        # only in the else arm is the recommended run-mode-differentiated split.
        return _is_unconditional_hard(value.body, constants)
    if isinstance(value, ast.Call):
        # An env-var override: `os.environ.get("ATLAN_PREFLIGHT_GATE_MODE",
        # "hard")` is hard on every deployment that does not set the var.
        return any(
            _is_unconditional_hard(arg, constants)
            for arg in [*value.args[1:], *(kw.value for kw in value.keywords)]
        )
    if isinstance(value, ast.Name) and constants is not None:
        # A module-level `MODE = "hard"` indirection is still always hard;
        # only an unambiguous single binding is resolved (no flow analysis).
        return constants.get(value.id) == "hard"
    if isinstance(value, ast.BoolOp) and isinstance(value.op, ast.Or):
        # `os.environ.get(VAR) or "hard"` — the or-default idiom, semantically
        # identical to the `.get(VAR, "hard")` form above.
        return _is_unconditional_hard(value.values[-1], constants)
    return False


def _string_constants(body: list[ast.stmt]) -> dict[str, object]:
    """``NAME = "literal"`` bindings among the straight-line statements of *body*.

    Used for module bodies AND class bodies: ``preflight_gate_mode`` is
    conventionally a class attribute, so ``class C: MODE = "hard"`` followed by
    ``preflight_gate_mode = MODE`` in the same body is at least as natural as
    the module-level form.

    Deliberately not constant propagation — only direct children of *body* are
    considered, so control flow is straight-line and the last write wins. A name
    ever assigned a computed value is dropped rather than guessed at.
    """
    seen: dict[str, object] = {}
    poisoned: set[str] = set()
    for stmt in body:
        if not (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
        ):
            continue
        name = stmt.targets[0].id
        if isinstance(stmt.value, ast.Constant):
            # Straight-line statements only, so the last write wins:
            # `MODE = "soft"` then `MODE = "hard"` is deterministically hard.
            seen[name] = stmt.value.value
        else:
            poisoned.add(name)  # a computed value — do not guess at it
    return {k: v for k, v in seen.items() if k not in poisoned}


def _gate_target_names(targets: list[ast.expr]) -> list[str]:
    return [t.id for t in targets if isinstance(t, ast.Name)] + [
        t.attr for t in targets if isinstance(t, ast.Attribute)
    ]


def _gate_assignments(stmts: list[ast.stmt]) -> list[tuple[ast.stmt, ast.expr]]:
    """``(node, value)`` for each ``preflight_gate_mode`` assignment in *stmts*."""
    out: list[tuple[ast.stmt, ast.expr]] = []
    for stmt in stmts:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Assign):
                targets, value = list(node.targets), node.value
            elif isinstance(node, ast.AnnAssign):
                targets, value = [node.target], node.value
            else:
                continue
            if value is None:
                continue
            if _PREFLIGHT_GATE_MODE_ATTR in _gate_target_names(targets):
                out.append((node, value))
    return out


def _run_mode_split_exempt(
    tree: ast.Module, constants: dict[str, object] | None = None
) -> set[int]:
    """ids of gate assigns that are the ``else`` arm of an if/else run-mode split.

    ``if ENABLE_ATLAN_UPLOAD: ... = "soft"`` / ``else: ... = "hard"`` is the
    documented posture spelled across two statements instead of as a ternary —
    an ordinary style choice once the branches grow past one line — and must not
    be flagged.  The polarity check mirrors the ternary's: ``"hard"`` on the
    *true* arm is the inverted posture and still fires, as does both-arms-hard.
    """
    exempt: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.If) or not node.orelse:
            continue
        body = _gate_assignments(node.body)
        orelse = _gate_assignments(node.orelse)
        if not body or not orelse:
            continue
        if any(_is_unconditional_hard(value, constants) for _n, value in body):
            continue  # hard on the true arm — inverted/both-hard, keep firing
        for assign, value in orelse:
            if _is_unconditional_hard(value, constants):
                exempt.add(id(assign))
    return exempt


def _check_p041(paths: list[Path], root: Path) -> list[Finding]:
    """P041: an SDR app must not hard-code ``preflight_gate_mode = "hard"``.

    Flags an assignment to ``preflight_gate_mode`` (plain or annotated) whose
    value is ``"hard"`` on the always-on or default path — see
    :func:`_is_unconditional_hard`.  The documented run-mode-differentiated
    posture (``"soft" if ENABLE_ATLAN_UPLOAD else "hard"``, env-overridable via
    ``ATLAN_PREFLIGHT_GATE_MODE``) is never flagged, in either its ternary or
    its ``if``/``else`` spelling.
    """
    findings: list[Finding] = []
    for path in paths:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, ValueError):
            continue
        try:
            rel = str(path.relative_to(root))
        except ValueError:
            rel = str(path)
        module_constants = _string_constants(tree.body)
        # `preflight_gate_mode` is conventionally a class attribute, so a
        # same-class `MODE = "hard"` shadows the module-level one.
        class_constants: dict[int, dict[str, object]] = {}
        for cls in ast.walk(tree):
            if not isinstance(cls, ast.ClassDef):
                continue
            merged = {**module_constants, **_string_constants(cls.body)}
            for item in ast.walk(cls):
                class_constants[id(item)] = merged
        split_exempt = _run_mode_split_exempt(tree, module_constants)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
                value = node.value
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
                value = node.value
            else:
                continue
            if value is None:
                continue
            if _PREFLIGHT_GATE_MODE_ATTR not in _gate_target_names(targets):
                continue
            if id(node) in split_exempt:
                continue
            if _is_unconditional_hard(
                value, class_constants.get(id(node), module_constants)
            ):
                findings.append(
                    Finding(
                        rule_id=RULE_P041,
                        file=rel,
                        line=node.lineno,
                        column=1,
                        message=(
                            f"{rel}:{node.lineno}: preflight_gate_mode is "
                            "unconditionally 'hard' in an SDR app. In agent mode the "
                            "app resolves config through the customer's secret "
                            "store, and non-secret values (e.g. a database name for "
                            "a schema-existence check) are rightly not mirrored "
                            "there — the preflight check then fails spuriously and "
                            "the hard gate aborts the whole workflow. Derive the "
                            "gate from the run mode instead (e.g. "
                            "preflight_gate_mode = 'soft' if ENABLE_ATLAN_UPLOAD "
                            "else 'hard') and keep it env-overridable via "
                            "ATLAN_PREFLIGHT_GATE_MODE, until SDK-side run-mode-"
                            "differentiated gate enforcement lands."
                        ),
                    )
                )
    return findings


def scan_path(path: Path, root: Path) -> list[Finding]:  # noqa: ARG001
    """No-op: the SDR checks require cross-artifact analysis; use scan_all."""
    return []


def scan_all(paths: list[Path], root: Path) -> list[Finding]:
    """Check the SDR-readiness rules (P029, P030, P037–P039, P041) for the repo.

    Parameters
    ----------
    paths:
        Python source files to inspect (as returned by :func:`discover`).
        These are the files checked by P030 for a ``self.upload(`` call /
        upload-bridge shape, by P037 for the credential-resolution shape, by
        P038 for the object-store prefix rooting, and by P041 for the
        preflight-gate posture.
    root:
        Repo root — used to locate ``atlan.yaml`` and ``app/generated/`` (P039
        also inspects the generated ``_input.py`` contract models).
    """
    if not _is_sdr_app(root):
        return []

    manifests = _discover_manifests(root)

    findings: list[Finding] = []
    findings.extend(_check_p029(manifests, root))
    if _app_has_publish_stage(manifests):
        findings.extend(_check_p030(paths, root))
    findings.extend(_check_p037(paths, root))
    findings.extend(_check_p038(paths, root))
    findings.extend(_check_p039(manifests, root))
    findings.extend(_check_p041(paths, root))
    return findings


main = make_cli_main(
    scan_all=scan_all,
    description=(
        "SDR-readiness checks — manifest agent_json slot (P029), "
        "upload call presence / no-op bridge stubs (P030), agent-aware "
        "credential resolution (P037), object-store prefix rooting (P038), "
        "agent_json input-contract consumption (P039), and preflight-gate "
        "posture (P041)."
    ),
)

if __name__ == "__main__":
    sys.exit(main())
