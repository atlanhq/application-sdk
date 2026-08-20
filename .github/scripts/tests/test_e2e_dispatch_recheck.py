"""Tests for .github/actions/e2e-dispatch-recheck/e2e_dispatch_recheck.py.

Co-located module (checked out with the composite action in consumer repos); the
test lives here with the other action-script tests.

Two properties carry this file. The first is that a superseded SHA stands down
BEFORE the tenant lease. The second, and the one worth more scrutiny, is that
nothing else ever does: the script decides whether to abandon an e2e run, and a
wrong "true" costs a live commit its only e2e. So every ambiguity below —
unreadable ref, unreadable blob, a claim with no PR recorded, an unreadable PR, a
ref that is not a commit sha — is pinned to "false", and the wiring tests pin the
gates to read the OUTPUT rather than the job result so an outright job failure
leases exactly as it did before.

The HTTP client is stubbed at the ``run()`` seam with curl-shaped responses, so
status handling goes through the same parser production uses. An unstubbed
request raises rather than answering 200, which is what stops a test passing
because the script quietly stopped making a call it was supposed to make.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest
import yaml

# The recheck ships inside a composite action, so it is not importable the way
# the other scripts under test are. The guard is imported alongside it because
# the pair share a cross-repo ref contract that only a test can hold together.
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(
    0, str(Path(__file__).parent.parent.parent / "actions" / "e2e-dispatch-recheck")
)

import e2e_dispatch_guard  # noqa: E402
from e2e_dispatch_recheck import (  # noqa: E402
    REF_NAMESPACE,
    claim_ref,
    is_superseded,
    main,
)

REPO = "atlanhq/application-sdk"
APP = "atlan-openapi-app"
# The real pair from application-sdk PR #3322: the bot force-pushed HEAD over
# STALE 54 seconds after the label started the run that dispatched STALE.
STALE = "be82fade2ad4a59a20be5f3709e57c1992c40fc3"
HEAD = "d47789e09fc1a41e48fa9d8ce9a1062c3d6eac08"
BLOB = "b" * 40
PR = 3322


# --- fake HTTP client ------------------------------------------------------


class FakeHTTP:
    def __init__(self) -> None:
        self.routes: dict[str, tuple[int, object]] = {}
        self.calls: list[str] = []
        self.cmds: list[list[str]] = []
        self.inputs: list[str | None] = []

    def route(self, contains: str, response: tuple[int, object]) -> None:
        self.routes[contains] = response

    def __call__(self, cmd: list[str], **kwargs):
        url = cmd[-1]
        self.calls.append(url)
        self.cmds.append(list(cmd))
        self.inputs.append(kwargs.get("input"))
        for contains, (status, body) in self.routes.items():
            if contains not in url:
                continue
            payload = "" if body is None else json.dumps(body)
            return _completed(f"HTTP/2 {status}\r\n\r\n{payload}")
        raise AssertionError(f"unstubbed request: {url}")


class _completed:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


@pytest.fixture
def http(monkeypatch: pytest.MonkeyPatch) -> FakeHTTP:
    monkeypatch.setenv("GH_TOKEN", "x")
    fake = FakeHTTP()
    monkeypatch.setattr("e2e_dispatch_recheck.run", fake)
    return fake


def _claim(pr_number: int | None = PR) -> dict:
    record: dict[str, object] = {"run_id": 32417548486, "attempt": 1}
    if pr_number is not None:
        record["pr_number"] = pr_number
    return {"content": base64.b64encode(json.dumps(record).encode()).decode()}


def _stub_claim(http: FakeHTTP, *, blob: dict | None = None) -> None:
    http.route(f"/git/ref/{REF_NAMESPACE}/", (200, {"object": {"sha": BLOB}}))
    http.route("/git/blobs/", (200, _claim() if blob is None else blob))


def _outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    out = tmp_path / "outputs"
    out.write_text("")
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary"))
    return out


def _read(out: Path) -> dict[str, str]:
    return dict(
        line.split("=", 1) for line in out.read_text().splitlines() if "=" in line
    )


# --- the case this exists for ----------------------------------------------


def test_a_superseded_sha_stands_down(http: FakeHTTP) -> None:
    _stub_claim(http)
    http.route(f"/pulls/{PR}", (200, {"head": {"sha": HEAD}}))

    assert is_superseded(REPO, APP, STALE) is True


def test_the_head_sha_proceeds(http: FakeHTTP) -> None:
    _stub_claim(http)
    http.route(f"/pulls/{PR}", (200, {"head": {"sha": HEAD}}))

    assert is_superseded(REPO, APP, HEAD) is False


def test_a_head_in_a_different_case_is_not_a_different_commit(http: FakeHTTP) -> None:
    """One SHA in two spellings must not read as two commits. That direction of
    error abandons the live commit's e2e, which is the one outcome worse than
    paying for a stale run."""
    _stub_claim(http)
    http.route(f"/pulls/{PR}", (200, {"head": {"sha": HEAD.upper()}}))

    assert is_superseded(REPO, APP, HEAD) is False


def test_it_reads_the_claim_the_dispatch_guard_wrote(http: FakeHTTP) -> None:
    """The ref layout is a cross-repo contract with e2e_dispatch_guard.claim_ref:
    the guard writes it in application-sdk, this reads it from a connector repo,
    and nothing in either repo's tests would notice the two drifting apart."""
    _stub_claim(http)
    http.route(f"/pulls/{PR}", (200, {"head": {"sha": HEAD}}))

    is_superseded(REPO, APP, STALE)

    assert claim_ref(APP, STALE) == f"refs/e2e-dispatch/{APP}/{STALE}"
    assert f"repos/{REPO}/git/ref/e2e-dispatch/{APP}/{STALE}" in http.calls[0]


