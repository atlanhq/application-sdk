"""Tests for K001 ContractAmendsLegacyModule and K002 LegacyContractApi checks.

Each test builds a minimal temporary repo tree in ``tmp_path``, writes one or
more ``contract/**/*.pkl`` files, calls the scanner, and asserts on the
returned findings.

Naming conventions
------------------
* ``pkl_content``: the raw text to write to ``contract/app.pkl``
  (or any path under ``contract/``).
* Findings are filtered by ``rule_id`` for precise assertions.
* ``suppressed=True`` findings are returned by the scanner but carry the
  ``suppressed`` flag; the runner treats them as non-blocking.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from conformance.suite.checks.legacy_contract import discover, scan_path
from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import EnforcementTier, RuleScope

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_repo(tmp_path: Path, pkl_files: dict[str, str]) -> list[Path]:
    """Write ``contract/**/*.pkl`` files under *tmp_path*.

    Returns the list of paths actually written, mirroring the output of
    :func:`discover`.
    """
    paths: list[Path] = []
    for rel, content in pkl_files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        paths.append(p)
    return paths


def _scan_all(tmp_path: Path, pkl_files: dict[str, str]) -> list:
    """Write files and run scan_path on each, aggregating findings."""
    paths = _write_repo(tmp_path, pkl_files)
    findings = []
    for path in paths:
        findings.extend(scan_path(path, tmp_path))
    return findings


# ---------------------------------------------------------------------------
# Conformant contract — no findings
# ---------------------------------------------------------------------------


def test_conformant_app_pkl_no_findings(tmp_path: Path) -> None:
    """A contract that amends App.pkl and uses no legacy APIs → zero findings."""
    content = dedent("""\
        amends "@app-contract-toolkit/App.pkl"

        name = "my-connector"
        connector = Connectors.MYSQL
    """)
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    assert findings == []


def test_no_contract_dir_no_findings(tmp_path: Path) -> None:
    """A repo with no ``contract/`` directory → discover returns [] → no findings."""
    paths = discover(tmp_path)
    assert paths == []


# ---------------------------------------------------------------------------
# K001 — amends legacy base module
# ---------------------------------------------------------------------------


def test_k001_nativeapp(tmp_path: Path) -> None:
    """K001 fires for ``amends "@app-contract-toolkit/NativeApp.pkl"``."""
    content = dedent("""\
        amends "@app-contract-toolkit/NativeApp.pkl"

        name = "my-connector"
    """)
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    k001 = [f for f in findings if f.rule_id == "K001"]
    assert len(k001) == 1
    assert k001[0].line == 1
    assert "NativeApp.pkl" in k001[0].message
    assert "App.pkl" in k001[0].message
    assert not k001[0].suppressed


def test_k001_nativeappbundle(tmp_path: Path) -> None:
    """K001 fires for ``amends "@app-contract-toolkit/NativeAppBundle.pkl"``."""
    content = dedent("""\
        amends "@app-contract-toolkit/NativeAppBundle.pkl"
    """)
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    k001 = [f for f in findings if f.rule_id == "K001"]
    assert len(k001) == 1
    assert "NativeAppBundle.pkl" in k001[0].message


def test_k001_relative_path(tmp_path: Path) -> None:
    """K001 also fires for relative-path amends (as used in toolkit examples)."""
    content = 'amends "../../src/NativeApp.pkl"\n'
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    k001 = [f for f in findings if f.rule_id == "K001"]
    assert len(k001) == 1


def test_k001_not_fired_for_app_pkl(tmp_path: Path) -> None:
    """K001 must NOT fire for ``amends "@app-contract-toolkit/App.pkl"``."""
    content = 'amends "@app-contract-toolkit/App.pkl"\n'
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    k001 = [f for f in findings if f.rule_id == "K001"]
    assert k001 == []


def test_k001_not_fired_for_commented_out_amends(tmp_path: Path) -> None:
    """A commented-out legacy amends line must NOT produce a K001 finding."""
    content = dedent("""\
        // amends "@app-contract-toolkit/NativeApp.pkl"
        amends "@app-contract-toolkit/App.pkl"
    """)
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    k001 = [f for f in findings if f.rule_id == "K001"]
    assert k001 == []


def test_k001_not_fired_for_block_commented_amends(tmp_path: Path) -> None:
    """A block-comment-wrapped legacy amends must NOT produce a K001 finding."""
    content = dedent("""\
        /*
        amends "@app-contract-toolkit/NativeApp.pkl"
        */
        amends "@app-contract-toolkit/App.pkl"
    """)
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    k001 = [f for f in findings if f.rule_id == "K001"]
    assert k001 == []


def test_k001_suppressed_by_directive_on_same_line(tmp_path: Path) -> None:
    """``// conformance: ignore[K001] reason`` on the amends line suppresses K001."""
    content = (
        'amends "@app-contract-toolkit/NativeApp.pkl" '
        "// conformance: ignore[K001] phased migration tracked in BLDX-9999\n"
    )
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    k001 = [f for f in findings if f.rule_id == "K001"]
    assert len(k001) == 1
    assert k001[0].suppressed
    assert "BLDX-9999" in (k001[0].suppression_justification or "")


