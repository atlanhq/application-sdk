"""Meta-tests for the P-series persistence-seam check (P048, CONNECT-1275).

P048 flags an app assembling the SDK-owned ``persistent-artifacts`` object-store
layout itself instead of deriving it from
``application_sdk.common.incremental.helpers.get_persistent_s3_prefix``.

The rule is a two-signal heuristic (prefix literal AND no seam import), so the
tests pin both halves, the exact-segment matching, the docstring/comment
exclusion, and — most importantly — the real CONNECT-1136 before/after shapes:
the pre-fix module must fire and the post-fix module must not.
"""

from __future__ import annotations

from conformance.suite.checks.persistence_seam import SERIES, scan_text


def _rule(src: str, rule_id: str = "P048", file: str = "app/miner.py") -> list:
    """Findings of a single rule from a per-file scan of *src* at path *file*."""
    return [f for f in scan_text(src, file) if f.rule_id == rule_id]


def test_series_letter() -> None:
    assert SERIES == "P"


# ── P048 — fires (app builds the prefix, no seam import) ─────────────────────


def test_p048_fires_on_joined_path_segments() -> None:
    # The CONNECT-1136 shape: segments in a list joined by "/", so the prefix
    # appears as a bare constant rather than inside a full path string.
    src = (
        "def key(app, conn):\n"
        '    return "/".join(["persistent-artifacts", "apps", app, "connection", conn])\n'
    )
    fs = _rule(src)
    assert len(fs) == 1 and fs[0].line == 2


def test_p048_fires_on_full_path_literal() -> None:
    src = 'KEY = "persistent-artifacts/apps/oracle/connection/1741219200/marker.txt"\n'
    assert len(_rule(src)) == 1


def test_p048_fires_on_fstring_with_runtime_segment() -> None:
    src = 'def p(cqn):\n    return f"persistent-artifacts/{cqn}/parquet/markers/mine"\n'
    assert len(_rule(src)) == 1


def test_p048_fires_on_module_level_template_constant() -> None:
    src = '_ROOT = "persistent-artifacts/apps/{app_name}/skills"\n'
    assert len(_rule(src)) == 1


def test_p048_fires_on_prefix_in_middle_of_path() -> None:
    src = 'def p(base):\n    return f"{base}/persistent-artifacts/x"\n'
    assert len(_rule(src)) == 1


def test_p048_reports_each_distinct_literal() -> None:
    src = 'A = "persistent-artifacts/apps/x"\nB = "persistent-artifacts/apps/y"\n'
    assert len(_rule(src)) == 2


# ── P048 — silent (delegating, or not this layout) ───────────────────────────


def test_p048_silent_when_seam_imported_from() -> None:
    # The post-fix CONNECT-1136 shape: derive the prefix from the SDK helper.
    src = (
        "from application_sdk.common.incremental.helpers import get_persistent_s3_prefix\n"
        "def key(cqn, app):\n"
        '    return f"{get_persistent_s3_prefix(cqn, app)}/miner-marker.txt"\n'
    )
    assert _rule(src) == []


def test_p048_silent_when_seam_imported_as_module() -> None:
    src = (
        "import application_sdk.common.incremental.marker as m\n"
        'KEY = "persistent-artifacts/apps/x"\n'
    )
    assert _rule(src) == []


def test_p048_silent_when_seam_submodule_imported() -> None:
    src = (
        "from application_sdk.common.incremental import marker\n"
        'KEY = "persistent-artifacts/apps/x"\n'
    )
    assert _rule(src) == []


def test_p048_silent_on_unrelated_sdk_import() -> None:
    # Importing some *other* SDK module is not delegation — must still fire.
    src = (
        "from application_sdk.observability.logger_adaptor import get_logger\n"
        'KEY = "persistent-artifacts/apps/x"\n'
    )
    assert len(_rule(src)) == 1


def test_p048_silent_on_lookalike_segment() -> None:
    # Exact path-segment match: a longer segment that merely starts with the
    # prefix is a different directory and not this rule's business.
    src = 'KEY = "persistent-artifacts-backup/apps/x"\n'
    assert _rule(src) == []


def test_p048_silent_on_other_object_store_roots() -> None:
    src = 'A = "artifacts/apps/x"\nB = "workflow_file_upload/y"\n'
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
    # Comments never reach the AST — the fleet's many explanatory
    # "# persistent-artifacts/..." notes cost nothing.
    src = "# layout: persistent-artifacts/apps/{app}/connection/{id}\nx = 1\n"
    assert _rule(src) == []


def test_p048_silent_on_local_marker_filename() -> None:
    # A local temp file named marker.txt is not the object-store layout; only
    # the prefix segment is a signal.
    src = 'import os\ntmp = os.path.join(local_dir, "marker.txt")\n'
    assert _rule(src) == []


def test_p048_silent_on_syntax_error() -> None:
    assert scan_text("def broken(:\n", "app/x.py") == []


# ── Suppression ──────────────────────────────────────────────────────────────


def test_p048_suppressed_inline() -> None:
    src = (
        'P = "persistent-artifacts/{cqn}/parquet/markers/mine"  '
        "# conformance: ignore[P048] Argo-layout path the SDK helper does not model\n"
    )
    fs = _rule(src)
    assert len(fs) == 1 and fs[0].suppressed


def test_p048_suppressed_by_comment_line_above() -> None:
    src = (
        "# conformance: ignore[P048] Argo-layout compatibility\n"
        'P = "persistent-artifacts/{cqn}/parquet/markers/mine"\n'
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


def test_p049_silent_when_seam_imported() -> None:
    # Post-fix shape: delegate the parse, then raise a typed error around the
    # SDK's own. That is correct, not a divergence.
    src = (
        "from application_sdk.common.incremental.helpers import get_persistent_s3_prefix\n"
        "def key(connection_qualified_name, app_name):\n"
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