def test_the_guard_and_the_recheck_agree_on_the_namespace() -> None:
    """Pins the duplicated constant. The two modules cannot import each other —
    only this one ships to connector repos — so the pair is held together here
    or not at all."""
    assert REF_NAMESPACE == e2e_dispatch_guard.REF_NAMESPACE


# Every connector repository the SDK's dispatch matrix can name. The claim ref
# is built from this string on one side and parsed from it on the other, so the
# fleet is the input set that actually matters.
CONNECTOR_REPOS = [
    "atlan-openapi-app",
    "atlan-mysql-app",
    "atlan-metabase-app",
    "atlan-postgres-app",
    "atlan-local-marketplace-app",
]


@pytest.mark.parametrize("repo_name", CONNECTOR_REPOS)
def test_the_two_ref_builders_agree_on_every_connector_name(repo_name: str) -> None:
    """The recheck does not reimplement the guard's slug(), on the grounds that
    none of its rules can fire on a repository name. That is a claim about the
    fleet, so it is asserted against the fleet rather than argued in a comment —
    if a connector is ever added whose name slug() would rewrite, this fails
    instead of the check silently looking for a ref that was never written."""
    assert claim_ref(repo_name, STALE) == e2e_dispatch_guard.claim_ref(repo_name, STALE)


def test_a_repo_name_in_a_different_case_still_finds_its_claim() -> None:
    """slug() lowercases and a repository name need not be lowercase, which is
    the one normalisation difference that can actually bite."""
    assert claim_ref("Atlan-OpenAPI-App", STALE) == e2e_dispatch_guard.claim_ref(
        "Atlan-OpenAPI-App", STALE
    )