def test_k001_suppressed_by_directive_on_line_above(tmp_path: Path) -> None:
    """A comment-only directive on the line directly above suppresses K001."""
    content = dedent("""\
        // conformance: ignore[K001] phased migration: BLDX-9999
        amends "@app-contract-toolkit/NativeApp.pkl"
    """)
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    k001 = [f for f in findings if f.rule_id == "K001"]
    assert len(k001) == 1
    assert k001[0].suppressed


def test_k001_no_suppression_without_justification(tmp_path: Path) -> None:
    """A bare ``// conformance: ignore[K001]`` with no text must NOT suppress."""
    content = dedent("""\
        // conformance: ignore[K001]
        amends "@app-contract-toolkit/NativeApp.pkl"
    """)
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    k001 = [f for f in findings if f.rule_id == "K001"]
    assert len(k001) == 1
    assert not k001[0].suppressed


def test_k001_multiple_entrypoint_files(tmp_path: Path) -> None:
    """Each legacy contract file in a multi-entrypoint repo produces its own K001."""
    crawler = 'amends "@app-contract-toolkit/NativeApp.pkl"\nname = "crawler"\n'
    miner = 'amends "@app-contract-toolkit/NativeApp.pkl"\nname = "miner"\n'
    findings = _scan_all(
        tmp_path,
        {
            "contract/crawler.pkl": crawler,
            "contract/miner.pkl": miner,
        },
    )
    k001 = [f for f in findings if f.rule_id == "K001"]
    assert len(k001) == 2
    files = {f.file for f in k001}
    assert any("crawler.pkl" in fn for fn in files)
    assert any("miner.pkl" in fn for fn in files)


# ---------------------------------------------------------------------------
# K002 — NativeApp-only properties
# ---------------------------------------------------------------------------


def test_k002_flat_manifest_args(tmp_path: Path) -> None:
    """K002 fires for ``flatManifestArgs``."""
    content = dedent("""\
        amends "@app-contract-toolkit/App.pkl"
        flatManifestArgs = true
    """)
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    k002 = [f for f in findings if f.rule_id == "K002"]
    assert len(k002) == 1
    assert "flatManifestArgs" in k002[0].message
    assert "always emits flat" in k002[0].message


def test_k002_manifest_metadata_args(tmp_path: Path) -> None:
    """K002 fires for ``manifestMetadataArgs``."""
    content = dedent("""\
        amends "@app-contract-toolkit/App.pkl"
        manifestMetadataArgs = false
    """)
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    k002 = [f for f in findings if f.rule_id == "K002"]
    assert len(k002) == 1
    assert "manifestMetadataArgs" in k002[0].message


def test_k002_workflow_type_override(tmp_path: Path) -> None:
    """K002 fires for ``workflowTypeOverride``."""
    content = dedent("""\
        amends "@app-contract-toolkit/App.pkl"
        workflowTypeOverride = "my-custom-type"
    """)
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    k002 = [f for f in findings if f.rule_id == "K002"]
    assert len(k002) == 1
    assert "workflowTypeOverride" in k002[0].message


def test_k002_config_import(tmp_path: Path) -> None:
    """K002 fires for ``import "…Config.pkl"``."""
    content = dedent("""\
        amends "@app-contract-toolkit/App.pkl"
        import "@app-contract-toolkit/Config.pkl"
    """)
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    k002 = [f for f in findings if f.rule_id == "K002"]
    assert len(k002) == 1
    assert "Config.pkl" in k002[0].message
    assert "re-exports widget types" in k002[0].message


def test_k002_connectors_import(tmp_path: Path) -> None:
    """K002 fires for ``import "…Connectors.pkl"``."""
    content = 'amends "@app-contract-toolkit/App.pkl"\nimport "@app-contract-toolkit/Connectors.pkl"\n'
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    k002 = [f for f in findings if f.rule_id == "K002"]
    assert len(k002) == 1
    assert "Connectors.pkl" in k002[0].message


def test_k002_credential_import(tmp_path: Path) -> None:
    """K002 fires for ``import "…Credential.pkl"`` (Argo-era)."""
    content = 'amends "@app-contract-toolkit/App.pkl"\nimport "@app-contract-toolkit/Credential.pkl"\n'
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    k002 = [f for f in findings if f.rule_id == "K002"]
    assert len(k002) == 1
    assert "Argo-era" in k002[0].message


