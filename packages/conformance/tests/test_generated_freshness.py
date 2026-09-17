"""Tests for K003/K004/K005 generated-artifact freshness (BLDX-1414).

Each test builds a minimal temporary app tree in ``tmp_path`` (a ``contract/``
directory plus generated artifacts), runs :func:`scan_all` over the discovered
paths, and asserts on the returned findings by ``rule_id``.
"""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent

from conformance.suite.checks._toolkit_baseline import load_baseline
from conformance.suite.checks.generated_freshness import (
    _declared_entrypoints,
    discover,
    scan_all,
)
from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import EnforcementTier, RuleScope

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

# The toolkit version floor (K007) and source check (K008) compare against the
# committed baseline, so the clean fixtures track it rather than a literal — that
# keeps this suite green across toolkit bumps (a bump regenerates the baseline).
_BASELINE = load_baseline()
assert _BASELINE is not None, "data/toolkit_baseline.json must be committed"
_LATEST = _BASELINE.latest_version
_CANONICAL = _BASELINE.canonical_base
# Comfortably older than any real toolkit release, for outdated/drift cases.
_OLDER = "0.0.1"


def _pkl_project(version: str = _LATEST, base: str = _CANONICAL) -> str:
    return (
        'amends "pkl:Project"\n'
        "dependencies {\n"
        '  ["app-contract-toolkit"] {\n'
        f'    uri = "package://{base}@{version}"\n'
        "  }\n"
        "}\n"
    )


def _deps_json(
    resolved: str = _LATEST, base: str = _CANONICAL, present: bool = True
) -> str:
    deps: dict[str, object] = {}
    if present:
        deps[f"package://{base}@0"] = {
            "type": "remote",
            "uri": f"projectpackage://{base}@{resolved}",
            "checksums": {"sha256": "deadbeef"},
        }
    return (
        json.dumps({"schemaVersion": 1, "resolvedDependencies": deps}, indent=2) + "\n"
    )


_PKL_PROJECT = _pkl_project()
_DEPS_JSON = _deps_json()

_APP_PKL = dedent("""\
    amends "@app-contract-toolkit/App.pkl"

    name = "demo"
""")

_BANNER = "# AUTO-GENERATED from contract/app.pkl — DO NOT EDIT MANUALLY.\n"
_BANNER_VARIANT = (
    "# Generated from contract/app.pkl via contract-toolkit. DO NOT EDIT.\n"
)


def _clean_files() -> dict[str, str]:
    """A fully conformant generated app tree."""
    return {
        "contract/PklProject": _PKL_PROJECT,
        "contract/PklProject.deps.json": _DEPS_JSON,
        "contract/app.pkl": _APP_PKL,
        "atlan.yaml": _BANNER + "name: demo\n",
        "app/generated/manifest.json": "{}\n",
        "app/generated/_input.py": _BANNER + "x = 1\n",
        "app/generated/_e2e_base.py": _BANNER + "class BaseE2E:\n    pass\n",
        "app/generated/__init__.py": "",
    }


def _scan(tmp_path: Path, files: dict[str, str]) -> list:
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return scan_all(discover(tmp_path), tmp_path)


def _ids(findings: list) -> list[str]:
    return sorted(f.rule_id for f in findings)


# ---------------------------------------------------------------------------
# Clean tree / no-op cases
# ---------------------------------------------------------------------------


def test_clean_app_no_findings(tmp_path: Path) -> None:
    assert _scan(tmp_path, _clean_files()) == []


def test_no_contract_dir_no_findings(tmp_path: Path) -> None:
    """A repo with no contract/ (SDK-like) produces nothing even with stray files."""
    findings = _scan(
        tmp_path, {"atlan.yaml": "name: x\n", "app/generated/foo.py": "x = 1\n"}
    )
    assert findings == []


# ---------------------------------------------------------------------------
# K003 — contract lock drift
# ---------------------------------------------------------------------------


def test_k003_stale_lock(tmp_path: Path) -> None:
    files = _clean_files()
    # Pin at latest, lock resolves something else -> pin-vs-lock drift.
    files["contract/PklProject.deps.json"] = _deps_json(resolved=_OLDER)
    findings = [f for f in _scan(tmp_path, files) if f.rule_id == "K003"]
    assert len(findings) == 1
    assert findings[0].file == "contract/PklProject"
    assert not findings[0].suppressed


