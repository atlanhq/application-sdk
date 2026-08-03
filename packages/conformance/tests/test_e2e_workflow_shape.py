"""Meta-tests for the T020-T022 e2e-workflow-shape checks.

T020 catches a bespoke full-DAG workflow calling the SDK's ``sdr-e2e`` action
directly instead of delegating to ``tests-reusable.yaml`` (the shape the SDR
fleet sweep hand-rolled across ~8 connectors).  T021 catches e2e suites no
caller can run.  T022 catches an SDR app whose caller omits the ADR-0014
``two-store: true`` posture, which is what makes a missing ``App.upload()``
bridge visible instead of masked.

Tests cover each fire path, the canonical ``atlan-mysql-app`` caller as the
clean case, the documented safe shapes, discovery gating, and inline
suppression.
"""

from __future__ import annotations

from pathlib import Path

from conformance.suite.checks.e2e_workflow_shape import discover, scan_all
from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import EnforcementTier, RuleScope

# The canonical thin caller (atlan-mysql-app/.github/workflows/tests.yaml).
_CANONICAL_CALLER = """\
name: Tests

on:
  pull_request:
    types: [opened, synchronize, reopened, labeled]

jobs:
  tests:
    uses: atlanhq/application-sdk/.github/workflows/tests-reusable.yaml@main
    with:
      app-name: "mysql"
      app-image-name: "atlan-mysql-app"
      application-sdk-ref: ${{ inputs.application_sdk_ref }}
      two-store: true
    secrets: inherit
"""

# The hand-rolled shape the fleet sweep produced.
_BESPOKE_WORKFLOW = """\
name: SDR full-DAG e2e (hive)

on:
  pull_request:
    types: [opened, synchronize, reopened, labeled]
  workflow_dispatch:

jobs:
  sdr-full-dag:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: atlanhq/application-sdk/.github/actions/sdr-e2e@main
        with:
          app-name: hive
          test-path: tests/e2e/test_hive_full_dag.py
          config-dir: .github/e2e
"""

_E2E_SUITE = """\
class TestThingFullDAG:
    pass
"""


def _repo(
    tmp_path: Path,
    *,
    workflows: dict[str, str] | None = None,
    e2e_suites: dict[str, str] | None = None,
    atlan_yaml: str | None = None,
) -> Path:
    root = tmp_path
    if workflows:
        wf_dir = root / ".github" / "workflows"
        wf_dir.mkdir(parents=True, exist_ok=True)
        for name, text in workflows.items():
            (wf_dir / name).write_text(text, encoding="utf-8")
    if e2e_suites:
        e2e_dir = root / "tests" / "e2e"
        e2e_dir.mkdir(parents=True, exist_ok=True)
        for name, text in e2e_suites.items():
            (e2e_dir / name).write_text(text, encoding="utf-8")
    if atlan_yaml is not None:
        (root / "atlan.yaml").write_text(atlan_yaml, encoding="utf-8")
    return root


def _scan(root: Path) -> list:
    return scan_all(discover(root), root)


def _ids(findings: list) -> list[str]:
    return [f.rule_id for f in findings if not f.suppressed]


# ---------------------------------------------------------------------------
# Rule metadata
# ---------------------------------------------------------------------------


def test_t020_rule_metadata() -> None:
    rule = get_rule("T020")
    assert rule.name == "BespokeFullDagE2EWorkflow"
    assert rule.tier == EnforcementTier.WARN
    assert rule.scope == RuleScope.APP
    assert rule.autofixable is False
    assert rule.since == "0.18.0"
    assert rule.category == "e2e-ci"
    assert rule.rationale.strip()


def test_t021_rule_metadata() -> None:
    rule = get_rule("T021")
    assert rule.name == "E2ESuiteUnreachableInCI"
    assert rule.tier == EnforcementTier.WARN
    assert rule.scope == RuleScope.APP
    assert rule.rationale.strip()


