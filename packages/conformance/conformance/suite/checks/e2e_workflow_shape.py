"""T020–T022 — the full-DAG e2e must run through the reusable Tests workflow.

The canonical wiring for a connector's full-DAG e2e is a *thin caller*: the
bootstrapped ``.github/workflows/tests.yaml`` invokes
``atlanhq/application-sdk/.github/workflows/tests-reusable.yaml`` and passes a
handful of inputs.  Everything else — the ``e2e`` PR-label gate, the
``discover-e2e-suites`` matrix (one parallel leg per ``tests/e2e/test_*.py``),
the per-leg ``ATLAN_DEPLOYMENT_NAME`` derivation that keeps worker and harness
on one Temporal queue, the GHCR image build, the ``sdr-e2e`` composite
invocation with the full-DAG ``config-dir`` / ``secrets-script`` /
``components-dir`` / ``compose-overlay`` set, the two-store posture, the
``Tests Gate`` aggregator, and the cross-repo SDK-dispatch plumbing — lives
inside the reusable and is maintained once for the whole fleet.
``atlan-mysql-app`` is the reference caller.

The SDR fleet sweep instead hand-rolled a per-repo
``.github/workflows/sdr-full-dag.yaml`` in ~8 connectors, each calling the
``sdr-e2e`` action directly.  Every one of those copies re-implements the
reusable's job scaffolding from memory, and each is a private fork of a
contract the SDK keeps evolving: they pin a single hard-coded ``test-path``
(so a second e2e suite is silently never run), they carry no matrix and
therefore no per-leg queue isolation, they miss ``two-store``, and they do not
participate in the ``Tests Gate`` required check.  Worse, they drift silently —
when the reusable gains an input or the action changes a default, the caller
picks it up and the copies do not.

Three rules, in remediation order:

* ``T020`` — BespokeFullDagE2EWorkflow: a workflow file invokes the SDK's
  ``sdr-e2e`` composite action directly instead of delegating to
  ``tests-reusable.yaml``.  A standalone ``sdr-full-dag.yaml`` is deleted and
  the caller wired; a legacy ``sdr:`` job that grew inside ``tests.yaml`` is
  replaced in place (the message says which, keyed on the filename — the wrong
  verb here is the difference between a two-line fix and a deleted CI entry
  point).

* ``T021`` — E2ESuiteUnreachableInCI: the repo ships collectable
  ``tests/e2e/test_*.py`` suites that **nothing** in ``.github/workflows/`` can
  run.  A suite counts as reachable through any of: a working
  ``tests-reusable.yaml`` caller (``enable-e2e`` not false, ``app-image-name``
  non-empty — an empty one disables the connector image build the e2e worker
  container is started from); a workflow naming a ``tests/e2e`` path directly;
  or a ``uses:`` of another reusable workflow whose filename names e2e (the
  legacy ``marketplace-releases/.github/workflows/e2e-app-test.yaml`` path).
  Those last two arms are deliberate: a bespoke runner is the *wrong* mechanism,
  which is T020's finding, but it is not an unreachable suite, and reporting
  both would say the same thing twice.  T021 is the narrower, complementary
  claim — these tests read as coverage in review and never execute at all.  Its
  main job is to catch the regression from "wrong runner" to "no runner" when
  someone remediates T020 by deleting the bespoke workflow and forgets to wire
  the caller.

* ``T022`` — E2ETwoStorePostureDisabled: an SDR app's caller does not set
  ``two-store: true``.  Without the ADR-0014 two-store posture the e2e worker's
  ``objectstore`` and ``atlan-objectstore`` bindings resolve to the same
  bucket, so a connector that never bridges its transformed artifacts across
  the boundary (``App.upload()``) still greens — the exact silent-zero-assets
  class ``P030`` polices statically.  With ``two-store: true`` the missing
  bridge shows up as zero downstream assets and lineage in Atlas.

Discovery
---------
Every ``*.yml``/``*.yaml`` under ``.github/workflows/``.  The rules also need
repo-level context (does ``tests/e2e/`` hold collectable suites? does
``atlan.yaml`` declare ``self_deployed_runtime: true``?), so the series runs
through ``scan_all`` rather than ``scan_path``.

Known exemption
---------------
Connectors that need OS-level native build dependencies before ``uv sync``
(ODBC drivers, SAP JCo, Kerberos headers) cannot use the reusable — the
reusable's own header documents this — and legitimately keep a bespoke
``tests.yaml``.  All three rules are WARN-tier and honour the standard inline
directive, so those repos annotate once:

    # conformance: ignore[T020] native ODBC build deps: reusable can't pre-install them

Inline suppression
------------------
``# conformance: ignore[T0NN] <reason>`` on the flagged line or the
comment-only line directly above it.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from conformance.suite.checks._ast_common import (
    is_collectable_test_file,
    make_cli_main,
    make_toml_finding,
    parse_toml_suppressions,
)
from conformance.suite.schema.findings import Finding

SERIES = "T"
RULE_T020 = "T020"
RULE_T021 = "T021"
RULE_T022 = "T022"

# The SDK composite action that actually runs a full-DAG e2e leg. The reusable
# workflow invokes it; a connector workflow never should.
_SDR_E2E_ACTION = "atlanhq/application-sdk/.github/actions/sdr-e2e"
# The reusable workflow a connector's tests.yaml is supposed to call.
_TESTS_REUSABLE = "atlanhq/application-sdk/.github/workflows/tests-reusable.yaml"

_USES_SDR_E2E_RE = re.compile(
    rf"^\s*(?:-\s*)?uses:\s*['\"]?{re.escape(_SDR_E2E_ACTION)}@"
)
_USES_REUSABLE_RE = re.compile(rf"^\s*uses:\s*['\"]?{re.escape(_TESTS_REUSABLE)}@")

# Any workflow naming a tests/e2e path — a bespoke pytest step, a
# `test-paths: "tests/unit tests/integration tests/e2e"` input, an sdr-e2e
# `test-path:`. The mechanism is wrong (T020's finding) but the suite does run,
# so T021 (which claims nothing runs it) must stay silent.
_E2E_PATH_RE = re.compile(r"tests/e2e\b")

# A `uses:` of some *other* reusable workflow whose filename names e2e — the
# legacy marketplace-releases path (`.../workflows/e2e-app-test.yaml@main`) that
# several pre-v3 connectors still run their suites through. Also reachable, also
# not this rule's business. `tests-reusable.yaml` does not match (no "e2e" in the
# filename) and is handled by the caller arm above; the sdr-e2e *action* has no
# `.yaml` suffix and correctly does not match either.
_USES_E2E_REUSABLE_RE = re.compile(r"^\s*uses:\s*['\"]?\S*e2e[^\s'\"]*\.ya?ml@")

_SDR_FLAG_RE = re.compile(
    r"^self_deployed_runtime:\s*(true|false)\b",
    re.MULTILINE | re.IGNORECASE,
)

# A `key: value` entry inside the caller's `with:` block.
_WITH_ENTRY_RE = re.compile(
    r"^(?P<indent>\s*)(?P<key>[A-Za-z0-9_-]+)\s*:\s*(?P<val>.*?)\s*$"
)

__all__ = ["SERIES", "discover", "main", "scan_all", "scan_path"]


# ---------------------------------------------------------------------------
# Repo-level context
# ---------------------------------------------------------------------------


def discover(root: Path) -> list[Path]:
    """Every workflow YAML under ``.github/workflows/``."""
    base = root / ".github" / "workflows"
    if not base.is_dir():
        return []
    return sorted(
        p for p in base.iterdir() if p.is_file() and p.suffix in {".yml", ".yaml"}
    )


def _has_e2e_suites(root: Path) -> bool:
    """True when ``tests/e2e/`` holds at least one pytest-collectable file."""
    base = root / "tests" / "e2e"
    if not base.is_dir():
        return False
    return any(
        p.is_file() and is_collectable_test_file(p.name)
        for p in base.rglob("*.py")
        if "__pycache__" not in p.parts
    )


def _is_sdr_app(root: Path) -> bool:
    """True when the generated ``atlan.yaml`` declares self-deployed runtime."""
    atlan_yaml = root / "atlan.yaml"
    if not atlan_yaml.is_file():
        return False
    try:
        text = atlan_yaml.read_text(encoding="utf-8")
    except OSError:
        return False
    m = _SDR_FLAG_RE.search(text)
    return m is not None and m.group(1).lower() == "true"


def _strip_comment(value: str) -> str:
    """Drop a trailing YAML comment and surrounding quotes from a scalar."""
    idx = value.find("#")
    if idx != -1:
        value = value[:idx]
    return value.strip().strip("'\"").strip()


def _reusable_inputs(lines: list[str], uses_idx: int) -> dict[str, str]:
    """Collect the ``with:`` mapping that belongs to the ``uses:`` at *uses_idx*.

    Scans forward from the ``uses:`` line for a sibling ``with:`` key at the
    same indentation, then reads the flat ``key: value`` entries nested under
    it.  Block scalars and nested mappings inside a value are not modelled —
    the reusable's inputs are all flat scalars, and an unparsed entry simply
    reads as absent (biasing toward a finding the author can suppress rather
    than a silent miss).
    """
    uses_indent = len(lines[uses_idx]) - len(lines[uses_idx].lstrip())
    with_indent: int | None = None
    inputs: dict[str, str] = {}

    for line in lines[uses_idx + 1 :]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if with_indent is None:
            if indent < uses_indent:
                return inputs  # dedented out of the job before finding `with:`
            if indent == uses_indent and line.strip().rstrip(":") == "with":
                with_indent = indent
            elif indent == uses_indent:
                continue  # a sibling key (e.g. `secrets: inherit`)
            continue
        if indent <= with_indent:
            break  # `with:` block closed
        m = _WITH_ENTRY_RE.match(line)
        if m is not None:
            inputs[m.group("key")] = _strip_comment(m.group("val"))
    return inputs


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


def _t020_message(workflow: str) -> str:
    # A bespoke sdr-full-dag.yaml is deleted outright; a tests.yaml that grew its
    # own e2e job is converted in place. Say the right one — the wrong verb here
    # is the difference between a two-line fix and a deleted CI entry point.
    is_tests_yaml = Path(workflow).name in {"tests.yaml", "tests.yml"}
    action = (
        "Replace this workflow's hand-rolled job with the thin caller"
        if is_tests_yaml
        else (
            "Delete this workflow and route the e2e through the caller in "
            ".github/workflows/tests.yaml"
        )
    )
    return (
        f"{workflow} calls the SDK's {_SDR_E2E_ACTION} composite action directly "
        "instead of delegating to the reusable Tests workflow. A bespoke full-DAG "
        "job is a private fork of a contract the SDK keeps evolving: it pins "
        "one hard-coded test-path (a second tests/e2e/ suite is then never run), it "
        "has no discover-e2e-suites matrix and therefore no per-leg "
        "ATLAN_DEPLOYMENT_NAME queue isolation (see T016/T017), it does not "
        "participate in the required Tests Gate check, and it silently misses every "
        f"later input the reusable gains. {action}: a `tests` job with "
        f"`uses: {_TESTS_REUSABLE}@main` plus `secrets: inherit` — see "
        "atlan-mysql-app/.github/workflows/tests.yaml, or regenerate the caller with "
        "`atlan-application-sdk-conformance bootstrap`. Keep the repo's real e2e "
        "assets (tests/e2e/ suites and the .github/e2e/ config dir) — the reusable "
        "points the sdr-e2e action at exactly those paths."
    )


def _t021_message(reason: str) -> str:
    return (
        f"tests/e2e/ ships collectable suites but nothing in .github/workflows/ runs "
        f"them ({reason}; no workflow names a tests/e2e path either). An e2e suite "
        "nothing executes reads as coverage in review "
        "and never runs: no full-DAG signal, and the connector's agent-mode path stays "
        "unexercised. Add (or fix) the caller in .github/workflows/tests.yaml — "
        f"`uses: {_TESTS_REUSABLE}@main` with `app-name` and a non-empty "
        "`app-image-name` (empty disables the connector image build the e2e worker "
        "container starts from) and `enable-e2e` left at its default true. The e2e job "
        "is then gated on the `e2e` PR label or a workflow_dispatch with "
        "`run_e2e: true`."
    )


def _t022_message() -> str:
    return (
        "atlan.yaml declares self_deployed_runtime: true but the reusable-workflow "
        "caller does not set `two-store: true`. Without the ADR-0014 two-store "
        "posture the e2e worker's `objectstore` and `atlan-objectstore` bindings "
        "resolve to the same bucket, so a connector that never bridges its "
        "transformed artifacts across the boundary (App.upload() / a key-preserving "
        "deployment->upstream bridge) still greens — the silent-zero-assets class "
        "P030 polices statically. Set `two-store: true` in the caller's `with:` block "
        "so the missing bridge surfaces as zero downstream assets and lineage in "
        "Atlas instead of passing."
    )


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------


def scan_path(path: Path, root: Path) -> list[Finding]:  # noqa: ARG001
    """No-op: T020–T022 need repo-level context; the runner calls scan_all."""
    return []


def scan_all(paths: list[Path], root: Path) -> list[Finding]:
    """Grade the repo's e2e CI wiring (T020, T021, T022)."""
    findings: list[Finding] = []
    # (relative path, text, suppressions, line index of the `uses:` reusable call)
    callers: list[
        tuple[str, str, dict[int, tuple[frozenset[str] | None, str]], int]
    ] = []
    names_e2e_path = False

    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            rel = str(path.relative_to(root))
        except ValueError:
            rel = str(path)

        lines = text.splitlines()
        suppressions = parse_toml_suppressions(text)

        if _E2E_PATH_RE.search(text) or any(
            _USES_E2E_REUSABLE_RE.match(line) for line in lines
        ):
            names_e2e_path = True

        reusable_idx: int | None = None
        for idx, line in enumerate(lines):
            if _USES_REUSABLE_RE.match(line):
                reusable_idx = idx
                break
        if reusable_idx is not None:
            callers.append((rel, text, suppressions, reusable_idx))

        # T020 — a direct sdr-e2e invocation outside the reusable.
        if reusable_idx is None:
            for idx, line in enumerate(lines):
                if not _USES_SDR_E2E_RE.match(line):
                    continue
                findings.append(
                    make_toml_finding(
                        rule_id=RULE_T020,
                        file=rel,
                        line=idx + 1,
                        column=1,
                        message=_t020_message(rel),
                        suppressions=suppressions,
                    )
                )

    if not _has_e2e_suites(root):
        return findings

    # T021 — can ANY workflow run the suites? A bespoke runner counts here (it
    # is the wrong mechanism, which is T020's finding, not an unreachable suite).
    runnable = names_e2e_path
    reason = "no workflow calls tests-reusable.yaml"
    if not runnable:
        for rel, _text, suppressions, uses_idx in callers:
            inputs = _reusable_inputs(_read_lines(root, rel), uses_idx)
            enable = inputs.get("enable-e2e", "true").lower()
            image = inputs.get("app-image-name", "")
            if enable == "false":
                reason = f"{rel} sets enable-e2e: false"
                continue
            if not image:
                reason = f"{rel} leaves app-image-name empty"
                continue
            runnable = True
            break

    if not runnable:
        anchor_file, anchor_line, anchor_suppressions = _t021_anchor(root, callers)
        findings.append(
            make_toml_finding(
                rule_id=RULE_T021,
                file=anchor_file,
                line=anchor_line,
                column=1,
                message=_t021_message(reason),
                suppressions=anchor_suppressions,
            )
        )

    # T022 — SDR apps must run the e2e under the two-store posture.
    if _is_sdr_app(root):
        for rel, _text, suppressions, uses_idx in callers:
            inputs = _reusable_inputs(_read_lines(root, rel), uses_idx)
            if inputs.get("two-store", "").lower() == "true":
                continue
            findings.append(
                make_toml_finding(
                    rule_id=RULE_T022,
                    file=rel,
                    line=uses_idx + 1,
                    column=1,
                    message=_t022_message(),
                    suppressions=suppressions,
                )
            )

    return findings


