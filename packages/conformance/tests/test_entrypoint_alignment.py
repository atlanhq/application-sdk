"""Tests for P016 EntryPointContractCodeDrift check.

Covers the alignment invariant between ``@entrypoint`` wire names (code side)
and ``app/generated/<name>/manifest.json`` subdirs (contract side).

Each test builds a minimal temporary app tree in ``tmp_path``, calls
:func:`scan_all`, and asserts on the resulting findings.

Fixture conventions
-------------------
* ``py_files``: ``{relative_path: source_text}`` written under ``tmp_path``.
  Source text uses bare names (``Input``, ``Output``, ``App``) because the
  AST scanner never *executes* the code; unresolved references are irrelevant.
* ``generated``: describes the ``app/generated/`` tree shape —

  * ``"absent"``         — no ``app/generated/`` directory at all.
  * ``"single"``         — ``app/generated/manifest.json`` at the root only.
  * list of strings      — multi-EP; each string becomes a subdir with a
                           ``manifest.json`` inside it.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from typing import Literal

import pytest
from conformance.suite.checks.entrypoint_alignment import scan_all
from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import EnforcementTier, RuleScope

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

GeneratedShape = Literal["absent", "single"] | list[str]


def _write_generated(tmp_path: Path, shape: GeneratedShape) -> None:
    """Create the ``app/generated/`` tree under *tmp_path*."""
    if shape == "absent":
        return
    if shape == "single":
        gen = tmp_path / "app" / "generated"
        gen.mkdir(parents=True)
        (gen / "manifest.json").write_text('{"dag": {}}')
        return
    # Multi-EP: one subdir per entry-point name
    for ep_name in shape:
        ep_dir = tmp_path / "app" / "generated" / ep_name
        ep_dir.mkdir(parents=True)
        (ep_dir / "manifest.json").write_text('{"dag": {}}')


def _write_py(tmp_path: Path, py_files: dict[str, str]) -> list[Path]:
    """Write Python source files under *tmp_path*, return the Path list."""
    paths: list[Path] = []
    for name, src in py_files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
        paths.append(p)
    return paths


def _run(
    tmp_path: Path,
    py_files: dict[str, str],
    generated: GeneratedShape,
) -> list:
    """Set up a temp app tree and run the P016 scanner."""
    _write_generated(tmp_path, generated)
    paths = _write_py(tmp_path, py_files)
    return scan_all(paths, tmp_path)


def _p016_ids(findings: list) -> list[str]:
    return [f.rule_id for f in findings if f.rule_id == "P016"]


# ---------------------------------------------------------------------------
# Rule metadata
# ---------------------------------------------------------------------------


def test_p016_rule_is_blocking() -> None:
    rule = get_rule("P016")
    assert rule.tier == EnforcementTier.BLOCK


def test_p016_rule_is_app_scoped() -> None:
    rule = get_rule("P016")
    assert rule.scope == RuleScope.APP


def test_p016_rule_is_not_autofixable() -> None:
    rule = get_rule("P016")
    assert rule.autofixable is False


# ---------------------------------------------------------------------------
# No-op cases
# ---------------------------------------------------------------------------


def test_p016_no_generated_dir_is_noop(tmp_path: Path) -> None:
    """Absent app/generated/ → no findings (not a native-app-contract repo)."""
    py = {
        "app/my_app.py": dedent("""\
            from application_sdk.app import App, entrypoint
            class MyApp(App):
                @entrypoint(name="crawler")
                async def crawl(self, input: Input) -> Output: ...
        """)
    }
    findings = _run(tmp_path, py, "absent")
    assert _p016_ids(findings) == []


def test_p016_third_party_entrypoint_not_counted(tmp_path: Path) -> None:
    """A ``@entrypoint`` from a non-SDK import is NOT counted (import-provenance guard).

    The file that imports from ``some_other_lib`` uses a decorator also named
    ``entrypoint``, but it must not be confused with the SDK's decorator.  Only
    the second file (with the real SDK import) contributes to the alignment check.
    """
    py = {
        # Third-party @entrypoint — must not be collected.
        "app/wrong.py": dedent("""\
            from some_other_lib import entrypoint
            class ThirdPartyApp:
                @entrypoint
                async def wrong_name(self, input): ...
        """),
        # SDK @entrypoint — aligns with the contract.
        "app/sdk_app.py": dedent("""\
            from application_sdk.app import App, entrypoint
            class RealApp(App):
                @entrypoint(name="crawler")
                async def crawl(self, input: Input) -> Output: ...
        """),
    }
    findings = _run(tmp_path, py, ["crawler"])
    assert _p016_ids(findings) == []


# ---------------------------------------------------------------------------
# Multi-EP mode — aligned
# ---------------------------------------------------------------------------


def test_p016_multi_ep_aligned_with_name_param(tmp_path: Path) -> None:
    """Exact set equality via @entrypoint(name=...) → no findings."""
    py = {
        "app/oracle.py": dedent("""\
            from application_sdk.app import App, entrypoint
            class OracleApp(App):
                @entrypoint(name="crawler")
                async def extract(self, input: Input) -> Output: ...
                @entrypoint(name="miner")
                async def mine(self, input: Input) -> Output: ...
        """)
    }
    findings = _run(tmp_path, py, ["crawler", "miner"])
    assert _p016_ids(findings) == []


def test_p016_multi_ep_aligned_via_method_name(tmp_path: Path) -> None:
    """Bare @entrypoint with method names that match contract dirs → no findings."""
    py = {
        "app/my_app.py": dedent("""\
            from application_sdk.app import App, entrypoint
            class MyApp(App):
                @entrypoint
                async def crawler(self, input: Input) -> Output: ...
                @entrypoint
                async def miner(self, input: Input) -> Output: ...
        """)
    }
    # Contract dirs: crawler, miner — match bare method names (no _ to - needed here)
    findings = _run(tmp_path, py, ["crawler", "miner"])
    assert _p016_ids(findings) == []


# ---------------------------------------------------------------------------
# Multi-EP mode — the oracle drift case
# ---------------------------------------------------------------------------


def test_p016_oracle_drift_four_findings(tmp_path: Path) -> None:
    """The oracle-app scenario: code derives extract-metadata / mine-queries but
    contract expects crawler / miner → 2 code-only + 2 contract-only findings."""
    py = {
        "app/oracle.py": dedent("""\
            from application_sdk.app import App, entrypoint
            class OracleApp(App):
                @entrypoint
                async def extract_metadata(self, input: Input) -> Output: ...
                @entrypoint
                async def mine_queries(self, input: Input) -> Output: ...
        """)
    }
    findings = _run(tmp_path, py, ["crawler", "miner"])
    p016 = [f for f in findings if f.rule_id == "P016"]
    assert len(p016) == 4
    messages = [f.message for f in p016]
    # Two code-only findings
    assert any("extract-metadata" in m and "crawler" in m for m in messages)
    assert any("mine-queries" in m and "miner" in m for m in messages)
    # Two contract-only findings
    assert any("crawler" in m and "not in code" in m for m in messages)
    assert any("miner" in m and "not in code" in m for m in messages)


def test_p016_oracle_drift_finding_is_blocking(tmp_path: Path) -> None:
    """P016 findings must be BLOCK-tier (SARIF error level)."""
    py = {
        "app/oracle.py": dedent("""\
            from application_sdk.app import App, entrypoint
            class OracleApp(App):
                @entrypoint
                async def extract_metadata(self, input: Input) -> Output: ...
        """)
    }
    findings = _run(tmp_path, py, ["crawler"])
    p016 = [f for f in findings if f.rule_id == "P016"]
    assert p016, "expected at least one P016 finding"
    rule = get_rule("P016")
    assert rule.tier == EnforcementTier.BLOCK


# ---------------------------------------------------------------------------
# Multi-EP mode — partial drift
# ---------------------------------------------------------------------------


def test_p016_partial_drift_two_findings(tmp_path: Path) -> None:
    """One entry point matches, one drifts → exactly 2 findings."""
    py = {
        "app/my_app.py": dedent("""\
            from application_sdk.app import App, entrypoint
            class MyApp(App):
                @entrypoint(name="crawler")
                async def crawl(self, input: Input) -> Output: ...
                @entrypoint(name="wrong-name")
                async def mine(self, input: Input) -> Output: ...
        """)
    }
    findings = _run(tmp_path, py, ["crawler", "miner"])
    p016 = [f for f in findings if f.rule_id == "P016"]
    assert len(p016) == 2
    messages = [f.message for f in p016]
    assert any("wrong-name" in m for m in messages)
    assert any("miner" in m and "not in code" in m for m in messages)


# ---------------------------------------------------------------------------
# Multi-EP mode — method name kebab conversion
# ---------------------------------------------------------------------------


def test_p016_kebab_conversion_aligns(tmp_path: Path) -> None:
    """Bare @entrypoint with underscored method name → kebab matches contract."""
    py = {
        "app/my_app.py": dedent("""\
            from application_sdk.app import App, entrypoint
            class MyApp(App):
                @entrypoint
                async def asset_export(self, input: Input) -> Output: ...
        """)
    }
    findings = _run(tmp_path, py, ["asset-export"])
    assert _p016_ids(findings) == []


def test_p016_kebab_conversion_mismatch(tmp_path: Path) -> None:
    """Bare @entrypoint derives 'asset-export' but contract expects 'export' → finding."""
    py = {
        "app/my_app.py": dedent("""\
            from application_sdk.app import App, entrypoint
            class MyApp(App):
                @entrypoint
                async def asset_export(self, input: Input) -> Output: ...
        """)
    }
    findings = _run(tmp_path, py, ["export"])
    p016 = [f for f in findings if f.rule_id == "P016"]
    assert len(p016) == 2  # code-only (asset-export) + contract-only (export)


# ---------------------------------------------------------------------------
# Single-EP mode
# ---------------------------------------------------------------------------


def test_p016_single_ep_one_entrypoint_any_name_ok(tmp_path: Path) -> None:
    """Single-EP contract + exactly one @entrypoint (any name) → no findings."""
    py = {
        "app/my_app.py": dedent("""\
            from application_sdk.app import App, entrypoint
            class MyApp(App):
                @entrypoint(name="crawl")
                async def do_crawl(self, input: Input) -> Output: ...
        """)
    }
    findings = _run(tmp_path, py, "single")
    assert _p016_ids(findings) == []


def test_p016_single_ep_zero_entrypoints_ok(tmp_path: Path) -> None:
    """Single-EP contract + no explicit @entrypoints → no findings (implicit run())."""
    py = {
        "app/my_app.py": dedent("""\
            from application_sdk.app import App
            class MyApp(App):
                async def run(self, input: Input) -> Output: ...
        """)
    }
    findings = _run(tmp_path, py, "single")
    assert _p016_ids(findings) == []


def test_p016_single_ep_two_entrypoints_fires(tmp_path: Path) -> None:
    """Single-EP contract but 2 @entrypoints in code → 1 finding (the extra one)."""
    py = {
        "app/my_app.py": dedent("""\
            from application_sdk.app import App, entrypoint
            class MyApp(App):
                @entrypoint(name="crawler")
                async def crawl(self, input: Input) -> Output: ...
                @entrypoint(name="miner")
                async def mine(self, input: Input) -> Output: ...
        """)
    }
    findings = _run(tmp_path, py, "single")
    p016 = [f for f in findings if f.rule_id == "P016"]
    assert len(p016) == 1
    assert "miner" in p016[0].message


# ---------------------------------------------------------------------------
# Unresolved name= argument
# ---------------------------------------------------------------------------


def test_p016_non_literal_name_fires(tmp_path: Path) -> None:
    """Non-literal name= → 1 unresolved finding (regardless of contract mode)."""
    py = {
        "app/my_app.py": dedent("""\
            from application_sdk.app import App, entrypoint
            EP_NAME = "crawler"
            class MyApp(App):
                @entrypoint(name=EP_NAME)
                async def crawl(self, input: Input) -> Output: ...
        """)
    }
    findings = _run(tmp_path, py, ["crawler"])
    p016 = [f for f in findings if f.rule_id == "P016"]
    assert len(p016) >= 1
    assert any("non-literal" in f.message for f in p016)


# ---------------------------------------------------------------------------
# Duplicate wire names
# ---------------------------------------------------------------------------


def test_p016_duplicate_wire_name_fires(tmp_path: Path) -> None:
    """Two @entrypoint methods with the same wire name → findings for each duplicate."""
    py = {
        "app/my_app.py": dedent("""\
            from application_sdk.app import App, entrypoint
            class MyApp(App):
                @entrypoint(name="crawler")
                async def crawl_v1(self, input: Input) -> Output: ...
                @entrypoint(name="crawler")
                async def crawl_v2(self, input: Input) -> Output: ...
        """)
    }
    findings = _run(tmp_path, py, ["crawler"])
    p016 = [f for f in findings if f.rule_id == "P016"]
    assert len(p016) == 2
    assert all("more than one" in f.message for f in p016)


def test_p016_duplicate_wire_name_halts_set_check(tmp_path: Path) -> None:
    """Duplicate detection short-circuits before the set-equality pass.

    The contract includes 'miner', which is absent from code.  Without the
    short-circuit guard, the set-equality pass would emit a contract-only
    "'miner' is defined in the contract ... but not in code" finding alongside
    the duplicate findings.  With the guard, only the 2 duplicate findings
    are returned.
    """
    py = {
        "app/my_app.py": dedent("""\
            from application_sdk.app import App, entrypoint
            class MyApp(App):
                @entrypoint(name="crawler")
                async def crawl_a(self, input: Input) -> Output: ...
                @entrypoint(name="crawler")
                async def crawl_b(self, input: Input) -> Output: ...
        """)
    }
    # Contract has "crawler" AND "miner".  If the short-circuit were absent,
    # the set-equality pass would add a "miner ... not in code" finding.
    findings = _run(tmp_path, py, ["crawler", "miner"])
    p016 = [f for f in findings if f.rule_id == "P016"]
    assert len(p016) == 2, f"expected exactly 2 duplicate findings, got {len(p016)}"
    assert all("more than one" in f.message for f in p016)
    assert not any("not in code" in f.message for f in p016)


# ---------------------------------------------------------------------------
# Inline suppression
# ---------------------------------------------------------------------------


def test_p016_suppressed_by_inline_directive(tmp_path: Path) -> None:
    """# conformance: ignore[P016] on the decorator line suppresses the finding."""
    py = {
        "app/oracle.py": dedent("""\
            from application_sdk.app import App, entrypoint
            class OracleApp(App):
                # conformance: ignore[P016] legacy name kept for Argo DAG compat
                @entrypoint
                async def extract_metadata(self, input: Input) -> Output: ...
        """)
    }
    findings = _run(tmp_path, py, ["crawler"])
    # The code-only finding for extract-metadata should be suppressed.
    # Use a precise substring so we don't accidentally match the contract-only
    # finding for 'crawler', whose message also contains "extract-metadata"
    # in the "code defines: extract-metadata" clause.
    code_only = [
        f
        for f in findings
        if f.rule_id == "P016" and "'extract-metadata' is defined in code" in f.message
    ]
    assert (
        code_only
    ), "expected at least one code-side P016 finding for extract-metadata"
    assert all(f.suppressed for f in code_only)