def test_t022_rule_metadata() -> None:
    rule = get_rule("T022")
    assert rule.name == "E2ETwoStorePostureDisabled"
    assert rule.tier == EnforcementTier.WARN
    assert rule.scope == RuleScope.APP
    assert rule.rationale.strip()


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_discover_only_workflow_yaml(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        workflows={"tests.yaml": _CANONICAL_CALLER, "notes.md": "not a workflow"},
    )
    (root / ".github" / "workflows" / "other.yml").write_text(
        "name: x\n", encoding="utf-8"
    )
    names = {p.name for p in discover(root)}
    assert names == {"tests.yaml", "other.yml"}


def test_discover_empty_without_workflow_dir(tmp_path: Path) -> None:
    assert discover(tmp_path) == []


# ---------------------------------------------------------------------------
# T020 — bespoke full-DAG workflow
# ---------------------------------------------------------------------------


def test_t020_fires_on_bespoke_sdr_e2e_workflow(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        workflows={
            "tests.yaml": _CANONICAL_CALLER,
            "sdr-full-dag.yaml": _BESPOKE_WORKFLOW,
        },
        e2e_suites={"test_thing_full_dag.py": _E2E_SUITE},
    )
    findings = [f for f in _scan(root) if f.rule_id == "T020"]
    assert len(findings) == 1
    assert findings[0].file == ".github/workflows/sdr-full-dag.yaml"
    assert "tests-reusable.yaml" in findings[0].message


def test_t020_clean_on_canonical_caller_only(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        workflows={"tests.yaml": _CANONICAL_CALLER},
        e2e_suites={"test_thing_full_dag.py": _E2E_SUITE},
    )
    assert "T020" not in _ids(_scan(root))


def test_t020_ignores_sdr_e2e_inside_the_caller_itself(tmp_path: Path) -> None:
    """A file that both calls the reusable and mentions the action is a caller."""
    mixed = (
        _CANONICAL_CALLER
        + """\
      - uses: atlanhq/application-sdk/.github/actions/sdr-e2e@main
"""
    )
    root = _repo(
        tmp_path,
        workflows={"tests.yaml": mixed},
        e2e_suites={"test_thing_full_dag.py": _E2E_SUITE},
    )
    assert "T020" not in _ids(_scan(root))


def test_t020_suppressed_inline(tmp_path: Path) -> None:
    suppressed = _BESPOKE_WORKFLOW.replace(
        "      - uses: atlanhq/application-sdk/.github/actions/sdr-e2e@main",
        "      # conformance: ignore[T020] native ODBC deps: reusable cannot "
        "pre-install them\n"
        "      - uses: atlanhq/application-sdk/.github/actions/sdr-e2e@main",
    )
    root = _repo(
        tmp_path,
        workflows={"tests.yaml": _CANONICAL_CALLER, "sdr-full-dag.yaml": suppressed},
        e2e_suites={"test_thing_full_dag.py": _E2E_SUITE},
    )
    t020 = [f for f in _scan(root) if f.rule_id == "T020"]
    assert len(t020) == 1
    assert t020[0].suppressed is True


# ---------------------------------------------------------------------------
# T021 — e2e suites unreachable in CI
# ---------------------------------------------------------------------------


def test_t021_fires_when_no_caller_exists(tmp_path: Path) -> None:
    unrelated = "name: Lint\njobs:\n  lint:\n    runs-on: ubuntu-latest\n"
    root = _repo(
        tmp_path,
        workflows={"checks.yaml": unrelated},
        e2e_suites={"test_thing_full_dag.py": _E2E_SUITE},
    )
    findings = [f for f in _scan(root) if f.rule_id == "T021"]
    assert len(findings) == 1
    assert "no workflow calls tests-reusable.yaml" in findings[0].message


def test_t021_silent_when_a_bespoke_workflow_runs_the_suite(tmp_path: Path) -> None:
    """Reachable-but-wrong is T020's finding, not an unreachable suite."""
    root = _repo(
        tmp_path,
        workflows={"sdr-full-dag.yaml": _BESPOKE_WORKFLOW},
        e2e_suites={"test_hive_full_dag.py": _E2E_SUITE},
    )
    ids = _ids(_scan(root))
    assert "T020" in ids
    assert "T021" not in ids