def test_k003_missing_lock(tmp_path: Path) -> None:
    files = _clean_files()
    del files["contract/PklProject.deps.json"]
    assert _ids([f for f in _scan(tmp_path, files) if f.rule_id == "K003"]) == ["K003"]


def test_k003_dependency_absent_from_lock(tmp_path: Path) -> None:
    files = _clean_files()
    files["contract/PklProject.deps.json"] = _deps_json(present=False)
    assert _ids([f for f in _scan(tmp_path, files) if f.rule_id == "K003"]) == ["K003"]


def test_k003_broad_pin_satisfied_no_finding(tmp_path: Path) -> None:
    """A broad pin (@0) is satisfied by any resolved 0.y.z — not drift."""
    files = _clean_files()
    files["contract/PklProject"] = _pkl_project(version="0")
    assert [f for f in _scan(tmp_path, files) if f.rule_id == "K003"] == []


def test_k003_suppressed_via_pkl_directive(tmp_path: Path) -> None:
    files = _clean_files()
    files["contract/PklProject.deps.json"] = _deps_json(resolved=_OLDER)
    files["contract/PklProject"] = _pkl_project().replace(
        "    uri =", "    // conformance: ignore[K003] phased bump\n    uri ="
    )
    findings = [f for f in _scan(tmp_path, files) if f.rule_id == "K003"]
    assert len(findings) == 1
    assert findings[0].suppressed
    assert findings[0].suppression_justification == "phased bump"


# ---------------------------------------------------------------------------
# K004 — missing generated artifact
# ---------------------------------------------------------------------------


def test_k004_missing_manifest(tmp_path: Path) -> None:
    files = _clean_files()
    del files["app/generated/manifest.json"]
    findings = [f for f in _scan(tmp_path, files) if f.rule_id == "K004"]
    assert len(findings) == 1
    assert findings[0].file == "contract/app.pkl"


def test_k004_all_outputs_missing(tmp_path: Path) -> None:
    files = {
        "contract/PklProject": _PKL_PROJECT,
        "contract/PklProject.deps.json": _DEPS_JSON,
        "contract/app.pkl": _APP_PKL,
    }
    # atlan.yaml, manifest.json, _input.py all absent -> three K004 findings.
    assert _ids([f for f in _scan(tmp_path, files) if f.rule_id == "K004"]) == [
        "K004",
        "K004",
        "K004",
    ]


def test_k004_no_contract_app_pkl_no_finding(tmp_path: Path) -> None:
    """No contract/app.pkl -> K004 does not fire even if outputs are absent."""
    files = {
        "contract/PklProject": _PKL_PROJECT,
        "contract/PklProject.deps.json": _DEPS_JSON,
    }
    assert [f for f in _scan(tmp_path, files) if f.rule_id == "K004"] == []


def test_k004_suppressed_via_pkl_directive(tmp_path: Path) -> None:
    files = _clean_files()
    del files["app/generated/manifest.json"]
    files["contract/app.pkl"] = (
        "// conformance: ignore[K004] utility app emits no manifest\n" + _APP_PKL
    )
    findings = [f for f in _scan(tmp_path, files) if f.rule_id == "K004"]
    assert len(findings) == 1
    assert findings[0].suppressed


_BUNDLE_APP_PKL = dedent("""\
    amends "@app-contract-toolkit/App.pkl"

    name = "demo"

    entrypoints {
      new Entrypoint {
        name = "crawler"
      }
    }
""")


def _bundle_files() -> dict[str, str]:
    """A conformant BUNDLE tree: generated artifacts live per-entrypoint."""
    files = _clean_files()
    files["contract/app.pkl"] = _BUNDLE_APP_PKL
    # A bundle emits nothing at the single-entrypoint paths...
    del files["app/generated/manifest.json"]
    del files["app/generated/_input.py"]
    del files["app/generated/_e2e_base.py"]
    # ...and one copy per entrypoint instead.
    files["app/generated/crawler/manifest.json"] = "{}\n"
    files["app/generated/crawler/_input.py"] = _BANNER + "x = 1\n"
    files["app/generated/crawler/_e2e_base.py"] = _BANNER + "class BaseE2E:\n    pass\n"
    files["app/generated/crawler/__init__.py"] = ""
    return files