# ---------------------------------------------------------------------------
# Cross-file detection
# ---------------------------------------------------------------------------


def test_p016_entrypoints_split_across_files(tmp_path: Path) -> None:
    """@entrypoint methods in different files are all collected."""
    py = {
        "app/crawler.py": dedent("""\
            from application_sdk.app import App, entrypoint
            class CrawlerApp(App):
                @entrypoint(name="crawler")
                async def crawl(self, input: Input) -> Output: ...
        """),
        "app/miner.py": dedent("""\
            from application_sdk.app import App, entrypoint
            class MinerApp(App):
                @entrypoint(name="miner")
                async def mine(self, input: Input) -> Output: ...
        """),
    }
    findings = _run(tmp_path, py, ["crawler", "miner"])
    assert _p016_ids(findings) == []


# ---------------------------------------------------------------------------
# Alias import
# ---------------------------------------------------------------------------


def test_p016_aliased_entrypoint_import(tmp_path: Path) -> None:
    """``from application_sdk.app import entrypoint as ep`` is tracked correctly."""
    py = {
        "app/my_app.py": dedent("""\
            from application_sdk.app import App
            from application_sdk.app import entrypoint as ep
            class MyApp(App):
                @ep(name="crawler")
                async def crawl(self, input: Input) -> Output: ...
        """)
    }
    findings = _run(tmp_path, py, ["crawler"])
    assert _p016_ids(findings) == []