def test_t021_silent_when_a_test_paths_input_covers_the_tier(tmp_path: Path) -> None:
    """`test-paths: "tests/unit tests/integration tests/e2e"` runs the suites."""
    caller = """\
jobs:
  unit-integration:
    runs-on: ubuntu-latest
    steps:
      - uses: atlanhq/application-sdk/.github/actions/connector-integration-tests@main
        with:
          app-name: powerbi
          test-paths: "tests/unit tests/integration tests/e2e"
"""
    root = _repo(
        tmp_path,
        workflows={"tests.yaml": caller},
        e2e_suites={"test_thing_full_dag.py": _E2E_SUITE},
    )
    assert "T021" not in _ids(_scan(root))


def test_t021_fires_on_enable_e2e_false(tmp_path: Path) -> None:
    caller = _CANONICAL_CALLER.replace(
        '      app-image-name: "atlan-mysql-app"',
        '      app-image-name: "atlan-mysql-app"\n      enable-e2e: false',
    )
    root = _repo(
        tmp_path,
        workflows={"tests.yaml": caller},
        e2e_suites={"test_thing_full_dag.py": _E2E_SUITE},
    )
    findings = [f for f in _scan(root) if f.rule_id == "T021"]
    assert len(findings) == 1
    assert "enable-e2e: false" in findings[0].message


def test_t021_fires_on_empty_app_image_name(tmp_path: Path) -> None:
    caller = _CANONICAL_CALLER.replace(
        '      app-image-name: "atlan-mysql-app"', '      app-image-name: ""'
    )
    root = _repo(
        tmp_path,
        workflows={"tests.yaml": caller},
        e2e_suites={"test_thing_full_dag.py": _E2E_SUITE},
    )
    findings = [f for f in _scan(root) if f.rule_id == "T021"]
    assert len(findings) == 1
    assert "app-image-name" in findings[0].message


def test_t021_silent_on_the_legacy_marketplace_e2e_reusable(tmp_path: Path) -> None:
    """Pre-v3 connectors run their suites through marketplace-releases."""
    legacy = """\
jobs:
  e2e:
    uses: atlanhq/marketplace-releases/.github/workflows/e2e-app-test.yaml@main
    secrets: inherit
"""
    root = _repo(
        tmp_path,
        workflows={"e2e-tests-feature.yaml": legacy},
        e2e_suites={"test_thing_full_dag.py": _E2E_SUITE},
    )
    assert "T021" not in _ids(_scan(root))


def test_t021_clean_on_canonical_caller(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        workflows={"tests.yaml": _CANONICAL_CALLER},
        e2e_suites={"test_thing_full_dag.py": _E2E_SUITE},
    )
    assert "T021" not in _ids(_scan(root))


def test_t021_silent_without_e2e_suites(tmp_path: Path) -> None:
    """No suites to run means nothing to be unreachable."""
    root = _repo(tmp_path, workflows={"sdr-full-dag.yaml": _BESPOKE_WORKFLOW})
    assert "T021" not in _ids(_scan(root))


def test_t021_ignores_uncollectable_e2e_files(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        workflows={},
        e2e_suites={"__init__.py": "", "helpers.py": "X = 1"},
    )
    assert "T021" not in _ids(_scan(root))


def test_t021_anchors_on_the_misconfigured_caller(tmp_path: Path) -> None:
    caller = _CANONICAL_CALLER.replace(
        '      app-image-name: "atlan-mysql-app"', '      app-image-name: ""'
    )
    root = _repo(
        tmp_path,
        workflows={"tests.yaml": caller},
        e2e_suites={"test_thing_full_dag.py": _E2E_SUITE},
    )
    finding = next(f for f in _scan(root) if f.rule_id == "T021")
    assert finding.file == ".github/workflows/tests.yaml"
    assert caller.splitlines()[finding.line - 1].strip().startswith("uses:")