def test_k004_bundle_per_entrypoint_outputs_no_finding(tmp_path: Path) -> None:
    """A bundle emits manifest.json / _input.py per-entrypoint — no K004.

    Regression: K004 hard-coded the single-entrypoint ``app/generated/`` prefix, so
    every bundle carried two permanently unsatisfiable findings whose remedy
    ("regenerate and commit") could not resolve them.
    """
    assert [f for f in _scan(tmp_path, _bundle_files()) if f.rule_id == "K004"] == []


def test_k004_bundle_missing_from_both_layouts_fires(tmp_path: Path) -> None:
    """Accepting either layout must not stop K004 firing on an ungenerated bundle."""
    files = _bundle_files()
    del files["app/generated/crawler/manifest.json"]
    findings = [f for f in _scan(tmp_path, files) if f.rule_id == "K004"]
    assert len(findings) == 1
    assert findings[0].file == "contract/app.pkl"
    # The remedy names the missing entrypoint, not a placeholder or the
    # single-entrypoint path.
    assert "app/generated/crawler/manifest.json" in findings[0].message


_TWO_ENTRYPOINT_APP_PKL = dedent("""\
    amends "@app-contract-toolkit/App.pkl"

    name = "demo"

    entrypoints {
      new Entrypoint {
        name = "crawler"
      }
      new Entrypoint {
        name = "miner"
      }
    }
""")


def test_declared_entrypoints_reads_toolkit_bundle_example() -> None:
    """The parser must read App.pkl Listing syntax used by every toolkit example."""
    example = (
        Path(__file__).resolve().parents[3]
        / "contract-toolkit"
        / "examples"
        / "bundle"
        / "app.pkl"
    )
    assert _declared_entrypoints(example.read_text(encoding="utf-8")) == (
        "crawler",
        "miner",
    )


def test_k004_mapping_syntax_bundle_still_resolves(tmp_path: Path) -> None:
    """Mapping-key bundles still resolve, even though App.pkl uses Listing form."""
    files = _bundle_files()
    files["contract/app.pkl"] = dedent("""\
        amends "@app-contract-toolkit/App.pkl"

        name = "demo"

        entrypoints {
          ["crawler"] { name = "crawler" }
        }
    """)
    assert [f for f in _scan(tmp_path, files) if f.rule_id == "K004"] == []


def test_k004_listing_bundle_fully_generated_no_finding(tmp_path: Path) -> None:
    """A Listing-syntax crawler+miner tree with both copies present is clean.

    Regression: ``_declared_entrypoints`` only read ``["key"]`` mapping keys, so
    App.pkl's ``new Entrypoint { name = "…" }`` form (every toolkit example)
    parsed as empty and K004 fired unsatisfiable ``<entrypoint>`` placeholders
    even when the files were on disk.
    """
    files = _bundle_files()
    files["contract/app.pkl"] = _TWO_ENTRYPOINT_APP_PKL
    files["app/generated/miner/manifest.json"] = "{}\n"
    files["app/generated/miner/_input.py"] = _BANNER + "x = 1\n"
    files["app/generated/miner/_e2e_base.py"] = _BANNER + "class BaseE2E:\n    pass\n"
    files["app/generated/miner/__init__.py"] = ""
    assert [f for f in _scan(tmp_path, files) if f.rule_id == "K004"] == []


def test_k004_partial_bundle_fires_for_ungenerated_entrypoint(tmp_path: Path) -> None:
    """One generated entrypoint does not satisfy K004 for the rest of the bundle.

    Regression: the unscoped ``any()`` fallback treated a same-named file under
    *any* subdirectory as enough, so adding an entrypoint and forgetting to
    regenerate produced zero findings.
    """
    files = _bundle_files()
    files["contract/app.pkl"] = _TWO_ENTRYPOINT_APP_PKL
    findings = [f for f in _scan(tmp_path, files) if f.rule_id == "K004"]
    messages = [f.message for f in findings]
    assert any("app/generated/miner/manifest.json" in m for m in messages)
    assert any("app/generated/miner/_input.py" in m for m in messages)
    assert not any("app/generated/crawler/" in m for m in messages)