# ---------------------------------------------------------------------------
# Single-mode route/card split (BLDX-1342): DAG-declared secondary entrypoints
# ---------------------------------------------------------------------------


def _alias_block(aliases: list[tuple[str, str]]) -> dict:
    """The manifest's ``legacy_workflow_types`` block (CONNECT-1081)."""
    return {
        "legacy_workflow_types": {
            "aliases": [
                {"alias": alias, "entrypoint": target} for alias, target in aliases
            ]
        }
    }


def _write_single_manifest_with_routes(
    tmp_path: Path,
    workflow_types: list[str],
    aliases: list[tuple[str, str]] | None = None,
) -> None:
    """Write app/generated/manifest.json (single mode) whose DAG declares the
    given ``workflow_type`` values (``"<app>:<wire>"`` for routes), optionally
    with a ``legacy_workflow_types`` block."""
    import json

    gen = tmp_path / "app" / "generated"
    gen.mkdir(parents=True)
    dag = {
        f"node{i}": {"workflow_type": workflow_type}
        for i, workflow_type in enumerate(workflow_types)
    }
    manifest = {"dag": dag, **(_alias_block(aliases) if aliases else {})}
    (gen / "manifest.json").write_text(json.dumps(manifest))


def test_p016_single_mode_route_declared_secondary_entrypoint_ok(
    tmp_path: Path,
) -> None:
    """Two @entrypoints, both declared as DAG routes → no finding (BLDX-1342)."""
    _write_single_manifest_with_routes(
        tmp_path,
        ["app:extract-metadata", "app:extract-lineage", "PublishWorkflow"],
    )
    paths = _write_py(
        tmp_path,
        {
            "app/connector.py": dedent("""\
                from application_sdk.app import App, entrypoint
                class MyApp(App):
                    @entrypoint(name="extract-metadata")
                    async def extract_metadata(self, input: Input) -> Output: ...
                    @entrypoint(name="extract-lineage")
                    async def extract_lineage(self, input: Input) -> Output: ...
            """)
        },
    )
    findings = scan_all(paths, tmp_path)
    assert _p016_ids(findings) == []


