"""Meta-tests for the P-series persistence-seam checks (P048/P049, CONNECT-1275).

P048 flags an app assembling the SDK-owned ``persistent-artifacts`` object-store
layout itself instead of deriving it from
``application_sdk.common.incremental.helpers.get_persistent_s3_prefix``; P049
flags a function that parses ``connection_qualified_name`` itself and raises
where the SDK warns and proceeds.

P048 matches the layout alone — there is no seam-import gate, and
``test_p048_fires_even_when_module_imports_the_seam`` is the contract.  P049 does
gate on delegation, but per function and only on the two helpers that own this
parse, so the tests pin the exact-segment matching, the docstring/comment
exclusion, the delegation boundary, and — most importantly — the real
CONNECT-1136 before/after shapes: the pre-fix module must fire and the post-fix
module must not.
"""

from __future__ import annotations

from conformance.suite.checks.persistence_seam import SERIES, scan_text


def _rule(src: str, rule_id: str = "P048", file: str = "app/miner.py") -> list:
    """Findings of a single rule from a per-file scan of *src* at path *file*."""
    return [f for f in scan_text(src, file) if f.rule_id == rule_id]


def test_series_letter() -> None:
    assert SERIES == "P"


# ── P048 — fires (app assembles the connection-scoped layout) ───────────────


def test_p048_fires_on_joined_path_segments() -> None:
    # The CONNECT-1136 shape: no single literal carries the layout — it exists
    # only once the join is assembled. The only literal in that file containing
    # both "apps" and "connection" was its docstring.
    src = (
        "def key(app, conn):\n"
        '    return "/".join(["persistent-artifacts", "apps", app, "connection", conn])\n'
    )
    fs = _rule(src)
    assert len(fs) == 1 and fs[0].line == 2


def test_p048_fires_on_full_path_literal() -> None:
    src = 'KEY = "persistent-artifacts/apps/oracle/connection/1741219200/marker.txt"\n'
    assert len(_rule(src)) == 1


def test_p048_fires_on_fstring_with_runtime_app_name() -> None:
    src = 'def p(app, cid):\n    return f"persistent-artifacts/apps/{app}/connection/{cid}"\n'
    assert len(_rule(src)) == 1


def test_p048_fires_on_string_concatenation() -> None:
    src = (
        'def p(app):\n    return "persistent-artifacts/apps/" + app + "/connection/x"\n'
    )
    assert len(_rule(src)) == 1


def test_p048_fires_on_tuple_join() -> None:
    src = (
        "def p(app, cid):\n"
        '    return "/".join(("persistent-artifacts", "apps", app, "connection", cid))\n'
    )
    assert len(_rule(src)) == 1


def test_p048_fires_even_when_module_imports_the_seam() -> None:
    # No import gate: a module that imports the seam and *still* hand-rolls the
    # connection layout is exactly a finding worth making.
    src = (
        "from application_sdk.common.incremental import get_persistent_s3_prefix\n"
        'KEY = "persistent-artifacts/apps/oracle/connection/1/marker.txt"\n'
    )
    assert len(_rule(src)) == 1


def test_p048_reports_each_distinct_path() -> None:
    src = (
        'A = "persistent-artifacts/apps/x/connection/1"\n'
        'B = "persistent-artifacts/apps/y/connection/2"\n'
    )
    assert len(_rule(src)) == 2


def test_p048_reports_an_assembled_path_once() -> None:
    # The join and its element constants must not both be reported.
    src = (
        "def p(app, cid):\n"
        '    return "/".join(["persistent-artifacts", "apps", app, "connection", cid])\n'
    )
    assert len(_rule(src)) == 1


# ── P048 — silent (paths the SDK helper does not own) ───────────────────────


def test_p048_silent_on_publish_state_layout() -> None:
    # Diverges at the 4th segment: the helper cannot produce ".../state/...".
    src = '_BASE = "persistent-artifacts/apps/atlan-publish-app/state"\n'
    assert _rule(src) == []


