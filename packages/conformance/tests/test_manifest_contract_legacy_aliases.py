"""Tests for K015 LegacyWorkflowTypeContractDrift (CONNECT-1081).

The generated manifest's ``legacy_workflow_types`` block is the contracted
declaration site for inbound-only Temporal workflow type aliases; the SDK's
``App.legacy_workflow_types`` class attribute is what registers them with the
worker. K015 holds the two in agreement.

Test helpers
------------
``_write_py``: writes ``{relative_path: source_text}`` under ``tmp_path``.
``_write_manifest``: writes one ``manifest.json`` with an optional alias block.
``_k015``: the unsuppressed K015 messages from a scan.
"""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent

from conformance.suite.checks.manifest_contract import scan_all
from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import EnforcementTier, RuleScope

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_py(tmp_path: Path, py_files: dict[str, str]) -> list[Path]:
    paths: list[Path] = []
    for name, src in py_files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src, encoding="utf-8")
        paths.append(p)
    return paths


def _write_manifest(
    path: Path,
    aliases: list[tuple[str, str]] | None = None,
    removal_version: str | None = None,
) -> None:
    manifest: dict = {"dag": {"extract": {"workflow_type": "myapp:extract-metadata"}}}
    if aliases is not None:
        block: dict = {
            "aliases": [
                {"alias": alias, "entrypoint": target} for alias, target in aliases
            ]
        }
        if removal_version is not None:
            block["removal_version"] = removal_version
        manifest["legacy_workflow_types"] = block
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest), encoding="utf-8")


def _k015(findings: list) -> list[str]:
    return [f.message for f in findings if f.rule_id == "K015" and not f.suppressed]