def test_t021_anchors_on_the_suite_when_no_workflows_exist(tmp_path: Path) -> None:
    root = _repo(tmp_path, e2e_suites={"test_thing_full_dag.py": _E2E_SUITE})
    finding = next(f for f in _scan(root) if f.rule_id == "T021")
    assert finding.file == "tests/e2e/test_thing_full_dag.py"


# ---------------------------------------------------------------------------
# T022 — two-store posture
# ---------------------------------------------------------------------------

_SDR_ATLAN_YAML = "name: mysql\nself_deployed_runtime: true\n"
_NON_SDR_ATLAN_YAML = "name: mysql\nself_deployed_runtime: false\n"


def test_t022_fires_when_sdr_app_omits_two_store(tmp_path: Path) -> None:
    caller = _CANONICAL_CALLER.replace("      two-store: true\n", "")
    root = _repo(
        tmp_path,
        workflows={"tests.yaml": caller},
        e2e_suites={"test_thing_full_dag.py": _E2E_SUITE},
        atlan_yaml=_SDR_ATLAN_YAML,
    )
    findings = [f for f in _scan(root) if f.rule_id == "T022"]
    assert len(findings) == 1
    assert "two-store: true" in findings[0].message


def test_t022_clean_when_two_store_set(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        workflows={"tests.yaml": _CANONICAL_CALLER},
        e2e_suites={"test_thing_full_dag.py": _E2E_SUITE},
        atlan_yaml=_SDR_ATLAN_YAML,
    )
    assert "T022" not in _ids(_scan(root))


def test_t022_silent_for_non_sdr_app(tmp_path: Path) -> None:
    caller = _CANONICAL_CALLER.replace("      two-store: true\n", "")
    root = _repo(
        tmp_path,
        workflows={"tests.yaml": caller},
        e2e_suites={"test_thing_full_dag.py": _E2E_SUITE},
        atlan_yaml=_NON_SDR_ATLAN_YAML,
    )
    assert "T022" not in _ids(_scan(root))


def test_t022_silent_without_atlan_yaml(tmp_path: Path) -> None:
    caller = _CANONICAL_CALLER.replace("      two-store: true\n", "")
    root = _repo(
        tmp_path,
        workflows={"tests.yaml": caller},
        e2e_suites={"test_thing_full_dag.py": _E2E_SUITE},
    )
    assert "T022" not in _ids(_scan(root))


def test_t022_suppressed_inline(tmp_path: Path) -> None:
    caller = _CANONICAL_CALLER.replace("      two-store: true\n", "").replace(
        "    uses: atlanhq/application-sdk/.github/workflows/tests-reusable.yaml@main",
        "    # conformance: ignore[T022] extract produces no artifacts to bridge\n"
        "    uses: atlanhq/application-sdk/.github/workflows/tests-reusable.yaml@main",
    )
    root = _repo(
        tmp_path,
        workflows={"tests.yaml": caller},
        e2e_suites={"test_thing_full_dag.py": _E2E_SUITE},
        atlan_yaml=_SDR_ATLAN_YAML,
    )
    t022 = [f for f in _scan(root) if f.rule_id == "T022"]
    assert len(t022) == 1
    assert t022[0].suppressed is True


# ---------------------------------------------------------------------------
# `with:` parsing
# ---------------------------------------------------------------------------


def test_with_block_parsing_ignores_later_jobs(tmp_path: Path) -> None:
    """A second job's inputs must not leak into the caller's `with:` mapping."""
    caller = """\
jobs:
  tests:
    uses: atlanhq/application-sdk/.github/workflows/tests-reusable.yaml@main
    with:
      app-name: "mysql"
      app-image-name: "atlan-mysql-app"
    secrets: inherit
  other:
    uses: ./.github/workflows/thing.yaml
    with:
      two-store: true
"""
    root = _repo(
        tmp_path,
        workflows={"tests.yaml": caller},
        e2e_suites={"test_thing_full_dag.py": _E2E_SUITE},
        atlan_yaml=_SDR_ATLAN_YAML,
    )
    # two-store belongs to the *other* job, so T022 must still fire.
    assert "T022" in _ids(_scan(root))
    assert "T021" not in _ids(_scan(root))