def test_p048_silent_on_workflow_config_layout() -> None:
    src = (
        "def p(app, wid):\n"
        '    return f"persistent-artifacts/apps/{app}/workflows/{wid}/config.json"\n'
    )
    assert _rule(src) == []


def test_p048_silent_on_argo_layout() -> None:
    # Diverges at the 2nd segment: {cqn} where the SDK layout has "apps".
    src = 'def p(cqn):\n    return f"persistent-artifacts/{cqn}/parquet/markers/mine"\n'
    assert _rule(src) == []


def test_p048_silent_on_skills_layout() -> None:
    src = '_ROOT = "persistent-artifacts/apps/{app_name}/skills"\n'
    assert _rule(src) == []


def test_p048_silent_on_bare_root_segment() -> None:
    # The root alone is not the layout — this is what made the rule fire 65
    # times fleet-wide with an inapplicable remedy.
    src = 'ROOT = "persistent-artifacts"\n'
    assert _rule(src) == []


def test_p048_silent_on_lookalike_segment() -> None:
    src = 'KEY = "persistent-artifacts-backup/apps/x/connection/1"\n'
    assert _rule(src) == []


def test_p048_silent_on_other_object_store_roots() -> None:
    src = 'A = "artifacts/apps/x/connection/1"\nB = "workflow_file_upload/y"\n'
    assert _rule(src) == []


def test_p048_silent_on_module_docstring() -> None:
    src = '"""Writes to persistent-artifacts/apps/{app}/connection/{id}."""\n'
    assert _rule(src) == []


def test_p048_silent_on_function_docstring() -> None:
    src = (
        "def key(cqn):\n"
        '    """Pattern: persistent-artifacts/apps/{app}/connection/{id}/marker.txt."""\n'
        "    return derive(cqn)\n"
    )
    assert _rule(src) == []


def test_p048_silent_on_comment() -> None:
    src = "# layout: persistent-artifacts/apps/{app}/connection/{id}\nx = 1\n"
    assert _rule(src) == []


def test_p048_silent_on_local_marker_filename() -> None:
    src = 'import os\ntmp = os.path.join(local_dir, "marker.txt")\n'
    assert _rule(src) == []


def test_p048_silent_on_syntax_error() -> None:
    assert scan_text("def broken(:\n", "app/x.py") == []


# ── P048 suppression ────────────────────────────────────────────────────────


def test_p048_suppressed_inline() -> None:
    src = (
        'P = "persistent-artifacts/apps/x/connection/1"  '
        "# conformance: ignore[P048] legacy key, migration tracked separately\n"
    )
    fs = _rule(src)
    assert len(fs) == 1 and fs[0].suppressed


def test_p048_suppressed_by_comment_line_above() -> None:
    src = (
        "# conformance: ignore[P048] legacy key\n"
        'P = "persistent-artifacts/apps/x/connection/1"\n'
    )
    fs = _rule(src)
    assert len(fs) == 1 and fs[0].suppressed


# ── P049 StrictConnectionQualifiedNameParse — fires ──────────────────────────


def _p049(src: str) -> list:
    return _rule(src, rule_id="P049")


def test_p049_fires_on_the_connect_1136_shape() -> None:
    # The defect: split the qualified name apart, then raise when no segment
    # looks like an epoch — where the SDK warns and proceeds.
    src = (
        "def stable_marker_key(connection_qualified_name, app_name=''):\n"
        '    parts = str(connection_qualified_name).strip("/").split("/")\n'
        "    connection_id = next((p for p in parts if p.isdigit()), None)\n"
        "    if not connection_id:\n"
        "        raise MarkerKeyInputError('no epoch segment')\n"
        "    return connection_id\n"
    )
    fs = _p049(src)
    assert len(fs) == 1 and fs[0].line == 5


def test_p049_fires_on_direct_split() -> None:
    src = (
        "def key(connection_qualified_name):\n"
        '    parts = connection_qualified_name.split("/")\n'
        "    if len(parts) < 3:\n"
        "        raise ValueError('bad qualified name')\n"
        "    return parts[-1]\n"
    )
    assert len(_p049(src)) == 1