def test_p016_single_mode_unrouted_code_entrypoint_flags(tmp_path: Path) -> None:
    """A code @entrypoint that the DAG never routes to is still flagged."""
    _write_single_manifest_with_routes(tmp_path, ["app:extract-metadata"])
    paths = _write_py(
        tmp_path,
        {
            "app/connector.py": dedent("""\
                from application_sdk.app import App, entrypoint
                class MyApp(App):
                    @entrypoint(name="extract-metadata")
                    async def extract_metadata(self, input: Input) -> Output: ...
                    @entrypoint(name="rogue")
                    async def rogue(self, input: Input) -> Output: ...
            """)
        },
    )
    findings = scan_all(paths, tmp_path)
    msgs = [f.message for f in findings if f.rule_id == "P016"]
    assert len(msgs) == 1 and "rogue" in msgs[0]


def test_p016_single_mode_legacy_no_routes_still_enforces_max_one(
    tmp_path: Path,
) -> None:
    """Legacy single manifest with no '<app>:<wire>' routes → max-1 fallback holds."""
    paths = _write_py(
        tmp_path,
        {
            "app/connector.py": dedent("""\
                from application_sdk.app import App, entrypoint
                class MyApp(App):
                    @entrypoint(name="a")
                    async def a(self, input: Input) -> Output: ...
                    @entrypoint(name="b")
                    async def b(self, input: Input) -> Output: ...
            """)
        },
    )
    # "single" shape writes {"dag": {}} → no routes
    _write_generated(tmp_path, "single")
    findings = scan_all(paths, tmp_path)
    assert len(_p016_ids(findings)) == 1