def test_the_guard_records_the_pr_number_this_reads_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of the same contract: the guard has to WRITE the field this
    reads. A claim without it is inert here (see the test below), so the guard
    silently dropping it would turn the recheck off with nothing going red."""
    written: list[dict] = []

    def fake_request(method, path, payload=None, **kwargs):
        written.append(json.loads(payload["content"]))
        return type("R", (), {"status": 201, "body": {"sha": BLOB}, "message": ""})()

    monkeypatch.setattr(e2e_dispatch_guard, "gh_request", fake_request)
    e2e_dispatch_guard.create_claim_blob(REPO, 1, 1, 1000.0, PR)

    assert written[0]["pr_number"] == PR


# --- every doubt resolves towards running the e2e --------------------------


def test_no_claim_ref_proceeds(http: FakeHTTP) -> None:
    """A hand-pinned application_sdk_ref, or a claim already pruned. Neither is
    an SDK pull-request dispatch, and skipping a human's deliberate run against a
    specific SDK commit would be a silent, baffling no-op."""
    http.route(f"/git/ref/{REF_NAMESPACE}/", (404, {"message": "Not Found"}))

    assert is_superseded(REPO, APP, STALE) is False


def test_a_claim_without_a_pr_number_proceeds(http: FakeHTTP) -> None:
    """Every claim written before the guard started recording the field, and
    every merge_group claim, which has no PR by construction. This is what lets
    the recheck ship without being in lockstep with the guard change."""
    _stub_claim(http, blob=_claim(pr_number=None))

    assert is_superseded(REPO, APP, STALE) is False


@pytest.mark.parametrize(
    "answer",
    [
        (403, {"message": "Resource not accessible by integration"}),
        (403, {"message": "API rate limit exceeded"}),
        (500, {"message": "boom"}),
        (200, {"message": "no object here"}),
        (200, None),
    ],
)
def test_an_unreadable_claim_ref_proceeds(http: FakeHTTP, answer) -> None:
    http.route(f"/git/ref/{REF_NAMESPACE}/", answer)

    assert is_superseded(REPO, APP, STALE) is False


@pytest.mark.parametrize(
    "answer",
    [
        (404, {"message": "Not Found"}),
        (403, {"message": "API rate limit exceeded"}),
        (200, {"content": "not base64 json"}),
        (200, {"content": base64.b64encode(b"[]").decode()}),
        (200, {"encoding": "base64"}),
    ],
)
def test_an_unreadable_claim_record_proceeds(http: FakeHTTP, answer) -> None:
    http.route(f"/git/ref/{REF_NAMESPACE}/", (200, {"object": {"sha": BLOB}}))
    http.route("/git/blobs/", answer)

    assert is_superseded(REPO, APP, STALE) is False


@pytest.mark.parametrize(
    "answer",
    [
        (404, {"message": "Not Found"}),
        (403, {"message": "API rate limit exceeded"}),
        (500, {"message": "boom"}),
        (200, {"number": PR}),
        (200, {"head": {"ref": "bump-version-main"}}),
    ],
)
def test_an_unreadable_pull_request_proceeds(http: FakeHTTP, answer) -> None:
    _stub_claim(http)
    http.route(f"/pulls/{PR}", answer)

    assert is_superseded(REPO, APP, STALE) is False


def test_a_pr_number_of_true_is_not_pull_request_one(http: FakeHTTP) -> None:
    """bool is an int in Python, so a corrupted record carrying `true` would
    otherwise be compared against whatever PR #1 happens to point at today."""
    _stub_claim(http, blob=_claim(pr_number=True))  # type: ignore[arg-type]

    assert is_superseded(REPO, APP, STALE) is False


def test_a_transport_failure_proceeds(
    http: FakeHTTP, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "e2e_dispatch_recheck.run",
        lambda *a, **k: _completed(
            "", returncode=6, stderr="curl: (6) could not resolve"
        ),
    )

    assert is_superseded(REPO, APP, STALE) is False


# --- the entry point --------------------------------------------------------