def test_k004_empty_entrypoints_block_uses_top_level(tmp_path: Path) -> None:
    """``entrypoints { }`` is App.pkl's default Listing — single-entrypoint layout."""
    files = _clean_files()
    files["contract/app.pkl"] = dedent("""\
        amends "@app-contract-toolkit/App.pkl"

        name = "demo"

        entrypoints {}
    """)
    del files["app/generated/manifest.json"]
    findings = [f for f in _scan(tmp_path, files) if f.rule_id == "K004"]
    assert len(findings) == 1
    assert "'app/generated/manifest.json'" in findings[0].message


def test_k004_stray_subdirectory_does_not_satisfy_single_entrypoint(
    tmp_path: Path,
) -> None:
    """A non-bundle contract requires the top-level file, not a same-named stray.

    Regression: the one-level-down fallback was applied unconditionally, so a
    single-entrypoint app that lost ``app/generated/manifest.json`` still passed
    if any unrelated subdirectory held a copy.
    """
    files = _clean_files()
    del files["app/generated/manifest.json"]
    files["app/generated/backup_old/manifest.json"] = "{}\n"
    findings = [f for f in _scan(tmp_path, files) if f.rule_id == "K004"]
    assert len(findings) == 1
    assert "'app/generated/manifest.json'" in findings[0].message


def test_k004_bundle_still_requires_atlan_yaml(tmp_path: Path) -> None:
    """``atlan.yaml`` is emitted by a bundle root too, so it stays in scope."""
    files = _bundle_files()
    del files["atlan.yaml"]
    findings = [f for f in _scan(tmp_path, files) if f.rule_id == "K004"]
    assert len(findings) == 1
    assert "atlan.yaml" in findings[0].message


def test_k004_single_entrypoint_message_keeps_top_level_path(tmp_path: Path) -> None:
    """A non-bundle contract still names the single-entrypoint path in its remedy."""
    files = _clean_files()
    del files["app/generated/manifest.json"]
    findings = [f for f in _scan(tmp_path, files) if f.rule_id == "K004"]
    assert len(findings) == 1
    assert "'app/generated/manifest.json'" in findings[0].message


# ---------------------------------------------------------------------------
# K005 — stripped provenance banner
# ---------------------------------------------------------------------------


def test_k005_stripped_banner_on_yaml(tmp_path: Path) -> None:
    files = _clean_files()
    files["atlan.yaml"] = "name: demo\n"  # no banner
    findings = [f for f in _scan(tmp_path, files) if f.rule_id == "K005"]
    assert len(findings) == 1
    assert findings[0].file == "atlan.yaml"


def test_k005_banner_variant_accepted(tmp_path: Path) -> None:
    """The '… via contract-toolkit. DO NOT EDIT.' variant is a valid banner."""
    files = _clean_files()
    files["app/generated/_e2e_base.py"] = _BANNER_VARIANT + "y = 2\n"
    assert [f for f in _scan(tmp_path, files) if f.rule_id == "K005"] == []


def test_k005_empty_init_and_json_exempt(tmp_path: Path) -> None:
    """Empty __init__.py and .json outputs never carry a banner and are exempt."""
    files = _clean_files()
    # __init__.py is empty and manifest.json has no banner — neither is flagged.
    assert [f for f in _scan(tmp_path, files) if f.rule_id == "K005"] == []


def test_k005_generated_py_missing_banner(tmp_path: Path) -> None:
    files = _clean_files()
    files["app/generated/_input.py"] = "x = 1\n"  # banner stripped
    findings = [f for f in _scan(tmp_path, files) if f.rule_id == "K005"]
    assert len(findings) == 1
    assert findings[0].file == "app/generated/_input.py"


def test_k005_markers_on_separate_lines_not_a_banner(tmp_path: Path) -> None:
    """A hand-written preamble that mentions "generated" and "do not edit" on
    separate lines is not a valid banner — both markers must appear together
    on the same header line."""
    files = _clean_files()
    files["atlan.yaml"] = (
        "# This file is auto-generated for reference only.\n"
        "# Please do not edit unless explicitly asked to.\n"
        "name: demo\n"
    )
    findings = [f for f in _scan(tmp_path, files) if f.rule_id == "K005"]
    assert len(findings) == 1
    assert findings[0].file == "atlan.yaml"


