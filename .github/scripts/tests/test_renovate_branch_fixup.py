"""Structural guards for renovate-branch-fixup.yaml and its two callers (FND-4928).

The reusable is the one implementation of the Renovate fix-up lanes'
generate-then-publish split. The invariants that make the split worth having
(where the App token lives, which SHA both jobs work on, who may trigger the
lane) are asserted here once, against the reusable. Each caller is then
checked for what it passes in: a driver that honours the reusable's CLI
contract, and a re-entrancy subject that matches the commit the driver writes.

Every one of these fails silently in production if it drifts: a skipped job is
not red anywhere.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import bound_lock_branch
import pytest
import renovate_approval_conditions as approval
import renovate_pyatlan_table_sync
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
REUSABLE = WORKFLOWS / "renovate-branch-fixup.yaml"

#: caller file -> (its job id, the driver module it names)
CALLERS: dict[str, tuple[str, Any]] = {
    "renovate-lock-cooldown.yaml": ("bound", bound_lock_branch),
    "renovate-pyatlan-table-sync.yaml": ("sync", renovate_pyatlan_table_sync),
}


def _load(path: Path) -> dict[Any, Any]:
    # Keys are not all strings: YAML 1.1 reads a bare `on` as True.
    return yaml.safe_load(path.read_text())


def _jobs() -> dict[str, Any]:
    return _load(REUSABLE)["jobs"]


def _step(job: str, *, uses: str = "", name: str = "") -> dict[str, Any]:
    return next(
        s
        for s in _jobs()[job]["steps"]
        if (uses and uses in s.get("uses", "")) or (name and s.get("name") == name)
    )


def _caller(file: str) -> dict[str, Any]:
    job_id, _ = CALLERS[file]
    return _load(WORKFLOWS / file)["jobs"][job_id]


class TestGuards:
    """The `if:` on `generate`: two guards doing two unrelated jobs."""

    @property
    def guard(self) -> str:
        return _jobs()["generate"]["if"]

    def test_the_actor_list_matches_the_approval_gates_renovate_identities(self):
        """One list of Renovate identities, not two.

        The gate accepts these as PR authors; the lanes accept them as pushers.
        They describe the same fact, so a change to one that misses the other
        leaves a lane running for a PR the gate will not approve, or approving
        a PR the lane did not fix up.
        """
        declared = re.findall(r'"([a-z0-9-]+\[bot\])"', self.guard)
        assert sorted(declared) == sorted(approval.RENOVATE_AUTHORS)

    def test_the_condition_folds_to_a_single_line(self):
        # A continuation line indented deeper than the folded block keeps its
        # newline, leaving a literal line break inside the `${{ }}`.
        assert "\n" not in self.guard

    def test_the_guard_reads_the_actor_and_not_something_spoofable(self):
        # Deriving the identity from the pushed content (a commit author, a
        # trailer) would let the push assert who made it.
        assert "github.actor" in self.guard
        assert "head_commit.author" not in self.guard

    def test_re_entrancy_is_keyed_by_the_callers_subject(self):
        assert (
            "startsWith(github.event.head_commit.message, inputs.own-commit-subject)"
            in self.guard
        )

    def test_publish_runs_only_after_generate(self):
        # `publish` inherits the guard through `needs`: a skipped `generate`
        # skips it too.
        assert _jobs()["publish"]["needs"] == "generate"
        assert "if" not in _jobs()["publish"]


class TestJobSplit:
    """The point of the split is where the App token lives."""

    def test_generate_never_sees_a_secret_or_a_write_permission(self):
        generate = _jobs()["generate"]
        assert generate["permissions"] == {"contents": "read"}
        assert "secrets." not in yaml.safe_dump(generate)
        checkout = _step("generate", uses="actions/checkout@")
        assert checkout["with"]["persist-credentials"] is False

    def test_generate_does_not_commit(self):
        run = _step("generate", name="Generate the outputs")["run"]
        assert "--no-commit" in run
        assert '"$SCRIPT"' in run

    def test_generate_pins_the_version_parser_it_needs(self):
        # The runner image is not a promise: without `packaging` the drivers'
        # up-only comparisons misread every upgrade.
        run = _step("generate", name="Generate the outputs")["run"]
        assert "--with 'packaging==" in run, run
        assert "--no-project" in run, run

    def test_the_token_job_installs_and_resolves_nothing(self):
        dumped = yaml.safe_dump(_jobs()["publish"])
        for forbidden in ("setup-uv", "setup-node", "uv run", "npm "):
            assert forbidden not in dumped, forbidden
        apply = _step("publish", name="Commit the outputs")
        assert apply["run"] == 'python3 "$SCRIPT" --apply-from "$RUNNER_TEMP/fixup-in"'

    def test_both_jobs_work_on_the_pushed_sha(self):
        # `publish` commits onto what `generate` resolved; a moved branch must
        # make the push non-fast-forward, not absorb stale outputs.
        for job in ("generate", "publish"):
            checkout = _step(job, uses="actions/checkout@")
            assert checkout["with"]["ref"] == "${{ github.sha }}", job

    def test_the_artifact_is_the_one_staged_folder(self):
        # Several file paths make upload-artifact drop their common prefix
        # (FND-5015), so the upload is always the staging folder.
        stage = _step("generate", name="Stage the outputs")
        assert stage["run"] == 'python3 "$SCRIPT" --stage-to "$RUNNER_TEMP/fixup-out"'
        upload = _step("generate", uses="/.github/actions/upload-artifact-retry@")
        assert upload["with"]["path"] == "${{ runner.temp }}/fixup-out"
        assert upload["with"]["name"] == "${{ inputs.artifact-name }}"
        download = _step("publish", uses="/.github/actions/download-artifact-retry@")
        assert download["with"]["pattern"] == "${{ inputs.artifact-name }}*"
        assert download["with"]["path"] == "${{ runner.temp }}/fixup-in"

    def test_the_app_token_is_contents_only_on_this_repo(self):
        mint = _step("publish", uses="actions/create-github-app-token@")
        assert mint["with"]["repositories"] == "application-sdk"
        perms = {k for k in mint["with"] if k.startswith("permission-")}
        assert perms == {"permission-contents"}


@pytest.mark.parametrize("file", sorted(CALLERS))
class TestCallers:
    def test_it_calls_the_reusable(self, file: str):
        assert _caller(file)["uses"] == "./.github/workflows/renovate-branch-fixup.yaml"

    def test_it_grants_exactly_what_the_reusable_needs(self, file: str):
        # A called workflow can only narrow the caller's grant.
        assert _caller(file)["permissions"] == {
            "contents": "read",
            "statuses": "write",
        }

    def test_it_passes_every_required_secret(self, file: str):
        required = set(_load(REUSABLE)[True]["workflow_call"]["secrets"])
        assert set(_caller(file)["secrets"]) == required

    def test_the_script_is_the_driver_whose_commit_subject_it_skips(self, file: str):
        _, driver = CALLERS[file]
        with_ = _caller(file)["with"]
        assert (REPO_ROOT / with_["script"]).resolve() == Path(
            driver.__file__
        ).resolve()
        # Re-entrancy only works while the subject matches what the driver
        # writes.
        assert driver.COMMIT_MESSAGE.startswith(with_["own-commit-subject"])

    def test_the_driver_honours_the_reusables_cli_contract(
        self, file: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # `--stage-to` copies OUTPUT_PATHS at their repo paths; refusing a
        # missing output proves the flag is wired to the driver's own list.
        _, driver = CALLERS[file]
        monkeypatch.chdir(tmp_path)
        with pytest.raises(FileNotFoundError):
            driver.main(["--stage-to", str(tmp_path / "out")])
        for path in driver.OUTPUT_PATHS:
            (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / path).write_text(path)
        assert driver.main(["--stage-to", str(tmp_path / "out")]) == 0
        staged = {
            str(f.relative_to(tmp_path / "out"))
            for f in (tmp_path / "out").rglob("*")
            if f.is_file()
        }
        assert staged == set(driver.OUTPUT_PATHS)
