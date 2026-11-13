"""One Trivy engine, one CLI pin, and a scan-and-push action that does what it says.

FND-4904. The repo ran Trivy two ways: the install-only
``aquasecurity/setup-trivy`` with a pinned CLI, and the all-in-one
``aquasecurity/trivy-action`` with whatever Trivy it bundles. The repo's own
comments said the bundled one reports no OS findings on Chainguard/Wolfi
images, and the SDK's PR gate (``trivy-container``) and its release build
(``secure-build-push-apps``) were the ones still on it. Nothing tied the two
versions together either, so a fix to one engine never reached the other.

These guards fail silently if they regress, which is why they are tests:

* a reintroduced ``trivy-action`` scans and passes, finding less;
* a setup-trivy copy left at an old version scans and passes, finding
  something else;
* an input the action reads but never declares is always empty, with no error
  (that is how ``secret-files`` sat in secure-build-push-apps).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import cast

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
GITHUB = REPO_ROOT / ".github"
ACTIONS = GITHUB / "actions"
COMPOSITE = ACTIONS / "trivy-image-scan" / "action.yaml"

Mapping = dict[str, object]

SETUP_TRIVY = "aquasecurity/setup-trivy@"
TRIVY_ACTION = "aquasecurity/trivy-action"

# Files that run `trivy image` themselves. The composite is the recipe; the
# other two run in connector repos, where they cannot reach it by `./` path,
# and repeat it inline. A fifth copy has to be added here on purpose.
IMAGE_SCANNERS = {
    ".github/actions/trivy-image-scan/action.yaml",
    ".github/actions/trivy-container/action.yaml",
    ".github/workflows/build-and-scan.yaml",
}

# Actions that must scan through the composite, not a copy.
COMPOSITE_CALLERS = ("sdk-trivy-scan", "secure-build-push-apps")


def _load(path: Path) -> Mapping:
    return cast(Mapping, yaml.safe_load(path.read_text(encoding="utf-8")))


def _ci_files() -> list[Path]:
    files: list[Path] = []
    for sub in ("actions", "workflows", "workflow-templates"):
        files.extend(
            p
            for p in sorted((GITHUB / sub).rglob("*"))
            if p.is_file() and p.suffix in {".yaml", ".yml"}
        )
    return files


def _steps(doc: Mapping) -> list[Mapping]:
    steps: list[Mapping] = []
    runs = doc.get("runs")
    if isinstance(runs, dict):
        steps.extend(cast(list[Mapping], cast(Mapping, runs).get("steps") or []))
    jobs = doc.get("jobs")
    if isinstance(jobs, dict):
        for job in cast(dict[str, Mapping], jobs).values():
            steps.extend(cast(list[Mapping], job.get("steps") or []))
    return steps


def _rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _composite_steps() -> list[Mapping]:
    return _steps(_load(COMPOSITE))


def _canonical_setup_trivy() -> tuple[str, str]:
    """(uses, version) the composite installs Trivy with."""
    doc = _load(COMPOSITE)
    inputs = cast(dict[str, Mapping], doc["inputs"])
    version = str(inputs["trivy-version"]["default"])
    install = next(
        s for s in _composite_steps() if str(s.get("uses", "")).startswith(SETUP_TRIVY)
    )
    with_ = cast(Mapping, install["with"])
    assert with_["version"] == "${{ inputs.trivy-version }}", (
        "the composite must install the version its input names"
    )
    return str(install["uses"]), version


def test_canonical_pin_is_a_concrete_version() -> None:
    _, version = _canonical_setup_trivy()
    assert re.fullmatch(r"v\d+\.\d+\.\d+", version), version


def test_no_ci_file_uses_the_bundled_trivy_action() -> None:
    offenders = [
        _rel(p)
        for p in _ci_files()
        if any(
            str(s.get("uses", "")).startswith(TRIVY_ACTION) for s in _steps(_load(p))
        )
    ]
    assert not offenders, (
        f"{TRIVY_ACTION} bundles a Trivy that reports no OS findings on the "
        "Chainguard base application-sdk builds from. Use the trivy-image-scan "
        "composite, or setup-trivy at its pin where `./` cannot reach it:\n  "
        + "\n  ".join(offenders)
    )


def test_every_setup_trivy_matches_the_composite_pin() -> None:
    uses, version = _canonical_setup_trivy()
    seen = 0
    mismatched: list[str] = []
    for path in _ci_files():
        if path == COMPOSITE:
            continue
        for step in _steps(_load(path)):
            if not str(step.get("uses", "")).startswith(SETUP_TRIVY):
                continue
            seen += 1
            got_version = cast(Mapping, step.get("with") or {}).get("version")
            if step["uses"] != uses or got_version != version:
                mismatched.append(f"{_rel(path)}: {step['uses']} version={got_version}")
    # Guards the guard: the inline copies exist, so a parse change that found
    # none would pass vacuously.
    assert seen >= 3, f"expected the inline setup-trivy copies, found {seen}"
    assert not mismatched, (
        f"setup-trivy must be {uses} at {version}, the trivy-image-scan "
        "composite's pin. Bump them together:\n  " + "\n  ".join(mismatched)
    )


def test_only_the_known_files_run_trivy_image() -> None:
    runners = {
        _rel(p)
        for p in _ci_files()
        if any("trivy image" in str(s.get("run", "")) for s in _steps(_load(p)))
    }
    assert runners == IMAGE_SCANNERS, (
        "a new Trivy image scan belongs in the trivy-image-scan composite. "
        f"Unexpected: {sorted(runners - IMAGE_SCANNERS)}; "
        f"gone: {sorted(IMAGE_SCANNERS - runners)}"
    )


def test_the_sdk_actions_scan_through_the_composite() -> None:
    for name in COMPOSITE_CALLERS:
        steps = _steps(_load(ACTIONS / name / "action.yaml"))
        uses = [str(s.get("uses", "")) for s in steps]
        assert "./.github/actions/trivy-image-scan" in uses, name
        assert not any(u.startswith(SETUP_TRIVY) for u in uses), (
            f"{name} installs Trivy itself instead of through the composite"
        )


def test_every_trivy_step_has_the_db_fallback_repositories() -> None:
    """A single DB source 429s under a fleet-wide burst and fails the scan."""
    for path in sorted(IMAGE_SCANNERS):
        for step in _steps(_load(REPO_ROOT / path)):
            run = str(step.get("run", ""))
            if "trivy image" not in run:
                continue
            env = cast(Mapping, step.get("env") or {})
            for key in ("TRIVY_DB_REPOSITORY", "TRIVY_JAVA_DB_REPOSITORY"):
                assert "mirror.gcr.io" in str(env.get(key, "")), (
                    f"{path}: step {step.get('name')!r} has no {key} fallback list"
                )


def test_the_gate_only_fails_on_findings_with_a_fix() -> None:
    gate = next(
        s for s in _composite_steps() if s.get("name") == "Fail on fixable findings"
    )
    run = str(gate["run"])
    assert "--ignore-unfixed" in run
    assert "--exit-code 1" in run
    assert gate["if"] == "inputs.fail-severity != ''"


# ── secure-build-push-apps does what its contract says ───────────────────────

_SBPA = ACTIONS / "secure-build-push-apps" / "action.yaml"
_INPUT_REF = re.compile(r"inputs\.([A-Za-z0-9_-]+)")


def _referenced_inputs(path: Path) -> set[str]:
    body = path.read_text(encoding="utf-8").split("\nruns:", 1)[1]
    return set(_INPUT_REF.findall(body))


def _declared_inputs(path: Path) -> set[str]:
    return set(cast(Mapping, _load(path)["inputs"]))


def test_scan_actions_read_only_inputs_they_declare() -> None:
    """An undeclared input is always empty, and nothing reports it."""
    for path in (_SBPA, COMPOSITE, ACTIONS / "trivy-container" / "action.yaml"):
        undeclared = _referenced_inputs(path) - _declared_inputs(path)
        assert not undeclared, (
            f"{_rel(path)} reads undeclared inputs {sorted(undeclared)}"
        )


def test_secure_build_push_apps_declares_no_dead_inputs() -> None:
    unused = _declared_inputs(_SBPA) - _referenced_inputs(_SBPA)
    assert not unused, f"declared but never read: {sorted(unused)}"


def test_secure_build_push_apps_enforcement_is_an_input() -> None:
    """It hard-coded ENFORCEMENT_MODE='false' under a description promising a
    mandatory scan, so the push could never be blocked."""
    text = _SBPA.read_text(encoding="utf-8")
    assert "ENFORCEMENT_MODE" not in text
    scan = next(
        s
        for s in _steps(_load(_SBPA))
        if s.get("uses") == "./.github/actions/trivy-image-scan"
    )
    fail_severity = str(cast(Mapping, scan["with"])["fail-severity"])
    assert "inputs.fail-on-vulns" in fail_severity
    # The push must come after the scan, so a failed gate stops it.
    steps = _steps(_load(_SBPA))
    order = [s.get("id") or s.get("uses") for s in steps]
    assert order.index("./.github/actions/trivy-image-scan") < order.index(
        "docker_push"
    )
    assert "if" not in next(s for s in steps if s.get("id") == "docker_push"), (
        "the push must run only when every earlier step succeeded, the default"
    )


def test_secure_build_push_apps_callers_pass_only_declared_inputs() -> None:
    declared = _declared_inputs(_SBPA)
    callers = 0
    for path in _ci_files():
        for step in _steps(_load(path)):
            if "secure-build-push-apps" not in str(step.get("uses", "")):
                continue
            callers += 1
            extra = set(cast(Mapping, step.get("with") or {})) - declared
            assert not extra, f"{_rel(path)} passes undeclared inputs {sorted(extra)}"
    assert callers >= 2