def test_k005_suppressed(tmp_path: Path) -> None:
    files = _clean_files()
    files["atlan.yaml"] = "# conformance: ignore[K005] hand-maintained\nname: demo\n"
    findings = [f for f in _scan(tmp_path, files) if f.rule_id == "K005"]
    assert len(findings) == 1
    assert findings[0].suppressed
    assert findings[0].suppression_justification == "hand-maintained"


def test_k005_not_fired_without_contract(tmp_path: Path) -> None:
    """No contract/app.pkl -> no banner expectation even on app/generated files."""
    files = {"app/generated/_input.py": "x = 1\n"}
    assert [f for f in _scan(tmp_path, files) if f.rule_id == "K005"] == []


# ---------------------------------------------------------------------------
# K007 — toolkit version outdated
# ---------------------------------------------------------------------------


def test_k007_outdated_toolkit(tmp_path: Path) -> None:
    files = _clean_files()
    files["contract/PklProject"] = _pkl_project(version=_OLDER)
    files["contract/PklProject.deps.json"] = _deps_json(resolved=_OLDER)
    findings = [f for f in _scan(tmp_path, files) if f.rule_id == "K007"]
    assert len(findings) == 1
    assert findings[0].file == "contract/PklProject"
    assert not findings[0].suppressed


def test_k007_latest_no_finding(tmp_path: Path) -> None:
    assert [f for f in _scan(tmp_path, _clean_files()) if f.rule_id == "K007"] == []


def test_k007_ahead_no_finding(tmp_path: Path) -> None:
    """An app on a newer toolkit than the recorded baseline is not flagged."""
    files = _clean_files()
    files["contract/PklProject.deps.json"] = _deps_json(resolved="999.0.0")
    assert [f for f in _scan(tmp_path, files) if f.rule_id == "K007"] == []


def test_k007_no_lock_defers_to_k003(tmp_path: Path) -> None:
    """Without a resolved lock K007 stays quiet — the missing lock is K003's job."""
    files = _clean_files()
    files["contract/PklProject"] = _pkl_project(version=_OLDER)
    del files["contract/PklProject.deps.json"]
    assert [f for f in _scan(tmp_path, files) if f.rule_id == "K007"] == []


def test_k007_suppressed(tmp_path: Path) -> None:
    files = _clean_files()
    files["contract/PklProject.deps.json"] = _deps_json(resolved=_OLDER)
    files["contract/PklProject"] = _pkl_project(version=_OLDER).replace(
        "    uri =", "    // conformance: ignore[K007] pinned intentionally\n    uri ="
    )
    findings = [f for f in _scan(tmp_path, files) if f.rule_id == "K007"]
    assert len(findings) == 1
    assert findings[0].suppressed
    assert findings[0].suppression_justification == "pinned intentionally"


# ---------------------------------------------------------------------------
# K008 — non-canonical toolkit source
# ---------------------------------------------------------------------------


def test_k008_noncanonical_source(tmp_path: Path) -> None:
    files = _clean_files()
    files["contract/PklProject"] = _pkl_project(
        base="github.com/someone/fork/app-contract-toolkit"
    )
    findings = [f for f in _scan(tmp_path, files) if f.rule_id == "K008"]
    assert len(findings) == 1
    assert findings[0].file == "contract/PklProject"


def test_k008_noncanonical_suppresses_version_check(tmp_path: Path) -> None:
    """A non-canonical source reports K008 only — the version floor is moot."""
    files = _clean_files()
    files["contract/PklProject"] = _pkl_project(
        version=_OLDER, base="example.com/fork/app-contract-toolkit"
    )
    ids = _ids([f for f in _scan(tmp_path, files) if f.rule_id in ("K007", "K008")])
    assert ids == ["K008"]


def test_k008_canonical_no_finding(tmp_path: Path) -> None:
    assert [f for f in _scan(tmp_path, _clean_files()) if f.rule_id == "K008"] == []


def test_k008_suppressed(tmp_path: Path) -> None:
    files = _clean_files()
    files["contract/PklProject"] = _pkl_project(
        base="example.com/fork/app-contract-toolkit"
    ).replace("    uri =", "    // conformance: ignore[K008] vendored fork\n    uri =")
    findings = [f for f in _scan(tmp_path, files) if f.rule_id == "K008"]
    assert len(findings) == 1
    assert findings[0].suppressed
    assert findings[0].suppression_justification == "vendored fork"