def _app_source(
    aliases: str = '{"LegacyCrawlerWorkflow": "crawler"}',
    removal_version: str | None = None,
    preamble: str = "",
) -> str:
    """An App subclass declaring *aliases*, with a real ``@entrypoint`` in its body.

    Built by joining whole lines rather than concatenating ``dedent`` blocks: a
    trailing block would be dedented against its own indentation and drop the
    class body out from under the class.
    """
    lines = [
        "from application_sdk.app import App, entrypoint",
        *([preamble] if preamble else []),
        "class MyApp(App):",
        '    name = "myapp"',
        f"    legacy_workflow_types = {aliases}",
    ]
    if removal_version is not None:
        lines.append(f'    legacy_workflow_types_removal_version = "{removal_version}"')
    lines += [
        '    @entrypoint(name="crawler")',
        "    async def crawler(self, input: Input) -> Output: ...",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Rule metadata
# ---------------------------------------------------------------------------


def test_k015_rule_metadata() -> None:
    rule = get_rule("K015")
    assert rule.name == "LegacyWorkflowTypeContractDrift"
    # BLOCK, not WARN: P016 is blocking and now routes off the manifest block, so a
    # drifted block changes what another blocking rule concludes.
    assert rule.tier is EnforcementTier.BLOCK
    assert rule.scope is RuleScope.APP
    assert rule.category == "contract-toolkit"


# ---------------------------------------------------------------------------
# Agreement
# ---------------------------------------------------------------------------


def test_k015_silent_when_manifest_and_code_agree(tmp_path: Path) -> None:
    _write_manifest(
        tmp_path / "app" / "generated" / "manifest.json",
        aliases=[("LegacyCrawlerWorkflow", "crawler")],
    )
    paths = _write_py(tmp_path, {"app/connector.py": _app_source()})
    assert _k015(scan_all(paths, tmp_path)) == []


def test_k015_silent_when_neither_site_declares_anything(tmp_path: Path) -> None:
    """The overwhelmingly common shape: no aliases anywhere, no finding."""
    _write_manifest(tmp_path / "app" / "generated" / "manifest.json")
    paths = _write_py(
        tmp_path,
        {
            "app/connector.py": dedent("""\
                from application_sdk.app import App, entrypoint
                class MyApp(App):
                    name = "myapp"
                    @entrypoint(name="crawler")
                    async def crawler(self, input: Input) -> Output: ...
            """)
        },
    )
    assert _k015(scan_all(paths, tmp_path)) == []


# ---------------------------------------------------------------------------
# Drift
# ---------------------------------------------------------------------------


def test_k015_fires_when_code_declares_an_alias_the_manifest_omits(
    tmp_path: Path,
) -> None:
    """The contracted declaration site is mandatory once app/generated/ exists."""
    _write_manifest(tmp_path / "app" / "generated" / "manifest.json")
    paths = _write_py(tmp_path, {"app/connector.py": _app_source()})
    msgs = _k015(scan_all(paths, tmp_path))
    assert len(msgs) == 1
    assert "LegacyCrawlerWorkflow -> crawler" in msgs[0]
    assert "the app contract does not" in msgs[0]


def test_k015_fires_when_the_manifest_declares_an_alias_the_app_does_not(
    tmp_path: Path,
) -> None:
    """The dangerous direction: the contract advertises a type the worker rejects."""
    _write_manifest(
        tmp_path / "app" / "generated" / "manifest.json",
        aliases=[("LegacyCrawlerWorkflow", "crawler")],
    )
    paths = _write_py(
        tmp_path,
        {
            "app/connector.py": dedent("""\
                from application_sdk.app import App, entrypoint
                class MyApp(App):
                    name = "myapp"
                    @entrypoint(name="crawler")
                    async def crawler(self, input: Input) -> Output: ...
            """)
        },
    )
    msgs = _k015(scan_all(paths, tmp_path))
    assert len(msgs) == 1
    assert "the SDK App does not" in msgs[0]
    assert "LegacyCrawlerWorkflow -> crawler" in msgs[0]


def test_k015_fires_when_an_alias_targets_a_different_entrypoint(
    tmp_path: Path,
) -> None:
    """Same alias, different target — reported in both directions."""
    _write_manifest(
        tmp_path / "app" / "generated" / "manifest.json",
        aliases=[("LegacyCrawlerWorkflow", "miner")],
    )
    paths = _write_py(tmp_path, {"app/connector.py": _app_source()})
    msgs = _k015(scan_all(paths, tmp_path))
    assert len(msgs) == 2
    assert any("LegacyCrawlerWorkflow -> crawler" in m for m in msgs)
    assert any("LegacyCrawlerWorkflow -> miner" in m for m in msgs)


def test_k015_fires_on_a_removal_version_mismatch(tmp_path: Path) -> None:
    _write_manifest(
        tmp_path / "app" / "generated" / "manifest.json",
        aliases=[("LegacyCrawlerWorkflow", "crawler")],
        removal_version="4.2.0",
    )
    paths = _write_py(
        tmp_path, {"app/connector.py": _app_source(removal_version="5.0.0")}
    )
    msgs = _k015(scan_all(paths, tmp_path))
    assert len(msgs) == 1
    assert "legacy_workflow_types_removal_version" in msgs[0]


def test_k015_fires_when_only_the_manifest_declares_an_expiry(tmp_path: Path) -> None:
    """An omitted removal_version means "no expiry", not "unspecified"."""
    _write_manifest(
        tmp_path / "app" / "generated" / "manifest.json",
        aliases=[("LegacyCrawlerWorkflow", "crawler")],
        removal_version="4.2.0",
    )
    paths = _write_py(tmp_path, {"app/connector.py": _app_source()})
    msgs = _k015(scan_all(paths, tmp_path))
    assert len(msgs) == 1
    assert "legacy_workflow_types_removal_version" in msgs[0]


def test_k015_accepts_a_matching_expiry(tmp_path: Path) -> None:
    _write_manifest(
        tmp_path / "app" / "generated" / "manifest.json",
        aliases=[("LegacyCrawlerWorkflow", "crawler")],
        removal_version="4.2.0",
    )
    paths = _write_py(
        tmp_path, {"app/connector.py": _app_source(removal_version="4.2.0")}
    )
    assert _k015(scan_all(paths, tmp_path)) == []


def test_k015_fires_on_a_non_literal_declaration(tmp_path: Path) -> None:
    """An unreadable declaration blocks the comparison; say so, do not guess.

    This finding moved here from P016 (CONNECT-1081): P016 no longer reads the
    class attribute at all, so it can no longer judge its shape.
    """
    _write_manifest(
        tmp_path / "app" / "generated" / "manifest.json",
        aliases=[("LegacyCrawlerWorkflow", "crawler")],
    )
    paths = _write_py(
        tmp_path,
        {
            "app/connector.py": _app_source(
                aliases="ALIASES",
                preamble='ALIASES = {"LegacyCrawlerWorkflow": "crawler"}',
            )
        },
    )
    msgs = _k015(scan_all(paths, tmp_path))
    assert len(msgs) == 1
    assert "not a literal declaration" in msgs[0]


def test_k015_reads_a_declaration_that_carries_a_separate_annotation(
    tmp_path: Path,
) -> None:
    """``attr: ClassVar[...]`` then ``attr = ...`` is one attribute with a value.

    Stopping the class-body scan at the bare annotation read a real declaration
    as absent, which surfaced as a false "the SDK App does not declare this".
    """
    _write_manifest(
        tmp_path / "app" / "generated" / "manifest.json",
        aliases=[("LegacyCrawlerWorkflow", "crawler")],
    )
    source = dedent("""\
        from typing import ClassVar
        from application_sdk.app import App, entrypoint
        class MyApp(App):
            name: ClassVar[str]
            name = "myapp"
            legacy_workflow_types: ClassVar[dict]
            legacy_workflow_types = {"LegacyCrawlerWorkflow": "crawler"}
            @entrypoint(name="crawler")
            async def crawler(self, input: Input) -> Output: ...
    """)
    assert (
        _k015(scan_all(_write_py(tmp_path, {"app/connector.py": source}), tmp_path))
        == []
    )


def test_k015_fires_on_a_non_literal_removal_version(tmp_path: Path) -> None:
    _write_manifest(
        tmp_path / "app" / "generated" / "manifest.json",
        aliases=[("LegacyCrawlerWorkflow", "crawler")],
    )
    source = dedent("""\
        from application_sdk.app import App, entrypoint
        VERSION = "4.2.0"
        class MyApp(App):
            name = "myapp"
            legacy_workflow_types = {"LegacyCrawlerWorkflow": "crawler"}
            legacy_workflow_types_removal_version = VERSION
            @entrypoint(name="crawler")
            async def crawler(self, input: Input) -> Output: ...
    """)
    msgs = _k015(scan_all(_write_py(tmp_path, {"app/connector.py": source}), tmp_path))
    assert len(msgs) == 1
    assert "not a literal declaration" in msgs[0]


# ---------------------------------------------------------------------------
# Multi-entrypoint mode
# ---------------------------------------------------------------------------


def test_k015_reads_every_entrypoint_manifest_in_multi_mode(tmp_path: Path) -> None:
    """The block is app-level, so each per-entrypoint manifest carries a copy.

    This is the shape `contract-toolkit/examples/bundle` generates: both the
    crawler's and the miner's manifest carry the identical block, including the
    miner's, whose alias targets the crawler.
    """
    generated = tmp_path / "app" / "generated"
    for name in ("crawler", "miner"):
        _write_manifest(
            generated / name / "manifest.json",
            aliases=[("LegacyCrawlerWorkflow", "crawler")],
        )
    paths = _write_py(tmp_path, {"app/connector.py": _app_source()})
    assert _k015(scan_all(paths, tmp_path)) == []


def test_k015_fires_when_per_entrypoint_manifests_disagree(tmp_path: Path) -> None:
    """One entry point regenerated and another not — the copies must be identical."""
    generated = tmp_path / "app" / "generated"
    _write_manifest(
        generated / "crawler" / "manifest.json",
        aliases=[("LegacyCrawlerWorkflow", "crawler")],
    )
    _write_manifest(generated / "miner" / "manifest.json")
    paths = _write_py(tmp_path, {"app/connector.py": _app_source()})
    msgs = _k015(scan_all(paths, tmp_path))
    assert len(msgs) == 1
    assert "disagree on legacy_workflow_types" in msgs[0]


def test_k015_accepts_the_committed_bundle_example_shape(tmp_path: Path) -> None:
    """Guard against the rule contradicting the toolkit's own generated output.

    The generator, the docs and this rule all have to agree on one declaration
    model. They disagreed once: the example declared the block on the crawler
    only, and K015 reported the correct output as unfixable drift.
    """
    repo_root = Path(__file__).resolve().parents[3]
    bundle = (
        repo_root / "contract-toolkit" / "examples" / "bundle" / "app" / "generated"
    )
    generated = tmp_path / "app" / "generated"
    for name in ("crawler", "miner"):
        target = generated / name / "manifest.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            (bundle / name / "manifest.json").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    paths = _write_py(
        tmp_path,
        {
            "app/connector.py": _app_source(
                aliases='{"SnowflakeCrawlerWorkflow": "crawler"}'
            )
        },
    )
    assert _k015(scan_all(paths, tmp_path)) == []