def test_p016_single_mode_duplicate_wire_name_fires(tmp_path: Path) -> None:
    """Two @entrypoints sharing one name= that is a declared route still fire the
    duplicate check — single mode now hosts >1 entry point, so the duplicate
    detection must run in single mode too, not only in multi mode."""
    _write_single_manifest_with_routes(tmp_path, ["app:extract-metadata"])
    paths = _write_py(
        tmp_path,
        {
            "app/connector.py": dedent("""\
                from application_sdk.app import App, entrypoint
                class MyApp(App):
                    @entrypoint(name="extract-metadata")
                    async def extract_a(self, input: Input) -> Output: ...
                    @entrypoint(name="extract-metadata")
                    async def extract_b(self, input: Input) -> Output: ...
            """)
        },
    )
    findings = scan_all(paths, tmp_path)
    p016 = [f for f in findings if f.rule_id == "P016"]
    assert len(p016) == 2
    assert all("more than one" in f.message for f in p016)


def test_p016_single_mode_workflow_type_outside_dag_ignored(tmp_path: Path) -> None:
    """A '<app>:<wire>' workflow_type outside the manifest's ``dag`` section is
    not a route: route collection is scoped to the DAG, so a code @entrypoint
    matching only a non-DAG workflow_type is still flagged as drift."""
    import json

    gen = tmp_path / "app" / "generated"
    gen.mkdir(parents=True)
    manifest = {
        "dag": {"node0": {"workflow_type": "app:extract-metadata"}},
        # A stray colon-bearing workflow_type OUTSIDE the dag must be ignored.
        "other": {"workflow_type": "app:rogue"},
    }
    (gen / "manifest.json").write_text(json.dumps(manifest))
    paths = _write_py(
        tmp_path,
        {
            "app/connector.py": dedent("""\
                from application_sdk.app import App, entrypoint
                class MyApp(App):
                    @entrypoint(name="extract-metadata")
                    async def extract_metadata(self, input: Input) -> Output: ...
                    @entrypoint(name="rogue")
                    async def rogue(self, input: Input) -> Output: ...
            """)
        },
    )
    findings = scan_all(paths, tmp_path)
    msgs = [f.message for f in findings if f.rule_id == "P016"]
    assert len(msgs) == 1 and "rogue" in msgs[0]


# ---------------------------------------------------------------------------
# legacy_workflow_types aliases (CNCT-199; manifest-sourced since CONNECT-1081)
# ---------------------------------------------------------------------------


def test_p016_declared_alias_is_recognised_as_a_route(tmp_path: Path) -> None:
    """F1: a bare DAG node matching a declared legacy alias routes its entry point.

    Without resolving the declaration, the colon-free DAG node reads as a
    foreign platform node, the entry point looks unrouted, and P016 fires a
    false positive on an app that is correctly wired.
    """
    _write_single_manifest_with_routes(
        tmp_path,
        ["app:extract-metadata", "KeifuWorkflow"],
        aliases=[("KeifuWorkflow", "keifu")],
    )
    paths = _write_py(
        tmp_path,
        {
            "app/connector.py": dedent("""\
                from application_sdk.app import App, entrypoint
                class MyApp(App):
                    name = "app"
                    legacy_workflow_types = {"KeifuWorkflow": "keifu"}
                    @entrypoint(name="extract-metadata")
                    async def extract_metadata(self, input: Input) -> Output: ...
                    @entrypoint(name="keifu")
                    async def keifu(self, input: Input) -> Output: ...
            """)
        },
    )
    findings = scan_all(paths, tmp_path)
    assert _p016_ids(findings) == []


def test_p016_alias_absent_from_manifest_still_flags(tmp_path: Path) -> None:
    """An alias the DAG never dispatches does not route — genuine drift fires."""
    _write_single_manifest_with_routes(
        tmp_path,
        ["app:extract-metadata", "SomeOtherWorkflow"],
        aliases=[("KeifuWorkflow", "keifu")],
    )
    paths = _write_py(
        tmp_path,
        {
            "app/connector.py": dedent("""\
                from application_sdk.app import App, entrypoint
                class MyApp(App):
                    legacy_workflow_types = {"KeifuWorkflow": "keifu"}
                    @entrypoint(name="extract-metadata")
                    async def extract_metadata(self, input: Input) -> Output: ...
                    @entrypoint(name="keifu")
                    async def keifu(self, input: Input) -> Output: ...
            """)
        },
    )
    findings = scan_all(paths, tmp_path)
    msgs = [f.message for f in findings if f.rule_id == "P016"]
    assert len(msgs) == 1 and "keifu" in msgs[0]