# ---------------------------------------------------------------------------
# K009 — unresolved scaffold placeholder
# ---------------------------------------------------------------------------


def test_k009_placeholder_in_yaml(tmp_path: Path) -> None:
    files = _clean_files()
    files["atlan.yaml"] = _BANNER + "app_id: {app_name}\n"
    findings = [f for f in _scan(tmp_path, files) if f.rule_id == "K009"]
    assert len(findings) == 1
    assert findings[0].file == "atlan.yaml"


def test_k009_placeholder_in_json(tmp_path: Path) -> None:
    files = _clean_files()
    files["app/generated/manifest.json"] = '{"conn": "{connection_name}"}\n'
    findings = [f for f in _scan(tmp_path, files) if f.rule_id == "K009"]
    assert len(findings) == 1
    assert findings[0].file == "app/generated/manifest.json"


def test_k009_double_brace_runtime_token_excluded(tmp_path: Path) -> None:
    """{{...}} E2E runtime-substitution tokens are intentional — never flagged."""
    files = _clean_files()
    files["app/generated/manifest.json"] = (
        '{"cred": "{{credential}}", "name": "{{name}}"}\n'
    )
    assert [f for f in _scan(tmp_path, files) if f.rule_id == "K009"] == []


def test_k009_deployment_name_token_is_legitimate(tmp_path: Path) -> None:
    """{deployment_name} is a deploy-time token the current toolkit still emits
    verbatim into every manifest task_queue — never a K009 leftover. Modeled on a
    real stale-toolkit manifest: {app_name} IS flagged, {deployment_name} is not."""
    files = _clean_files()
    files["app/generated/manifest.json"] = (
        "{\n"
        '  "app_name": "{app_name}",\n'
        '  "task_queue": "atlan-glue-{deployment_name}",\n'
        '  "args": {"connection": "{{connection}}"}\n'
        "}\n"
    )
    findings = [f for f in _scan(tmp_path, files) if f.rule_id == "K009"]
    assert len(findings) == 1
    assert findings[0].line == 2  # only the {app_name} line


def test_k009_suppressed_in_yaml(tmp_path: Path) -> None:
    files = _clean_files()
    files["atlan.yaml"] = (
        _BANNER
        + "# conformance: ignore[K009] legacy placeholder, migration tracked\n"
        + "app_id: {app_name}\n"
    )
    findings = [f for f in _scan(tmp_path, files) if f.rule_id == "K009"]
    assert len(findings) == 1
    assert findings[0].suppressed
    assert (
        findings[0].suppression_justification == "legacy placeholder, migration tracked"
    )


def test_k009_clean_no_finding(tmp_path: Path) -> None:
    assert [f for f in _scan(tmp_path, _clean_files()) if f.rule_id == "K009"] == []


def test_k009_non_utf8_artifact_does_not_crash(tmp_path: Path) -> None:
    """A non-UTF-8 blob under app/generated/ is skipped, never crashes the run."""
    for rel, content in _clean_files().items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    (tmp_path / "app/generated/logo.bin").write_bytes(b"\xff\xfe\x00{app_name}")
    findings = scan_all(discover(tmp_path), tmp_path)
    assert [f for f in findings if f.rule_id == "K009"] == []


def test_k009_suppressed_after_earlier_hash(tmp_path: Path) -> None:
    """A directive after an earlier # (e.g. a URL fragment) still suppresses."""
    files = _clean_files()
    files["atlan.yaml"] = (
        _BANNER
        + 'app_id: "{app_name}#frag"  # conformance: ignore[K009] migration tracked\n'
    )
    findings = [f for f in _scan(tmp_path, files) if f.rule_id == "K009"]
    assert len(findings) == 1
    assert findings[0].suppressed
    assert findings[0].suppression_justification == "migration tracked"


def test_k009_placeholder_in_yaml_comment_is_silent(tmp_path: Path) -> None:
    """A {name} inside an explanatory YAML comment is prose, not a leftover.

    Modeled on atlan-powerbi-app atlan.yaml:17-20 — the comment documents the
    runtime URL template `GET /workflows/v1/manifest?entrypoint={name}` while
    the actual generated entrypoint names are concrete.
    """
    files = _clean_files()
    files["atlan.yaml"] = (
        _BANNER
        + "name: atlan-powerbi\n"
        + "# Generated entrypoints land under `contract/generated/` so the SDK's\n"
        + "# `GET /workflows/v1/manifest?entrypoint={name}` resolves correctly.\n"
        + "entrypoints:\n"
        + "  - crawl\n"
    )
    assert [f for f in _scan(tmp_path, files) if f.rule_id == "K009"] == []