# ---------------------------------------------------------------------------
# No-ops and suppression
# ---------------------------------------------------------------------------


def test_k015_no_ops_without_a_generated_tree(tmp_path: Path) -> None:
    """Absent mode: the class attribute is the only declaration site."""
    paths = _write_py(tmp_path, {"app/connector.py": _app_source()})
    assert _k015(scan_all(paths, tmp_path)) == []


def test_k015_no_ops_when_no_app_subclass_is_found(tmp_path: Path) -> None:
    _write_manifest(
        tmp_path / "app" / "generated" / "manifest.json",
        aliases=[("LegacyCrawlerWorkflow", "crawler")],
    )
    paths = _write_py(
        tmp_path, {"app/helpers.py": "def helper() -> None:\n    return None\n"}
    )
    assert _k015(scan_all(paths, tmp_path)) == []


def test_k015_is_suppressible_inline(tmp_path: Path) -> None:
    _write_manifest(tmp_path / "app" / "generated" / "manifest.json")
    source = dedent("""\
        from application_sdk.app import App, entrypoint
        # conformance: ignore[K015] callers still migrating; contract lands next release
        class MyApp(App):
            name = "myapp"
            legacy_workflow_types = {"LegacyCrawlerWorkflow": "crawler"}
            @entrypoint(name="crawler")
            async def crawler(self, input: Input) -> Output: ...
    """)
    findings = scan_all(_write_py(tmp_path, {"app/connector.py": source}), tmp_path)
    k015 = [f for f in findings if f.rule_id == "K015"]
    assert k015 and all(f.suppressed for f in k015)
    assert _k015(findings) == []