def test_commented_two_store_does_not_count(tmp_path: Path) -> None:
    caller = _CANONICAL_CALLER.replace(
        "      two-store: true\n", "      # two-store: true\n"
    )
    root = _repo(
        tmp_path,
        workflows={"tests.yaml": caller},
        e2e_suites={"test_thing_full_dag.py": _E2E_SUITE},
        atlan_yaml=_SDR_ATLAN_YAML,
    )
    assert "T022" in _ids(_scan(root))


# ---------------------------------------------------------------------------
# Regression: evidence must come from the scope being graded
# (each case below returned the wrong verdict before the review fixes)
# ---------------------------------------------------------------------------


def test_scan_survives_non_utf8_workflow(tmp_path: Path) -> None:
    """A non-UTF-8 byte must not abort the whole multi-series run.

    ``UnicodeDecodeError`` is a ``ValueError``, not an ``OSError``, so an
    ``OSError``-only guard let it escape ``scan_all`` — and the runner wraps
    neither ``discover()`` nor ``scan_all()``.
    """
    root = _repo(
        tmp_path,
        workflows={"tests.yaml": _CANONICAL_CALLER},
        e2e_suites={"test_thing_full_dag.py": _E2E_SUITE},
    )
    (root / ".github" / "workflows" / "broken.yaml").write_bytes(
        b"\xff\xfename: broken\n"
    )
    assert _scan(root) == [] or isinstance(_scan(root), list)


def test_t021_not_silenced_by_a_comment_mentioning_tests_e2e(tmp_path: Path) -> None:
    """A stray comment must not disable the rule for the whole repo."""
    root = _repo(
        tmp_path,
        workflows={
            "lint.yaml": (
                "name: Lint\non:\n  pull_request:\njobs:\n"
                "  lint:\n    runs-on: ubuntu-latest\n    steps:\n"
                "      # TODO: we should eventually wire up tests/e2e in CI\n"
                "      - run: ruff check .\n"
            )
        },
        e2e_suites={"test_thing_full_dag.py": _E2E_SUITE},
    )
    assert "T021" in _ids(_scan(root))


def test_t021_not_silenced_by_a_trigger_paths_filter(tmp_path: Path) -> None:
    """`on: pull_request: paths: ['tests/e2e/**']` selects *when* a workflow
    runs, never what it executes."""
    root = _repo(
        tmp_path,
        workflows={
            "lint.yaml": (
                "name: Lint\non:\n  pull_request:\n    paths:\n"
                "      - 'tests/e2e/**'\njobs:\n"
                "  lint:\n    runs-on: ubuntu-latest\n    steps:\n"
                "      - run: ruff check .\n"
            )
        },
        e2e_suites={"test_thing_full_dag.py": _E2E_SUITE},
    )
    assert "T021" in _ids(_scan(root))


def test_t021_not_silenced_by_an_artifact_path(tmp_path: Path) -> None:
    """`tests/e2e-results/...` must not count — a bare \\b accepts the hyphen."""
    root = _repo(
        tmp_path,
        workflows={
            "lint.yaml": (
                "name: Lint\non:\n  pull_request:\njobs:\n"
                "  lint:\n    runs-on: ubuntu-latest\n    steps:\n"
                "      - run: cat tests/e2e-results/artifact.json\n"
            )
        },
        e2e_suites={"test_thing_full_dag.py": _E2E_SUITE},
    )
    assert "T021" in _ids(_scan(root))