def _read_lines(root: Path, rel: str) -> list[str]:
    try:
        return (root / rel).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []


def _t021_anchor(
    root: Path,
    callers: list[tuple[str, str, dict[int, tuple[frozenset[str] | None, str]], int]],
) -> tuple[str, int, dict[int, tuple[frozenset[str] | None, str]]]:
    """Where to anchor a T021 finding.

    Prefer the caller that exists but is misconfigured (its ``uses:`` line), so
    the annotation lands next to the input the author has to change.  With no
    caller at all, anchor at ``.github/workflows/tests.yaml`` when present — the
    file that should hold it — and fall back to the repo's first e2e suite.
    """
    if callers:
        rel, _text, suppressions, uses_idx = callers[0]
        return rel, uses_idx + 1, suppressions

    tests_yaml = root / ".github" / "workflows" / "tests.yaml"
    if tests_yaml.is_file():
        try:
            return (
                ".github/workflows/tests.yaml",
                1,
                parse_toml_suppressions(tests_yaml.read_text(encoding="utf-8")),
            )
        except OSError:
            pass

    e2e_dir = root / "tests" / "e2e"
    suites = sorted(
        p
        for p in e2e_dir.rglob("*.py")
        if p.is_file()
        and is_collectable_test_file(p.name)
        and "__pycache__" not in p.parts
    )
    if suites:
        rel_suite = str(suites[0].relative_to(root))
        try:
            return (
                rel_suite,
                1,
                parse_toml_suppressions(suites[0].read_text(encoding="utf-8")),
            )
        except OSError:
            return rel_suite, 1, {}
    return "tests/e2e", 1, {}


main = make_cli_main(
    scan_all=scan_all,
    discover=discover,
    description=(
        "T020-T022: the full-DAG e2e must run through the reusable Tests workflow "
        "(no bespoke sdr-e2e workflow, suites reachable in CI, two-store posture on "
        "SDR apps)."
    ),
    default_scan_paths=(".github/workflows",),
)
"""CLI entry point for the T020-T022 e2e-workflow-shape checks."""


if __name__ == "__main__":
    sys.exit(main())