def test_p049_fires_on_async_function() -> None:
    src = (
        "async def key(connection_qualified_name):\n"
        '    if not connection_qualified_name.split("/")[-1].isdigit():\n'
        "        raise ValueError('not an epoch')\n"
        "    return 1\n"
    )
    assert len(_p049(src)) == 1


def test_p049_anchors_at_the_earliest_raise() -> None:
    # Fingerprint stability: the anchor must be source order, not walk order.
    src = (
        "def key(connection_qualified_name):\n"
        "    if not connection_qualified_name:\n"
        "        raise ValueError('empty')\n"
        '    parts = connection_qualified_name.split("/")\n'
        "    if not parts:\n"
        "        raise ValueError('unsplittable')\n"
        "    return parts[-1]\n"
    )
    fs = _p049(src)
    assert len(fs) == 1 and fs[0].line == 3


# ── P049 — silent ────────────────────────────────────────────────────────────


def test_p049_fires_when_a_sibling_function_delegates() -> None:
    """The gate is per-function, not per-module.

    A module-level gate goes blind exactly as apps adopt the seam — which is the
    goal — so "module imports the seam and one function still hand-rolls a strict
    parse" would become undetectable at BLOCK tier.
    """
    src = (
        "from application_sdk.common.incremental import get_persistent_s3_prefix\n"
        "def good(connection_qualified_name, app_name):\n"
        "    return get_persistent_s3_prefix(connection_qualified_name, app_name)\n"
        "def bad(connection_qualified_name):\n"
        '    parts = connection_qualified_name.split("/")\n'
        "    if not parts[-1].isdigit():\n"
        "        raise ValueError('not an epoch')\n"
        "    return parts[-1]\n"
    )
    fs = _p049(src)
    assert len(fs) == 1 and fs[0].line == 7


def test_p049_fires_when_the_only_seam_call_is_a_marker_read() -> None:
    """Reaching the seam is not the same as delegating *this* parse.

    ``fetch_marker_from_storage`` takes an already-derived prefix and says
    nothing about which segment is the connection id, so a function that reads
    its marker through the SDK and still splits the qualified name apart itself
    has forked exactly the decision P049 guards. This is the half-migrated shape
    a BLOCK-tier recurrence guard must not go blind on.
    """
    src = (
        "from application_sdk.common.incremental import fetch_marker_from_storage\n"
        "def key(connection_qualified_name, app_name):\n"
        '    parts = connection_qualified_name.split("/")\n'
        "    if not parts[-1].isdigit():\n"
        "        raise ValueError('not an epoch')\n"
        '    return fetch_marker_from_storage(f"p/{parts[-1]}")\n'
    )
    fs = _p049(src)
    assert len(fs) == 1 and fs[0].line == 5


def test_p049_fires_when_the_only_seam_call_is_create_next_marker() -> None:
    # Same boundary through the module-alias spelling, which resolves the
    # symbol name via the receiver's import origin rather than the bare name.
    src = (
        "from application_sdk.common.incremental import marker\n"
        "def key(connection_qualified_name):\n"
        '    parts = connection_qualified_name.split("/")\n'
        "    if not parts[-1].isdigit():\n"
        "        raise ValueError('not an epoch')\n"
        "    return marker.create_next_marker(parts[-1])\n"
    )
    assert len(_p049(src)) == 1


def test_p049_silent_when_function_calls_extract_epoch_id() -> None:
    # The other half of the boundary: the parse helper itself is delegation.
    src = (
        "from application_sdk.common.incremental import (\n"
        "    extract_epoch_id_from_qualified_name,\n"
        ")\n"
        "def key(connection_qualified_name):\n"
        '    _ = connection_qualified_name.split("/")\n'
        "    cid = extract_epoch_id_from_qualified_name(connection_qualified_name)\n"
        "    if not cid:\n"
        "        raise ValueError('no connection id')\n"
        "    return cid\n"
    )
    assert _p049(src) == []