def test_p016_alias_declared_only_in_code_does_not_route(tmp_path: Path) -> None:
    """The manifest is P016's alias source (CONNECT-1081), not the class attribute.

    The DAG dispatches the alias and the App declares it, but the manifest
    carries no ``legacy_workflow_types`` block — so the bare node stays a
    foreign platform node here. K015 is what reports the missing block.
    """
    _write_single_manifest_with_routes(
        tmp_path, ["app:extract-metadata", "KeifuWorkflow"]
    )
    paths = _write_py(
        tmp_path,
        {
            "app/connector.py": dedent("""\
                from application_sdk.app import App, entrypoint
                class MyApp(App):
                    name = "app"
                    legacy_workflow_types = {"KeifuWorkflow": "keifu"}
                    @entrypoint(name="extract-metadata")
                    async def extract_metadata(self, input: Input) -> Output: ...
                    @entrypoint(name="keifu")
                    async def keifu(self, input: Input) -> Output: ...
            """)
        },
    )
    findings = scan_all(paths, tmp_path)
    msgs = [f.message for f in findings if f.rule_id == "P016"]
    assert len(msgs) == 1 and "keifu" in msgs[0]


def test_p016_foreign_bare_node_does_not_rescue_an_unrelated_entrypoint(
    tmp_path: Path,
) -> None:
    """F2: an undeclared platform node like PublishWorkflow is not a route."""
    _write_single_manifest_with_routes(
        tmp_path, ["app:extract-metadata", "PublishWorkflow"]
    )
    paths = _write_py(
        tmp_path,
        {
            "app/connector.py": dedent("""\
                from application_sdk.app import App, entrypoint
                class MyApp(App):
                    @entrypoint(name="extract-metadata")
                    async def extract_metadata(self, input: Input) -> Output: ...
                    @entrypoint(name="rogue")
                    async def rogue(self, input: Input) -> Output: ...
            """)
        },
    )
    findings = scan_all(paths, tmp_path)
    msgs = [f.message for f in findings if f.rule_id == "P016"]
    assert len(msgs) == 1 and "rogue" in msgs[0]


def test_p016_multi_mode_aliases_do_not_move_the_wire_name(tmp_path: Path) -> None:
    """Aliases move Temporal registration only — subdir names still match."""
    findings = _run(
        tmp_path,
        {
            "app/connector.py": dedent("""\
                from application_sdk.app import App, entrypoint
                class MyApp(App):
                    legacy_workflow_types = {
                        "QueryIntelligenceWorkflow": "query-intelligence",
                        "KeifuWorkflow": "keifu",
                    }
                    @entrypoint(name="query-intelligence")
                    async def query_intelligence(self, input: Input) -> Output: ...
                    @entrypoint(name="keifu")
                    async def keifu(self, input: Input) -> Output: ...
            """)
        },
        ["query-intelligence", "keifu"],
    )
    assert _p016_ids(findings) == []


def _write_two_node_manifest(
    tmp_path: Path,
    aliased_node: dict,
    aliases: list[tuple[str, str]] | None = None,
) -> None:
    import json

    gen = tmp_path / "app" / "generated"
    gen.mkdir(parents=True)
    manifest = {
        "dag": {
            "local": {
                "workflow_type": "app:extract-metadata",
                "inputs": {"app_name": "app", "task_queue": "shared-queue"},
            },
            "aliased": aliased_node,
        },
        **(_alias_block(aliases) if aliases else {}),
    }
    (gen / "manifest.json").write_text(json.dumps(manifest))


_ALIASED_APP_SOURCE = dedent("""\
    from application_sdk.app import App, entrypoint
    class MyApp(App):
        name = "app"
        legacy_workflow_types = {"OverrideWorkflow": "keifu"}
        @entrypoint(name="extract-metadata")
        async def extract_metadata(self, input: Input) -> Output: ...
        @entrypoint(name="keifu")
        async def keifu(self, input: Input) -> Output: ...
""")


@pytest.mark.parametrize(
    "aliased_node",
    [
        {"workflow_type": "OverrideWorkflow"},
        {"workflow_type": "OverrideWorkflow", "inputs": {"task_queue": "shared-queue"}},
        {"workflow_type": "OverrideWorkflow", "inputs": {"app_name": "app"}},
    ],
    ids=["bare", "app-nameless-with-queue", "names-this-app"],
)
def test_p016_declared_alias_routes_when_node_identity_does_not_contradict(
    tmp_path: Path, aliased_node: dict
) -> None:
    """F3: a declared alias routes through a node that carries no app_name or
    names this app — task_queue plays no part."""
    _write_two_node_manifest(
        tmp_path, aliased_node, aliases=[("OverrideWorkflow", "keifu")]
    )
    paths = _write_py(tmp_path, {"app/connector.py": _ALIASED_APP_SOURCE})
    findings = scan_all(paths, tmp_path)
    assert _p016_ids(findings) == []