def test_k009_placeholder_in_yaml_value_still_fires(tmp_path: Path) -> None:
    """The same URL template as a real field *value* (not a comment) still fires.

    The quoted scalar carries a # hash-route *before* the placeholder, so a
    naive "cut the line at the first #" narrowing would wrongly hide it —
    comment stripping must respect YAML quoting.
    """
    files = _clean_files()
    files["atlan.yaml"] = (
        _BANNER
        + "name: atlan-powerbi\n"
        + 'manifest_url: "https://tenant.atlan.com/#/workflows/v1/manifest?entrypoint={name}"\n'
    )
    findings = [f for f in _scan(tmp_path, files) if f.rule_id == "K009"]
    assert len(findings) == 1
    assert findings[0].file == "atlan.yaml"
    assert findings[0].line == 3


# ---------------------------------------------------------------------------
# K010 — missing E2E scaffolding
# ---------------------------------------------------------------------------


def test_k010_missing_e2e_base(tmp_path: Path) -> None:
    files = _clean_files()
    del files["app/generated/_e2e_base.py"]
    findings = [f for f in _scan(tmp_path, files) if f.rule_id == "K010"]
    assert len(findings) == 1
    assert findings[0].file == "contract/app.pkl"


def test_k010_present_no_finding(tmp_path: Path) -> None:
    assert [f for f in _scan(tmp_path, _clean_files()) if f.rule_id == "K010"] == []


def test_k010_multi_entrypoint_bundle_skipped(tmp_path: Path) -> None:
    """A bundle (entrypoints block) emits E2E scaffolding per-entrypoint — no K010."""
    files = _clean_files()
    del files["app/generated/_e2e_base.py"]
    files["contract/app.pkl"] = (
        'amends "@app-contract-toolkit/App.pkl"\n\n'
        'name = "demo"\n\n'
        "entrypoints {\n"
        "  new Entrypoint {\n"
        '    name = "crawler"\n'
        "  }\n"
        "}\n"
    )
    assert [f for f in _scan(tmp_path, files) if f.rule_id == "K010"] == []


def test_k010_suppressed(tmp_path: Path) -> None:
    files = _clean_files()
    del files["app/generated/_e2e_base.py"]
    files["contract/app.pkl"] = (
        "// conformance: ignore[K010] utility app ships no e2e\n" + _APP_PKL
    )
    findings = [f for f in _scan(tmp_path, files) if f.rule_id == "K010"]
    assert len(findings) == 1
    assert findings[0].suppressed


# ---------------------------------------------------------------------------
# Rule metadata
# ---------------------------------------------------------------------------


def test_rule_metadata_app_scoped_warn() -> None:
    for rid in ("K004", "K005", "K007", "K008", "K010"):
        rule = get_rule(rid)
        assert rule.scope == RuleScope.APP
        assert rule.tier == EnforcementTier.WARN
        assert rule.rationale, f"{rid} must have a non-empty rationale"


def test_k003_is_block_tier() -> None:
    """K003 left the WARN hygiene group in FND-311.

    A pin that disagrees with its lock is not a staleness proxy like its K004 /
    K005 neighbours — it means the committed artifacts were generated from a
    toolkit version the contract no longer claims, which is the route the
    K009/K011 customer-facing breakages travel to a tenant.
    """
    rule = get_rule("K003")
    assert rule.scope == RuleScope.APP
    assert rule.tier == EnforcementTier.BLOCK
    assert rule.rationale, "K003 must have a non-empty rationale"


def test_k009_is_block_tier() -> None:
    """K009 is the one BLOCK-tier K-rule: an unresolved {app_name} ships a wrong
    artifact and is never a false positive, so it hard-fails the gate rather than
    landing as WARN."""
    rule = get_rule("K009")
    assert rule.scope == RuleScope.APP
    assert rule.tier == EnforcementTier.BLOCK
    assert rule.rationale