def test_p049_silent_when_seam_symbol_is_aliased() -> None:
    # An ``as`` alias binds a different name but the same import origin.
    src = (
        "from application_sdk.common.incremental import (\n"
        "    get_persistent_s3_prefix as prefix_for,\n"
        ")\n"
        "def key(connection_qualified_name, app_name):\n"
        '    _ = connection_qualified_name.split("/")\n'
        "    try:\n"
        "        return prefix_for(connection_qualified_name, app_name)\n"
        "    except AppError as exc:\n"
        "        raise MarkerKeyInputError('cannot derive') from exc\n"
    )
    assert _p049(src) == []


def test_p049_silent_when_function_calls_seam_via_module_alias() -> None:
    src = (
        "from application_sdk.common.incremental import helpers\n"
        "def key(connection_qualified_name):\n"
        '    parts = connection_qualified_name.split("/")\n'
        "    if not parts:\n"
        "        raise ValueError('x')\n"
        "    return helpers.get_persistent_s3_prefix(connection_qualified_name)\n"
    )
    assert _p049(src) == []


def test_p049_silent_when_seam_imported() -> None:
    # Post-fix shape: delegate the parse, then raise a typed error around the
    # SDK's own. That is correct, not a divergence.
    src = (
        "from application_sdk.common.incremental.helpers import get_persistent_s3_prefix\n"
        "def key(connection_qualified_name, app_name):\n"
        '    _ = connection_qualified_name.split("/")\n'
        "    try:\n"
        "        prefix = get_persistent_s3_prefix(connection_qualified_name, app_name)\n"
        "    except AppError as exc:\n"
        "        raise MarkerKeyInputError('cannot derive') from exc\n"
        "    return prefix\n"
    )
    assert _p049(src) == []


def test_p049_silent_without_raise() -> None:
    # Parsing locally but warning-and-proceeding matches the SDK's contract.
    src = (
        "def key(connection_qualified_name):\n"
        '    parts = connection_qualified_name.split("/")\n'
        "    if not parts[-1].isdigit():\n"
        "        logger.warning('not an epoch')\n"
        "    return parts[-1]\n"
    )
    assert _p049(src) == []


def test_p049_silent_when_splitting_a_different_string() -> None:
    # The netsuite false positive this rule was tightened against: the split is
    # on an unrelated value and the raise is about an unrelated field.
    src = (
        "def map_column(schema_entity, connection_qualified_name):\n"
        "    original_qn = schema_entity.get('qualifiedName', '')\n"
        '    name = original_qn.split("/")[-1]\n'
        "    if not name:\n"
        "        raise EntityMappingError('no property name')\n"
        "    return name\n"
    )
    assert _p049(src) == []


def test_p049_silent_on_non_connection_qualified_name() -> None:
    # Table/column/asset qualified names have a different owner and different
    # segment semantics.
    src = (
        "def key(table_qualified_name):\n"
        '    parts = table_qualified_name.split("/")\n'
        "    if len(parts) < 4:\n"
        "        raise ValueError('bad table qn')\n"
        "    return parts[-1]\n"
    )
    assert _p049(src) == []


def test_p049_silent_when_raise_is_in_a_nested_function() -> None:
    # A nested def is its own scope; its raise is not this function's control flow.
    src = (
        "def key(connection_qualified_name):\n"
        '    parts = connection_qualified_name.split("/")\n'
        "    def fail():\n"
        "        raise ValueError('inner')\n"
        "    return parts[-1]\n"
    )
    assert _p049(src) == []


def test_p049_silent_without_split() -> None:
    src = (
        "def key(connection_qualified_name):\n"
        "    if not connection_qualified_name:\n"
        "        raise ValueError('required')\n"
        "    return connection_qualified_name\n"
    )
    assert _p049(src) == []


# ── P049 suppression ─────────────────────────────────────────────────────────


def test_p049_suppressed_inline() -> None:
    src = (
        "def key(connection_qualified_name):\n"
        '    parts = connection_qualified_name.split("/")\n'
        "    if len(parts) < 3:\n"
        "        raise ValueError('x')  # conformance: ignore[P049] stricter by design\n"
        "    return parts[-1]\n"
    )
    fs = _p049(src)
    assert len(fs) == 1 and fs[0].suppressed