def test_p016_declared_alias_naming_a_foreign_node_does_not_launder(
    tmp_path: Path,
) -> None:
    """A node whose app_name names a different app dispatches on that app's
    worker; declaring its type locally must not silence P016 on a genuinely
    unrouted entry point."""
    _write_two_node_manifest(
        tmp_path,
        {
            "workflow_type": "OverrideWorkflow",
            "inputs": {"app_name": "platform", "task_queue": "atlan-platform"},
        },
        aliases=[("OverrideWorkflow", "keifu")],
    )
    paths = _write_py(tmp_path, {"app/connector.py": _ALIASED_APP_SOURCE})
    findings = scan_all(paths, tmp_path)
    msgs = [f.message for f in findings if f.rule_id == "P016"]
    assert len(msgs) == 1 and "keifu" in msgs[0]


def _write_generated_shape_manifest(
    tmp_path: Path,
    aliased_node: dict,
    aliases: list[tuple[str, str]] | None = None,
) -> None:
    """A manifest shaped the way the toolkit actually generates one.

    The entry point's own node is `extract`, its `workflow_type` is **bare** (the
    kebab-cased contract name, no `<app>:` prefix), and its identity lives in
    `inputs.app_name`. No committed example manifest contains a colon-qualified
    workflow_type, so this — not the route/card-split shape — is the common case.
    """
    import json

    gen = tmp_path / "app" / "generated"
    gen.mkdir(parents=True)
    manifest = {
        "dag": {
            "extract": {
                "app_name": "app",
                "inputs": {
                    "workflow_type": "ExtractMetadataWorkflow",
                    "app_name": "app",
                    "task_queue": "atlan-app",
                },
            },
            "aliased": aliased_node,
        },
        **(_alias_block(aliases) if aliases else {}),
    }
    (gen / "manifest.json").write_text(json.dumps(manifest))


def test_p016_alias_routes_on_a_generated_shape_manifest(tmp_path: Path) -> None:
    """The manifest identity must come from the own node, not only from colons.

    Deriving `own_app_names` purely from `<app>:` prefixes left it empty on every
    manifest the toolkit generates, so a bare alias node carrying `inputs.app_name`
    never routed and P016 — a BLOCK rule — failed a correctly-wired app.
    """
    _write_generated_shape_manifest(
        tmp_path,
        {"workflow_type": "OverrideWorkflow", "inputs": {"app_name": "app"}},
        aliases=[("OverrideWorkflow", "keifu")],
    )
    paths = _write_py(
        tmp_path,
        {
            "app/connector.py": dedent("""\
                from application_sdk.app import App, entrypoint
                class MyApp(App):
                    name = "app"
                    legacy_workflow_types = {"OverrideWorkflow": "keifu"}
                    @entrypoint(name="extract-metadata")
                    async def extract_metadata(self, input: Input) -> Output: ...
                    @entrypoint(name="keifu")
                    async def keifu(self, input: Input) -> Output: ...
            """)
        },
    )
    findings = scan_all(paths, tmp_path)
    msgs = [f.message for f in findings if f.rule_id == "P016"]
    # "keifu" also appears in other messages' "declared routes" list, so match the
    # subject of the finding rather than the bare name.
    assert not any("Entry point 'keifu'" in m for m in msgs)


def test_p016_generated_shape_still_refuses_a_foreign_node(tmp_path: Path) -> None:
    """Widening identity to the own node must not widen it to every node.

    Same generated shape, but the aliased node names a different app — it
    dispatches on that app's worker, so the alias cannot reach `keifu` here.
    """
    _write_generated_shape_manifest(
        tmp_path,
        {"workflow_type": "OverrideWorkflow", "inputs": {"app_name": "platform"}},
        aliases=[("OverrideWorkflow", "keifu")],
    )
    paths = _write_py(
        tmp_path,
        {
            "app/connector.py": dedent("""\
                from application_sdk.app import App, entrypoint
                class MyApp(App):
                    name = "app"
                    legacy_workflow_types = {"OverrideWorkflow": "keifu"}
                    @entrypoint(name="extract-metadata")
                    async def extract_metadata(self, input: Input) -> Output: ...
                    @entrypoint(name="keifu")
                    async def keifu(self, input: Input) -> Output: ...
            """)
        },
    )
    findings = scan_all(paths, tmp_path)
    msgs = [f.message for f in findings if f.rule_id == "P016"]
    assert any("Entry point 'keifu'" in m for m in msgs)


def test_p016_foreign_colon_prefix_does_not_widen_identity(tmp_path: Path) -> None:
    """A sibling `<other>:<wire>` node must not launder a foreign bare node.

    Identity is seeded from the entry point's own node; a colon-qualified type
    naming a different app is that app's route, not a second identity of this
    one. Adding every prefix let `platform:other` admit a bare alias node whose
    `app_name` is `platform`, silencing P016 on a genuinely unrouted entry point.
    """
    import json

    gen = tmp_path / "app" / "generated"
    gen.mkdir(parents=True)
    manifest = {
        "dag": {
            "extract": {
                "inputs": {
                    "workflow_type": "ExtractMetadataWorkflow",
                    "app_name": "app",
                    "task_queue": "atlan-app",
                }
            },
            "foreign-route": {"workflow_type": "platform:other"},
            "aliased": {
                "workflow_type": "OverrideWorkflow",
                "inputs": {"app_name": "platform"},
            },
        },
        **_alias_block([("OverrideWorkflow", "keifu")]),
    }
    (gen / "manifest.json").write_text(json.dumps(manifest))
    paths = _write_py(tmp_path, {"app/connector.py": _ALIASED_APP_SOURCE})
    findings = scan_all(paths, tmp_path)
    msgs = [f.message for f in findings if f.rule_id == "P016"]
    assert any("Entry point 'keifu'" in m for m in msgs)