def test_k002_renderers_import(tmp_path: Path) -> None:
    """K002 fires for ``import "…Renderers.pkl"`` (Argo-era)."""
    content = 'amends "@app-contract-toolkit/App.pkl"\nimport "@app-contract-toolkit/Renderers.pkl"\n'
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    k002 = [f for f in findings if f.rule_id == "K002"]
    assert len(k002) == 1
    assert "Argo-era" in k002[0].message


def test_k002_property_in_line_comment_not_flagged(tmp_path: Path) -> None:
    """A property name that appears only in a line comment must NOT fire K002."""
    content = dedent("""\
        amends "@app-contract-toolkit/App.pkl"
        // flatManifestArgs is a NativeApp.pkl-only knob, not used here
        name = "my-app"
    """)
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    k002 = [f for f in findings if f.rule_id == "K002"]
    assert k002 == []


def test_k002_suppressed(tmp_path: Path) -> None:
    """A ``// conformance: ignore[K002] reason`` directive suppresses K002."""
    content = dedent("""\
        amends "@app-contract-toolkit/App.pkl"
        // conformance: ignore[K002] flatManifestArgs intentionally kept during migration
        flatManifestArgs = true
    """)
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    k002 = [f for f in findings if f.rule_id == "K002"]
    assert len(k002) == 1
    assert k002[0].suppressed


# ---------------------------------------------------------------------------
# Combined K001 + K002 (typical legacy contract)
# ---------------------------------------------------------------------------


def test_k001_and_k002_together(tmp_path: Path) -> None:
    """A typical legacy NativeApp.pkl contract fires K001 + multiple K002."""
    content = dedent("""\
        amends "@app-contract-toolkit/NativeApp.pkl"

        import "@app-contract-toolkit/Config.pkl"
        import "@app-contract-toolkit/Connectors.pkl"

        name = "my-connector"
        connector = Connectors.MYSQL
        flatManifestArgs = true
        workflowTypeOverride = "MyConnector"
    """)
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    k001 = [f for f in findings if f.rule_id == "K001"]
    k002 = [f for f in findings if f.rule_id == "K002"]
    assert len(k001) == 1
    # flatManifestArgs + workflowTypeOverride + Config.pkl import + Connectors.pkl import
    assert len(k002) == 4
    # All unsuppressed
    assert all(not f.suppressed for f in k001 + k002)


# ---------------------------------------------------------------------------
# discover() and scan_path()
# ---------------------------------------------------------------------------


def test_discover_finds_pkl_files(tmp_path: Path) -> None:
    """discover() returns all .pkl files under contract/."""
    _write_repo(
        tmp_path,
        {
            "contract/app.pkl": "amends ...\n",
            "contract/sub/entry.pkl": "amends ...\n",
            "app/generated/manifest.json": "{}",  # not a .pkl, not in contract/
        },
    )
    paths = discover(tmp_path)
    assert len(paths) == 2
    assert all(p.suffix == ".pkl" for p in paths)
    assert all("contract" in str(p) for p in paths)


def test_discover_returns_empty_when_no_contract_dir(tmp_path: Path) -> None:
    """discover() returns [] when no contract/ directory exists."""
    assert discover(tmp_path) == []


def test_scan_path_reads_file(tmp_path: Path) -> None:
    """scan_path reads the file and delegates to scan_text."""
    p = tmp_path / "contract" / "app.pkl"
    p.parent.mkdir(parents=True)
    p.write_text('amends "@app-contract-toolkit/NativeApp.pkl"\n', encoding="utf-8")
    findings = scan_path(p, tmp_path)
    assert any(f.rule_id == "K001" for f in findings)


def test_scan_path_graceful_on_missing_file(tmp_path: Path) -> None:
    """scan_path returns [] for a non-existent file (never raises OSError)."""
    absent = tmp_path / "contract" / "app.pkl"
    # Do NOT create the directory or file
    findings = scan_path(absent, tmp_path)
    assert findings == []


# ---------------------------------------------------------------------------
# Rule-metadata assertions
# ---------------------------------------------------------------------------


def test_k001_rule_metadata() -> None:
    """K001 has the correct tier, scope, mechanism, and orthogonal_gate."""
    rule = get_rule("K001")
    assert rule.name == "ContractAmendsLegacyModule"
    assert rule.tier == EnforcementTier.WARN
    assert rule.scope == RuleScope.APP
    assert rule.autofixable is False
    assert rule.orthogonal_gate == "pkl-eval"
    assert rule.help_uri is not None
    assert "#k001" in rule.help_uri


def test_k002_rule_metadata() -> None:
    """K002 has the correct tier, scope, mechanism, and orthogonal_gate."""
    rule = get_rule("K002")
    assert rule.name == "LegacyContractApi"
    assert rule.tier == EnforcementTier.WARN
    assert rule.scope == RuleScope.APP
    assert rule.autofixable is False
    assert rule.orthogonal_gate == "pkl-eval"
    assert rule.help_uri is not None
    assert "#k002" in rule.help_uri