def test_main_writes_true_for_a_superseded_ref(
    http: FakeHTTP, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = _outputs(tmp_path, monkeypatch)
    _stub_claim(http)
    http.route(f"/pulls/{PR}", (200, {"head": {"sha": HEAD}}))

    assert main(["--sdk-repo", REPO, "--sdk-ref", STALE, "--app", APP]) == 0

    assert _read(out)["superseded"] == "true"


def test_main_writes_false_for_the_head(
    http: FakeHTTP, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = _outputs(tmp_path, monkeypatch)
    _stub_claim(http)
    http.route(f"/pulls/{PR}", (200, {"head": {"sha": HEAD}}))

    assert main(["--sdk-repo", REPO, "--sdk-ref", HEAD, "--app", APP]) == 0

    assert _read(out)["superseded"] == "false"


@pytest.mark.parametrize(
    "ref", ["", "   ", "main", "refs/heads/main", "be82fad", "z" * 40]
)
def test_a_ref_that_is_not_a_commit_sha_asks_nothing(
    http: FakeHTTP, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ref
) -> None:
    """The connector's own pull_request/push runs pass empty, and pinning the SDK
    by branch is a supported way to run e2e. Neither names a commit that can be
    superseded — and FakeHTTP raising on an unstubbed call is what proves no
    request is made, rather than one being made and ignored."""
    out = _outputs(tmp_path, monkeypatch)

    assert main(["--sdk-repo", REPO, "--sdk-ref", ref, "--app", APP]) == 0

    assert _read(out)["superseded"] == "false"
    assert http.calls == []


def test_main_never_fails_the_job(
    http: FakeHTTP, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit 0 on the unhappy path too. This job sits in front of the tenant
    lease; a non-zero exit here would take the e2e down with it, which is the
    opposite of what a best-effort optimisation may do."""
    _outputs(tmp_path, monkeypatch)
    http.route(f"/git/ref/{REF_NAMESPACE}/", (500, {"message": "boom"}))

    assert main(["--sdk-repo", REPO, "--sdk-ref", STALE, "--app", APP]) == 0


def test_the_token_never_reaches_the_command_line(http: FakeHTTP) -> None:
    """It goes in through stdin (-K -), so nothing else sharing the runner can
    read it out of /proc/<pid>/cmdline while the request is in flight."""
    _stub_claim(http)
    http.route(f"/pulls/{PR}", (200, {"head": {"sha": HEAD}}))

    is_superseded(REPO, APP, STALE)

    assert not any("x" == arg or "Bearer x" in arg for arg in http.cmds[0])
    assert http.inputs[0] is not None and "Bearer x" in http.inputs[0]


# --- wiring -----------------------------------------------------------------


def _workflow() -> dict:
    path = Path(__file__).parent.parent.parent / "workflows" / "tests-reusable.yaml"
    return yaml.safe_load(path.read_text())


def _action() -> dict:
    path = (
        Path(__file__).parent.parent.parent
        / "actions"
        / "e2e-dispatch-recheck"
        / "action.yaml"
    )
    return yaml.safe_load(path.read_text())


def test_the_recheck_runs_before_the_lease_and_not_after() -> None:
    """Asked as late as the DAG allows, because the answer is only worth as much
    as its freshness — and strictly before anything takes a tenant."""
    jobs = _workflow()["jobs"]

    assert "sdk-head-recheck" in jobs["lease-tenant"]["needs"]
    assert "lease-tenant" not in jobs["sdk-head-recheck"]["needs"]


def test_the_lease_reads_the_output_not_the_job_result() -> None:
    """The whole fail-open posture in one assertion. `needs.<job>.result` would
    make an infrastructure failure of the recheck SKIP the lease — and a skipped
    lease skips the install and greens the run vacuously. The output is empty
    when the job did not answer, so `!= 'true'` leases exactly as before."""
    gate = _workflow()["jobs"]["lease-tenant"]["if"]

    assert "needs.sdk-head-recheck.outputs.superseded != 'true'" in gate
    assert "needs.sdk-head-recheck.result" not in gate


def test_the_lease_gate_lifts_the_implicit_success_check() -> None:
    """`always()` is what lets the `if:` above be consulted at all. Without it
    GitHub applies an implicit success() over every need and skips the job before
    the gate is read, so a failed recheck would skip the lease — the exact
    fail-CLOSED behaviour the output check exists to avoid. And because always()
    lifts the check for the other needs too, they have to be named explicitly."""
    gate = _workflow()["jobs"]["lease-tenant"]["if"]

    assert "always()" in gate
    assert "needs.discover-e2e.result == 'success'" in gate
    assert "needs.merge-e2e-image.result == 'success'" in gate


def test_the_legs_stand_down_with_the_lease() -> None:
    """Gating the lease alone is not enough, and getting this wrong is worse than
    not doing it: a SKIPPED lease-tenant is the benign value in the legs' own
    clauses (it is the install-app-to-tenant:false path), so the legs would run
    against a tenant nobody installed onto with expected-app-version empty — a
    silently passing wrong-version run, which is the FND-31 failure the lease was
    built to make impossible."""
    e2e = _workflow()["jobs"]["e2e"]

    assert "sdk-head-recheck" in e2e["needs"]
    assert "needs.sdk-head-recheck.outputs.superseded != 'true'" in e2e["if"]


def test_the_install_and_the_release_cascade_off_the_lease() -> None:
    """Neither needs its own clause: both already gate on lease-tenant having
    RUN, and a superseded run skips it. Pinned so that gating is not later
    loosened to `always()` without noticing it carries the stand-down too."""
    jobs = _workflow()["jobs"]

    for name in ("prepare-tenant", "release-tenant"):
        assert "needs.lease-tenant.result != 'skipped'" in jobs[name]["if"], name


def test_the_recheck_is_keyed_on_the_repo_name_not_the_app_name() -> None:
    """The claim ref the SDK writes is keyed on the connector REPOSITORY, which
    is what its dispatch matrix iterates over. `inputs.app-name` is the short
    form ("openapi", not "atlan-openapi-app") and would find no claim at all —
    a check that silently never fires."""
    step = _workflow()["jobs"]["sdk-head-recheck"]["steps"][0]

    assert step["with"]["app"] == "${{ github.event.repository.name }}"
    assert step["with"]["sdk-ref"] == "${{ inputs.application-sdk-ref }}"


def test_the_recheck_is_consumed_at_main() -> None:
    """Same reason the tenant lease is: this reads a ref layout the dispatching
    SDK run wrote, so the protocol must not vary with whichever ref a connector
    happened to check out."""
    step = _workflow()["jobs"]["sdk-head-recheck"]["steps"][0]

    assert step["uses"].endswith("/e2e-dispatch-recheck@main")


def test_both_gate_evaluations_are_told_about_the_stand_down() -> None:
    """Standing down produces the exact tuple the gate driver's matrix-skipped
    anomaly exists to catch — discovery success, matrix skipped, no install-path
    failure. Untold, `tests-passed` reds the required check AND `report-to-sdk`
    mirrors conclusion=failure onto the dispatching SDK commit: "your change
    broke the connector" for a run that deliberately stood down, which is the
    FND-218 misattribution the cancelled/failure split exists to prevent.

    Both call sites, because the two are meant to be one decision evaluated
    twice: a gate told and a callback not told would disagree, which is the
    drift the shared driver exists to remove."""
    jobs = _workflow()["jobs"]

    for name in ("tests-passed", "report-to-sdk"):
        step = next(s for s in jobs[name]["steps"] if s.get("id") == "gate")
        assert step["uses"].endswith("/verify-test-gate@main"), name
        assert (
            step["with"]["superseded"]
            == "${{ needs.sdk-head-recheck.outputs.superseded }}"
        ), name
        # The expression above renders EMPTY unless the job declares the need,
        # and empty reads as "unexplained" — the anomaly would fire silently.
        assert "sdk-head-recheck" in jobs[name]["needs"], name


def test_the_gate_input_is_optional_and_defaults_to_unexplained() -> None:
    """The driver is consumed cross-repo at @main. A required input would break
    every connector the instant it merged, and a default of "true" would let any
    unexplained skipped matrix green the gate — the anomaly's whole purpose."""
    gate_action = yaml.safe_load(
        (
            Path(__file__).parent.parent.parent
            / "actions"
            / "verify-test-gate"
            / "action.yaml"
        ).read_text()
    )
    superseded = gate_action["inputs"]["superseded"]

    assert superseded["required"] is False
    assert superseded["default"] == "false"


def test_the_action_needs_no_write_permission() -> None:
    """Every read is of public data in another repository, so the job runs on
    contents:read. A check that quietly required more would fail open forever."""
    job = _workflow()["jobs"]["sdk-head-recheck"]

    assert job["permissions"] == {"contents": "read"}
    assert _action()["runs"]["using"] == "composite"