def test_p016_no_own_node_and_disagreeing_prefixes_stay_conservative(
    tmp_path: Path,
) -> None:
    """Without an own node, a single DAG-wide colon prefix is the only identity.

    When the colon prefixes disagree, no identity can be established, so a bare
    alias node carrying any `app_name` does not route — conservative for the
    same reason a foreign node does not.
    """
    import json

    gen = tmp_path / "app" / "generated"
    gen.mkdir(parents=True)
    manifest = {
        "dag": {
            "local": {
                "workflow_type": "app:extract-metadata",
                "inputs": {"app_name": "app", "task_queue": "shared-queue"},
            },
            "foreign-route": {"workflow_type": "platform:other"},
            "aliased": {
                "workflow_type": "OverrideWorkflow",
                "inputs": {"app_name": "platform"},
            },
        },
        **_alias_block([("OverrideWorkflow", "keifu")]),
    }
    (gen / "manifest.json").write_text(json.dumps(manifest))
    paths = _write_py(tmp_path, {"app/connector.py": _ALIASED_APP_SOURCE})
    findings = scan_all(paths, tmp_path)
    msgs = [f.message for f in findings if f.rule_id == "P016"]
    assert any("Entry point 'keifu'" in m for m in msgs)


def test_p016_a_second_app_class_does_not_launder_a_foreign_node(
    tmp_path: Path,
) -> None:
    """Node identity is checked against the manifest, not against code-side names.

    A repo defining a second App whose name matches the foreign node's `app_name`
    must not thereby route the alias: the node still dispatches on that other
    app's worker. Unioning the code-side App names made exactly that mistake.
    """
    _write_two_node_manifest(
        tmp_path,
        {
            "workflow_type": "OverrideWorkflow",
            "inputs": {"app_name": "platform", "task_queue": "atlan-platform"},
        },
        aliases=[("OverrideWorkflow", "keifu")],
    )
    paths = _write_py(
        tmp_path,
        {
            "app/connector.py": _ALIASED_APP_SOURCE,
            "app/platform.py": dedent("""\
                from application_sdk.app import App
                class PlatformApp(App):
                    name = "platform"
            """),
        },
    )
    findings = scan_all(paths, tmp_path)
    msgs = [f.message for f in findings if f.rule_id == "P016"]
    assert len(msgs) == 1 and "keifu" in msgs[0]


def test_p016_undeclared_bare_node_on_shared_queue_does_not_route(
    tmp_path: Path,
) -> None:
    """F3: without a declaration, a shared queue proves nothing."""
    import json

    gen = tmp_path / "app" / "generated"
    gen.mkdir(parents=True)
    manifest = {
        "dag": {
            "local": {
                "workflow_type": "app:extract-metadata",
                "inputs": {"app_name": "app", "task_queue": "shared-queue"},
            },
            "foreign": {
                "workflow_type": "OverrideWorkflow",
                "inputs": {"task_queue": "shared-queue"},
            },
        }
    }
    (gen / "manifest.json").write_text(json.dumps(manifest))
    paths = _write_py(
        tmp_path,
        {
            "app/connector.py": dedent("""\
                from application_sdk.app import App, entrypoint
                class MyApp(App):
                    name = "app"
                    @entrypoint(name="extract-metadata")
                    async def extract_metadata(self, input: Input) -> Output: ...
                    @entrypoint(name="rogue")
                    async def rogue(self, input: Input) -> Output: ...
            """)
        },
    )
    findings = scan_all(paths, tmp_path)
    msgs = [f.message for f in findings if f.rule_id == "P016"]
    assert len(msgs) == 1 and "rogue" in msgs[0]


def test_p016_ignores_a_non_literal_alias_declaration(tmp_path: Path) -> None:
    """P016 no longer reads the class attribute, so its shape is not its concern.

    The manifest declares the alias, so the entry point routes and P016 stays
    silent even though the class attribute is unreadable. Reporting that
    declaration is K015's job (CONNECT-1081).
    """
    _write_single_manifest_with_routes(
        tmp_path,
        ["app:extract-metadata", "KeifuWorkflow"],
        aliases=[("KeifuWorkflow", "keifu")],
    )
    paths = _write_py(
        tmp_path,
        {
            "app/connector.py": dedent("""\
                from application_sdk.app import App, entrypoint
                ALIASES = {"KeifuWorkflow": "keifu"}
                class MyApp(App):
                    name = "app"
                    legacy_workflow_types = ALIASES
                    @entrypoint(name="extract-metadata")
                    async def extract_metadata(self, input: Input) -> Output: ...
                    @entrypoint(name="keifu")
                    async def keifu(self, input: Input) -> Output: ...
            """)
        },
    )
    findings = scan_all(paths, tmp_path)
    assert _p016_ids(findings) == []