def test_t021_silent_when_a_run_step_actually_invokes_the_suites(
    tmp_path: Path,
) -> None:
    """A `run:` body that really executes them is reachable (T020's business)."""
    root = _repo(
        tmp_path,
        workflows={
            "bespoke.yaml": (
                "name: Bespoke\non:\n  pull_request:\njobs:\n"
                "  e2e:\n    runs-on: ubuntu-latest\n    steps:\n"
                "      - run: |\n"
                "          uv run pytest tests/e2e\n"
            )
        },
        e2e_suites={"test_thing_full_dag.py": _E2E_SUITE},
    )
    assert "T021" not in _ids(_scan(root))


def test_t021_fires_when_enable_e2e_is_yaml_no(tmp_path: Path) -> None:
    """`enable-e2e: no` is YAML 1.1 false — a literal "false" test misses it."""
    caller = _CANONICAL_CALLER.replace(
        '      app-name: "mysql"', '      app-name: "mysql"\n      enable-e2e: no'
    )
    root = _repo(
        tmp_path,
        workflows={"tests.yaml": caller},
        e2e_suites={"test_thing_full_dag.py": _E2E_SUITE},
    )
    assert "T021" in _ids(_scan(root))


def test_t022_silent_when_two_store_is_yaml_yes(tmp_path: Path) -> None:
    """`two-store: yes` is YAML 1.1 true — flagging it is a false positive."""
    caller = _CANONICAL_CALLER.replace("two-store: true", "two-store: yes")
    root = _repo(
        tmp_path,
        workflows={"tests.yaml": caller},
        e2e_suites={"test_thing_full_dag.py": _E2E_SUITE},
        atlan_yaml="self_deployed_runtime: true\n",
    )
    assert "T022" not in _ids(_scan(root))


def test_t020_fires_on_a_legacy_job_beside_a_correct_caller(tmp_path: Path) -> None:
    """A repo mid-migration — caller added, legacy `sdr:` job not yet removed —
    is the likeliest real state, and the case the rule's own message promises to
    handle. Deciding per file exempted the whole workflow."""
    mixed = (
        _CANONICAL_CALLER
        + """\
  legacy-sdr-full-dag:
    runs-on: ubuntu-latest
    steps:
      - uses: atlanhq/application-sdk/.github/actions/sdr-e2e@main
        with:
          app-name: hive
          test-path: tests/e2e/test_hive_full_dag.py
"""
    )
    root = _repo(
        tmp_path,
        workflows={"tests.yaml": mixed},
        e2e_suites={"test_thing_full_dag.py": _E2E_SUITE},
    )
    t020 = [f for f in _scan(root) if f.rule_id == "T020"]
    assert len(t020) == 1
    assert t020[0].file == ".github/workflows/tests.yaml"


def test_t021_suppressed_inline(tmp_path: Path) -> None:
    """T021 honours the documented directive, like T020/T022/T023/T024."""
    caller = _CANONICAL_CALLER.replace(
        "    uses: atlanhq",
        "    # conformance: ignore[T021] native ODBC deps: e2e runs out-of-band\n"
        "    uses: atlanhq",
    ).replace('      app-image-name: "atlan-mysql-app"', '      app-image-name: ""')
    root = _repo(
        tmp_path,
        workflows={"tests.yaml": caller},
        e2e_suites={"test_thing_full_dag.py": _E2E_SUITE},
    )
    t021 = [f for f in _scan(root) if f.rule_id == "T021"]
    assert len(t021) == 1
    assert t021[0].suppressed is True


def test_t021_silent_on_env_indirection_to_a_run_step(tmp_path: Path) -> None:
    """`env: SUITE_PATH: tests/e2e/...` consumed by a later `run:` does run them.

    A key-name allow-list on top of the skip-block gating was under-inclusive
    and reported a suite that genuinely executes as unreachable.
    """
    root = _repo(
        tmp_path,
        workflows={
            "e2e.yaml": (
                "name: E2E\non:\n  pull_request:\njobs:\n"
                "  e2e:\n    runs-on: ubuntu-latest\n"
                "    env:\n      SUITE_PATH: tests/e2e/test_full.py\n"
                '    steps:\n      - run: pytest "$SUITE_PATH"\n'
            )
        },
        e2e_suites={"test_thing_full_dag.py": _E2E_SUITE},
    )
    assert "T021" not in _ids(_scan(root))


def test_t021_silent_on_underscore_test_path_input(tmp_path: Path) -> None:
    """A composite action taking `test_path:` (underscore) also runs the suites."""
    root = _repo(
        tmp_path,
        workflows={
            "e2e.yaml": (
                "name: E2E\non:\n  pull_request:\njobs:\n"
                "  e2e:\n    runs-on: ubuntu-latest\n    steps:\n"
                "      - uses: ./.github/actions/custom-e2e-runner\n"
                "        with:\n          test_path: tests/e2e/test_full.py\n"
            )
        },
        e2e_suites={"test_thing_full_dag.py": _E2E_SUITE},
    )
    assert "T021" not in _ids(_scan(root))


def test_t021_fires_when_only_a_step_name_mentions_the_suite(tmp_path: Path) -> None:
    """A step `name:` describes; it never runs anything."""
    root = _repo(
        tmp_path,
        workflows={
            "ci.yaml": (
                "name: CI\non:\n  pull_request:\njobs:\n"
                "  unit:\n    runs-on: ubuntu-latest\n    steps:\n"
                "      - name: Run tests/e2e/test_full.py\n"
                "        run: pytest tests/unit\n"
            )
        },
        e2e_suites={"test_thing_full_dag.py": _E2E_SUITE},
    )
    assert "T021" in _ids(_scan(root))


def test_t021_fires_when_only_an_artifact_path_mentions_the_suite(
    tmp_path: Path,
) -> None:
    """An upload-artifact target must never mark the suites reachable.

    This is the case the checker's own docstring names as the thing that must
    not silence T021 — one artifact path would otherwise disarm it repo-wide.
    """
    root = _repo(
        tmp_path,
        workflows={
            "ci.yaml": (
                "name: CI\non:\n  pull_request:\njobs:\n"
                "  unit:\n    runs-on: ubuntu-latest\n    steps:\n"
                "      - run: pytest tests/unit\n"
                "      - uses: actions/upload-artifact@v4\n"
                "        with:\n"
                "          name: logs\n"
                "          path: tests/e2e/test_full.py.log\n"
            )
        },
        e2e_suites={"test_thing_full_dag.py": _E2E_SUITE},
    )
    assert "T021" in _ids(_scan(root))


def test_t021_fires_when_only_run_name_mentions_the_suite(tmp_path: Path) -> None:
    """`run-name:` is GitHub's cosmetic display field; it executes nothing."""
    root = _repo(
        tmp_path,
        workflows={
            "ci.yaml": (
                "name: CI\n"
                "run-name: Testing tests/e2e/test_full.py on ${{ github.ref }}\n"
                "on:\n  pull_request:\njobs:\n"
                "  unit:\n    runs-on: ubuntu-latest\n    steps:\n"
                "      - run: pytest tests/unit\n"
            )
        },
        e2e_suites={"test_thing_full_dag.py": _E2E_SUITE},
    )
    assert "T021" in _ids(_scan(root))


def test_t021_fires_when_only_a_cache_path_mentions_the_suite(
    tmp_path: Path,
) -> None:
    """`actions/cache` names a location it reads and writes, never a command."""
    root = _repo(
        tmp_path,
        workflows={
            "ci.yaml": (
                "name: CI\non:\n  pull_request:\njobs:\n"
                "  unit:\n    runs-on: ubuntu-latest\n    steps:\n"
                "      - uses: actions/cache@v4\n"
                "        with:\n"
                "          path: tests/e2e/.cache\n"
                "          key: e2e-cache\n"
                "      - run: pytest tests/unit\n"
            )
        },
        e2e_suites={"test_thing_full_dag.py": _E2E_SUITE},
    )
    assert "T021" in _ids(_scan(root))
