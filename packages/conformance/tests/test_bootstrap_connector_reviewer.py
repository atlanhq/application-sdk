"""Bootstrap installs the connector reviewer and retires the mothership kit (FND-5317).

One resync has to move a repository from the old reviewer to the new one:

- in: the ``connector-reviewer.yaml`` workflow, the managed review block in
  ``CLAUDE.md`` (and ``AGENTS.md`` where one exists), and a README in the
  repository's own principles folder;
- out: the review gate workflow and its script, the rule-fetch script, the
  session reminder and its hook entry, the local review skill,
  ``.mothership/reviewer.yaml``, and the whole local review-rules folder
  ``.mothership/review-rulesets/connector-app/``, real rules included.

Every repository reviews against its own principles now, so nothing in the old
rules folder is kept. Nothing else under ``.mothership/`` is touched, and no
symlink is followed out of the repository.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
from importlib.resources import files

import pytest
import yaml

from conformance.bootstrap.args import BOOTSTRAP_USAGE
from conformance.bootstrap.command import _KNOWN_LEGACY_CONNECTOR_REVIEW_BLOCK
from conformance.bootstrap.render import (
    CONNECTOR_REVIEWER_PRINCIPLES_README,
    LOCAL_REVIEW_RULES_DIR,
    MANAGED_ACTION_FILES,
    MANAGED_WORKFLOWS,
    RETIRED_ACTION_FILES,
    RETIRED_CONNECTOR_REVIEW_FILES,
    RETIRED_FILES,
    RETIRED_SHIMS_WITHOUT_REUSABLE,
    RETIRED_WORKFLOWS,
    render,
)
from conformance.cli import _cmd_bootstrap
from conformance.suite.checks.bootstrap_drift import discover, scan_path

_WORKFLOW = "connector-reviewer.yaml"
_BEGIN = "<!-- BEGIN APPLICATION SDK CONNECTOR REVIEW -->"
_END = "<!-- END APPLICATION SDK CONNECTOR REVIEW -->"
_BLOCK = render("connector-review-claude.md")
_README_REL, _README_TEMPLATE = CONNECTOR_REVIEWER_PRINCIPLES_README

_OLD_GATE_WORKFLOW = ".github/workflows/connector-review-gate.yaml"
_OLD_GATE_SCRIPT = ".github/scripts/connector_review_gate.py"
_REMINDER = ".claude/hooks/connector-review-reminder.sh"
# Everything the mothership kit put in a repository as a plain file.
_MOTHERSHIP_KIT_FILES = (
    _OLD_GATE_WORKFLOW,
    _OLD_GATE_SCRIPT,
    "scripts/fetch-review-rules.sh",
    _REMINDER,
    ".claude/skills/connector-review/SKILL.md",
    ".mothership/reviewer.yaml",
)

# The SessionStart entry the old kit added to .claude/settings.json.
_REMINDER_ENTRY = {
    "hooks": [{"type": "command", "command": _REMINDER, "timeout": 10}],
}


def _placeholder(name: str = "Widget", rule_id: str = "WIDGET-REPO-001") -> str:
    """The generic rule most repositories were seeded with, for *name*."""
    return (
        "---\n"
        "schema: 2\n"
        f"id: {rule_id}\n"
        "level: L3\n"
        "category: correctness\n"
        "globs: []\n"
        "severity: HIGH\n"
        "suppressible: true\n"
        "---\n"
        f"# Review {name}-specific behavior\n"
        "\n"
        "- Report a finding only when changed code breaks a concrete repository"
        " contract\n"
        "  shown by code, tests, generated contracts, or maintained documentation.\n"
        "- Check source-specific discovery, identity, extraction, lineage, or"
        " workflow\n"
        "  behavior only when the diff affects it.\n"
        "- Do not restate conformance, shared connector, or platform policy"
        " covered by\n"
        "  L1, L2, or L4.\n"
    )


# A rule somebody wrote. Invented for this test; it stands for any rule that
# says something about one connector.
_REAL_RULE = (
    "---\n"
    "schema: 2\n"
    "id: WIDGET-PAGING-001\n"
    "level: L3\n"
    "category: correctness\n"
    "globs: []\n"
    "severity: HIGH\n"
    "suppressible: true\n"
    "---\n"
    "# Widget pages are fetched to the end\n"
    "\n"
    "- A change to the page loop must still stop only on an empty page.\n"
)


def _write(root: pathlib.Path, rel: str, text: str) -> pathlib.Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _seed_local_rules(
    root: pathlib.Path, rules: dict[str, str], manifest: str | None = None
) -> pathlib.Path:
    """Write a local review-rules folder holding *rules* and its manifest."""
    folder = root / LOCAL_REVIEW_RULES_DIR
    for name, text in rules.items():
        _write(folder, f"rules/{name}", text)
    if manifest is None:
        manifest = "schema: 2\nrules:\n" + "".join(
            f"  - rules/{name}\n" for name in rules
        )
    _write(folder, "MANIFEST.yaml", manifest)
    return folder


def _tree(root: pathlib.Path) -> dict[str, str]:
    """Every file under *root*, as {relative path: content}."""
    return {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _manifest(capsys: pytest.CaptureFixture[str]) -> dict:
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def _c002(root: pathlib.Path) -> list[str]:
    return [
        finding.message for path in discover(root) for finding in scan_path(path, root)
    ]


@pytest.fixture
def repo(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """An empty app repository, with bootstrap run from its root."""
    root = tmp_path / "atlan-widget-app"
    root.mkdir()
    monkeypatch.chdir(root)
    return root


# ---------------------------------------------------------------------------
# The workflow
# ---------------------------------------------------------------------------


def _workflow() -> dict:
    return yaml.safe_load(render(_WORKFLOW))


def test_reviewer_workflow_is_a_managed_shim() -> None:
    assert _WORKFLOW in MANAGED_WORKFLOWS
    assert _WORKFLOW not in RETIRED_WORKFLOWS


def test_reviewer_run_name_carries_the_pull_request_number() -> None:
    """The reviewer finds an older run for the same pull request by this name.

    It has to be quoted: unquoted, YAML reads `` #`` as the start of a comment
    and every run would be named ``connector-reviewer``.
    """
    assert _workflow()["run-name"] == (
        "connector-reviewer #${{ github.event.issue.number || inputs.pr }}"
    )


def test_reviewer_runs_only_when_asked() -> None:
    """A comment or a manual dispatch. Never a push, never a pull_request event."""
    # PyYAML parses the bare key `on` as the boolean True.
    triggers = _workflow()[True]
    assert set(triggers) == {"issue_comment", "workflow_dispatch"}
    assert triggers["issue_comment"] == {"types": ["created"]}
    inputs = triggers["workflow_dispatch"]["inputs"]
    assert inputs["pr"]["required"] is True
    assert inputs["force"] == {
        "description": "Review again even if this commit was already reviewed",
        "type": "boolean",
        "default": False,
    }


def test_reviewer_has_no_concurrency_group() -> None:
    """Unlike the other comment-driven shim, on purpose.

    ``test-commands.yaml`` needs a group so two identical comments cannot both
    start a run. The reviewer holds its own lock: a running review finishes and
    a second request is dropped. A group here would cancel the running review
    or queue the second one, which is the opposite.
    """
    workflow = _workflow()
    assert "concurrency" not in workflow
    assert "concurrency" not in workflow["jobs"]["review"]


def test_reviewer_does_not_declare_merge_group() -> None:
    """It is not a required check, so a merge queue never waits on it.

    The gates that declare ``merge_group`` do so because a required context
    with no run on the queue commit blocks the queue. The reviewer reports a
    commit status when asked, not a check on every commit.
    """
    assert "merge_group" not in _workflow()[True]
    assert "pull_request" not in _workflow()[True]


def test_reviewer_job_calls_the_reusable_with_only_its_secrets() -> None:
    job = _workflow()["jobs"]["review"]
    assert job["uses"] == (
        "atlanhq/application-sdk/.github/workflows/"
        "connector-reviewer-reusable.yaml@main"
    )
    # Explicit, not `inherit`: the reusable gets nothing it does not use.
    assert job["secrets"] == {
        name: "${{ secrets." + name + " }}"
        for name in (
            "FLEET_APP_ID",
            "FLEET_APP_PRIVATE_KEY",
            "CONNECTOR_REVIEWER_GATEWAY_KEY",
        )
    }
    assert job["permissions"] == {"contents": "read"}
    assert _workflow()["permissions"] == {"contents": "read"}
    # A caller job has no steps, so it takes no timeout of its own.
    assert "steps" not in job
    assert "timeout-minutes" not in job


def test_reviewer_job_filters_ordinary_comments_before_a_runner_starts() -> None:
    condition = _workflow()["jobs"]["review"]["if"]
    assert condition == (
        "github.event_name != 'issue_comment' || "
        "(github.event.issue.pull_request && "
        "startsWith(github.event.comment.body, '/review'))"
    )


@pytest.mark.parametrize("argv", [[], ["--resync"]])
def test_every_run_installs_the_reviewer_and_removes_the_gate(
    repo: pathlib.Path, argv: list[str]
) -> None:
    """The workflow swap rides a bare run too: both files are under .github/."""
    _write(repo, _OLD_GATE_WORKFLOW, "name: Connector Review\n")
    _write(repo, _OLD_GATE_SCRIPT, "print('gate')\n")

    assert _cmd_bootstrap(argv) == 0

    assert (repo / ".github/workflows" / _WORKFLOW).read_text() == render(_WORKFLOW)
    assert not (repo / _OLD_GATE_WORKFLOW).exists()
    assert not (repo / _OLD_GATE_SCRIPT).exists()


# ---------------------------------------------------------------------------
# Retirement registries
# ---------------------------------------------------------------------------


def test_the_gate_and_its_script_are_retired_not_managed() -> None:
    assert _OLD_GATE_WORKFLOW in RETIRED_FILES
    assert _OLD_GATE_SCRIPT in RETIRED_FILES
    assert "connector-review-gate.yaml" in RETIRED_WORKFLOWS
    assert "connector-review-gate.yaml" not in MANAGED_WORKFLOWS
    assert _OLD_GATE_SCRIPT not in {dest for dest, _ in MANAGED_ACTION_FILES}
    # It was an inline job, so there is no application-sdk reusable to keep.
    assert "connector-review-gate.yaml" in RETIRED_SHIMS_WITHOUT_REUSABLE


def test_retired_action_files_are_retired_files() -> None:
    """C002 may only report a file bootstrap also removes."""
    assert set(RETIRED_ACTION_FILES) <= set(RETIRED_FILES)
    assert not set(RETIRED_ACTION_FILES) & {dest for dest, _ in MANAGED_ACTION_FILES}


@pytest.mark.parametrize(
    "template",
    [
        "connector-review-gate.yaml",
        "connector_review_gate.py",
        "connector-review.md",
        "connector-review-reminder.sh",
        "fetch-review-rules.sh",
    ],
)
def test_the_mothership_kit_templates_are_gone(template: str) -> None:
    with pytest.raises(FileNotFoundError):
        render(template)


def test_the_mothership_kit_files_are_all_retired() -> None:
    retired = set(RETIRED_FILES) | set(RETIRED_CONNECTOR_REVIEW_FILES)
    assert set(_MOTHERSHIP_KIT_FILES) <= retired


# ---------------------------------------------------------------------------
# C002: the retired gate is reported while on disk, never as missing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rel", [_OLD_GATE_WORKFLOW, _OLD_GATE_SCRIPT])
def test_c002_reports_a_retired_gate_file_still_on_disk(
    tmp_path: pathlib.Path, rel: str
) -> None:
    path = _write(tmp_path, rel, "legacy\n")
    assert path in discover(tmp_path)
    (finding,) = scan_path(path, tmp_path)
    assert finding.rule_id == "C002"
    assert finding.file == rel
    assert "retired" in finding.message
    assert "to remove it" in finding.message


@pytest.mark.parametrize("rel", [_OLD_GATE_WORKFLOW, _OLD_GATE_SCRIPT])
def test_c002_does_not_report_a_retired_gate_file_as_missing(
    tmp_path: pathlib.Path, rel: str
) -> None:
    assert scan_path(tmp_path / rel, tmp_path) == []


def test_c002_reports_a_missing_reviewer_workflow(tmp_path: pathlib.Path) -> None:
    path = tmp_path / ".github/workflows" / _WORKFLOW
    assert path in discover(tmp_path)
    (finding,) = scan_path(path, tmp_path)
    assert "absent" in finding.message


def test_c002_reports_a_drifted_reviewer_workflow(repo: pathlib.Path) -> None:
    _cmd_bootstrap([])
    path = repo / ".github/workflows" / _WORKFLOW
    path.write_text(path.read_text() + "# a local edit\n")
    (finding,) = scan_path(path, repo)
    assert "drifted" in finding.message


# ---------------------------------------------------------------------------
# The managed review block: CLAUDE.md and AGENTS.md
# ---------------------------------------------------------------------------


def test_block_keeps_the_markers_existing_blocks_are_found_by() -> None:
    assert _BLOCK.startswith(_BEGIN + "\n")
    assert _BLOCK.endswith(_END + "\n")


def test_block_keeps_the_conformance_command_and_its_exit_cases() -> None:
    assert (
        "## Mandatory before opening a pull request:"
        " conformance and a principles review\n"
    ) in _BLOCK
    assert "### 1. Conformance on your change\n" in _BLOCK
    assert (
        "git fetch origin main\n"
        "CONFORMANCE_SINCE=origin/main uv run atlan-application-sdk-conformance"
        " detect \\\n"
        "  --repo . --output /tmp/conformance.sarif\n"
    ) in _BLOCK
    for case in (
        "- Exit 0: nothing blocking was introduced.",
        "- Exit 1 with `[INTRODUCED]` lines:",
        "- Exit 1 with `falling back to the whole-repository verdict`",
        "- Exit 1 with a `[PARSE]` line:",
    ):
        assert case in _BLOCK


def _flat(text: str) -> str:
    """*text* with every run of whitespace as one space, so a re-wrap passes."""
    return " ".join(text.split())


def test_block_asks_for_a_principles_review_before_the_pull_request() -> None:
    """Step 2 sits between the conformance step and the reviewer section, and
    reads both principle sets the way the reviewer does."""
    conformance = _BLOCK.index("### 1. Conformance on your change\n")
    principles = _BLOCK.index("### 2. Review your change against the principles\n")
    reviewer = _BLOCK.index("## After opening a pull request: the connector reviewer\n")
    assert conformance < principles < reviewer
    flat = _flat(_BLOCK[principles:reviewer])
    for phrase in (
        "every `rp*.md` in `.github/connector-reviewer/principles/`",
        "every `fp*.md` in application-sdk's `.github/connector-reviewer/principles/`"
        " on `main`",
        "https://github.com/atlanhq/application-sdk/tree/main/.github/"
        "connector-reviewer/principles",
        # The fields every principle file carries (the principles README).
        "whose `applies-to` covers a file you changed",
        # The principles README: leave `applies-to` out and it applies everywhere.
        "a principle with no `applies-to` covers every changed file",
        "answer its `Ask:` questions against your diff (`git diff origin/main...HEAD`)",
        "skip what its `Don't flag:` excludes",
        "judge severity by its `Severity:` line",
        # Lenient: the agent fixes what it judges valid; the reviewer has the
        # final word once the pull request is open.
        "Fix the findings you judge valid, and leave the rest; the reviewer"
        " judges them again once the pull request is open.",
        "Do not weaken, delete or reword a principle so that your change passes.",
        "A change to a principle is its own pull request,",
        "This local review does not replace `/review`.",
    ):
        assert phrase in flat, phrase
    assert "Fix every finding before you open the pull request." not in flat


def test_block_asks_for_introduced_warnings_to_be_fixed_too() -> None:
    """runner.py compares only blocking findings with the base (`[INTRODUCED]`);
    a warning prints as `[WARN]` with no introduced/inherited mark, so the
    block says how to tell an introduced warning apart without inventing one."""
    flat = _flat(_BLOCK)
    for phrase in (
        "Fix every finding your change introduces before you open the pull"
        " request, blocking and warning alike",
        "no output line or SARIF field marks a warning as introduced.",
        "`[<rule>] [WARN] <file>:<line>:<column>`",
        "run `detect` on `origin/main` too, and ignore that run's exit code:",
        # The base run writes its own SARIF, so the branch's report survives,
        # and its checkout is removed afterwards.
        "git worktree add /tmp/conformance-base origin/main",
        "--repo /tmp/conformance-base --output /tmp/conformance-base.sarif",
        "git worktree remove /tmp/conformance-base",
        "A rule and file with more warnings on your branch than on `origin/main`"
        " is one your change introduced.",
        # The backlog stays the remediation lane's.
        "warnings your change did not introduce, do not fail the run. Leave them"
        " alone unless the task is to fix them",
    ):
        assert phrase in flat, phrase


def test_block_describes_the_connector_reviewer() -> None:
    assert "## After opening a pull request: the connector reviewer\n" in _BLOCK
    flat = _flat(_BLOCK)
    for phrase in (
        "It reviews only when asked.",
        "- `/review` reviews the pull request.",
        "- `/review force` reviews again",
        "a `connector-reviewer` status on the head commit",
        "- `/review dismiss F-xxxxxx <reason>`",
        "- `/review recheck F-xxxxxx <what changed, or why it is wrong>`",
        "- `/review waive <reason>`",
        "https://github.com/atlanhq/application-sdk/tree/main/.github/"
        "connector-reviewer/principles",
        "The repository's CI configuration remains authoritative for enforcement.",
    ):
        assert phrase in flat, phrase


def test_block_states_the_command_grammar() -> None:
    """event.py: every word on the `/review` line must be a known option, or the
    reviewer replies with usage and runs nothing; a note goes on the next line.
    The plain-review options are `force` and `verify-budget`."""
    flat = _flat(_BLOCK)
    for phrase in (
        "every word on its line must be part of one of the commands below",
        "Any other word gets a usage reply and nothing runs.",
        "Below a plain `/review` or `/review force`, a note on the next line is"
        " for the people reading the pull request; the reviewer does not read it.",
        # Until the parser takes the subcommand from the first line only, a
        # note below a bare `/review` that starts with one of these is read as
        # that command.
        "Do not start that note with `dismiss`, `recheck` or `waive`.",
        "Add `verify-budget` after it",
    ):
        assert phrase in flat, phrase


def test_block_says_subcommand_text_below_is_part_of_the_reason() -> None:
    """event.py reads a dismiss/recheck/waive reason from the whole comment, so
    text on the lines below is part of it, never an ignored note (lens
    F-98f9ca)."""
    flat = _flat(_BLOCK)
    assert (
        "After `/review dismiss`, `/review recheck` or `/review waive`, the"
        " finding ids and the reason may continue on the lines below, and the"
        " reviewer reads all of that text as the reason."
    ) in flat


def test_block_explains_contested_findings_and_the_status() -> None:
    """A contested finding is medium: it no longer blocks merge, but the status
    stays red until every finding of any severity is closed."""
    flat = _flat(_BLOCK)
    for phrase in (
        # skeptic.py checks every new blocking finding: critical and high.
        'A finding marked "⚖️ contested" was raised as critical or high',
        "It is now medium, so it does not block merge and the author may dismiss it.",
        "The status is green only when every finding, of any severity,",
        "Each finding names the principle it cites.",
    ):
        assert phrase in flat, phrase


def test_block_states_the_waiver_exception_to_a_green_status() -> None:
    """review.py: `/review waive` turns the head's status `success` with the
    findings still open, so "green only when every finding is closed" needs
    the exception (lens F-ce599e)."""
    flat = _flat(_BLOCK)
    for phrase in (
        "or when the review was waived.",
        "A waive turns the status green for that head commit only and resolves"
        " no finding",
    ):
        assert phrase in flat, phrase


def test_block_points_at_the_repositorys_own_principles() -> None:
    flat = _flat(_BLOCK)
    for phrase in (
        "This repository's own principles are in"
        " `.github/connector-reviewer/principles/`",
        "`rp01-<slug>.md`",
        "To add one, follow the README in that folder.",
        "That folder belongs to this repository.",
    ):
        assert phrase in flat, phrase
    # The README the block points at is the one bootstrap scaffolds there.
    assert _README_REL == ".github/connector-reviewer/principles/README.md"


def test_block_no_longer_claims_what_is_untrue() -> None:
    """Earlier wording: no review "needed" for exempt PRs (they are reported
    exempt only once someone asks), and a blanket ban on editing `.github/`
    that contradicted the repository's own principles folder."""
    flat = _flat(_BLOCK)
    assert "No review is needed for" not in flat
    assert "Do not edit bootstrap-managed files under `.github/`" not in flat


def test_block_says_what_an_agent_may_do() -> None:
    """An agent may ask for a review and dispute a finding with evidence.

    Dismissing and waiving record a person's decision, and force spends past
    the reviewer's admission rules, so an agent does none of them on its own.
    """
    section = _BLOCK.split("### What an agent may do\n", 1)[1]
    flat = " ".join(section.split())
    for phrase in (
        "may run conformance and the principles review",
        "comment `/review` after a push",
        "comment `/review recheck` with the evidence",
        "Dismissing a finding and waiving the review are decisions for a person.",
        "Use `/review force` only when a person asks for it.",
    ):
        assert phrase in flat, phrase


# The previous reviewer's vocabulary. Its review levels were named L1 to L4
# (conformance, shared connector rules, repository rules, platform rules).
# Nothing bootstrap writes into a repository uses those names any more.
_RETIRED_VOCABULARY = re.compile(
    r"\bL[1-4]\b|mothership|review-rulesets|ai-connector-review|"
    r"fetch-review-rules|reviewer\.yaml|connector-review skill|"
    r"CONNECTOR_REVIEW_GATE"
)


_TEMPLATES = pathlib.Path(str(files("conformance.bootstrap"))) / "templates"


def _installed_templates() -> list[str]:
    return sorted(path.name for path in _TEMPLATES.iterdir() if path.is_file())


def test_there_are_installed_templates_to_scan() -> None:
    """Guard the guard: an empty list would pass the scan below silently."""
    names = _installed_templates()
    assert _WORKFLOW in names
    assert "remediate.md" in names
    assert len(names) > 15


@pytest.mark.parametrize("template", _installed_templates())
def test_nothing_bootstrap_installs_uses_the_retired_review_vocabulary(
    template: str,
) -> None:
    text = (_TEMPLATES / template).read_text(encoding="utf-8")
    found = sorted({match.group(0) for match in _RETIRED_VOCABULARY.finditer(text)})
    assert not found, (
        f"{template} still uses the retired reviewer's terms {found}. Say"
        " 'conformance', 'the connector reviewer', 'fleet principles' and"
        " 'this repository's principles' instead."
    )


def test_fresh_claude_md_is_the_block(repo: pathlib.Path) -> None:
    assert _cmd_bootstrap(["--resync"]) == 0
    assert (repo / "CLAUDE.md").read_text() == _BLOCK


def test_block_is_appended_to_a_claude_md_without_one(repo: pathlib.Path) -> None:
    _write(repo, "CLAUDE.md", "# App notes\n\nKeep this.\n")
    assert _cmd_bootstrap(["--connector-review-kit"]) == 0
    assert (repo / "CLAUDE.md").read_text() == f"# App notes\n\nKeep this.\n\n{_BLOCK}"


def test_existing_block_is_replaced_in_place(repo: pathlib.Path) -> None:
    """The old kit's block goes; what the repository wrote around it stays."""
    old_block = (
        f"{_BEGIN}\n"
        "## Mandatory pre-commit review (L2-L4 rules; L1 before the PR and in PR"
        " CI)\n\nBefore any `git commit`, run the `connector-review` skill.\n"
        f"{_END}\n"
    )
    _write(repo, "CLAUDE.md", f"# App notes\n\n{old_block}\n## After\n\nKeep this.\n")

    assert _cmd_bootstrap(["--resync"]) == 0

    text = (repo / "CLAUDE.md").read_text()
    assert text == f"# App notes\n\n{_BLOCK}## After\n\nKeep this.\n"
    assert text.count(_BEGIN) == 1
    assert "connector-review` skill" not in text


def test_block_is_stable_across_reruns(
    repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(repo, "CLAUDE.md", "# App notes\n")
    _write(repo, "AGENTS.md", "# Agent notes\n")
    _cmd_bootstrap(["--resync"])
    before = _tree(repo)
    capsys.readouterr()

    _cmd_bootstrap(["--resync", "--json"])

    assert _manifest(capsys)["touched"] == []
    assert _tree(repo) == before


def test_known_legacy_block_at_end_of_file_is_replaced(repo: pathlib.Path) -> None:
    """The first kit appended its block unmarked; that exact text is replaceable."""
    _write(repo, "CLAUDE.md", f"# App notes\n\n{_KNOWN_LEGACY_CONNECTOR_REVIEW_BLOCK}")

    assert _cmd_bootstrap(["--connector-review-kit"]) == 0

    assert (repo / "CLAUDE.md").read_text() == f"# App notes\n\n{_BLOCK}"


def test_legacy_block_with_text_after_it_stops_the_named_kit(
    repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only the complete known block may be replaced without a person looking."""
    original = _KNOWN_LEGACY_CONNECTOR_REVIEW_BLOCK + "\n### App notes\nKeep this.\n"
    _write(repo, "CLAUDE.md", original)
    _write(repo, _REMINDER, "#!/usr/bin/env bash\n")

    assert _cmd_bootstrap(["--connector-review-kit"]) == 2

    assert "unmarked connector-review block" in capsys.readouterr().err
    assert (repo / "CLAUDE.md").read_text() == original
    # Asked for by name and refused: nothing at all was written.
    assert (repo / _REMINDER).exists()
    assert not (repo / ".github").exists()


def test_legacy_block_with_text_after_it_skips_the_kit_on_resync(
    repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """One hand-edited CLAUDE.md must not block the rest of a fleet resync."""
    original = _KNOWN_LEGACY_CONNECTOR_REVIEW_BLOCK + "\n### App notes\nKeep this.\n"
    _write(repo, "CLAUDE.md", original)
    _write(repo, _REMINDER, "#!/usr/bin/env bash\n")
    _write(repo, _OLD_GATE_WORKFLOW, "name: Connector Review\n")

    assert _cmd_bootstrap(["--resync"]) == 0

    out = capsys.readouterr().out
    assert "skipped: cannot safely merge" in out
    # The phrase the resync lane matches to tell this skip from a refusal.
    assert "connector review kit" in out
    assert "--connector-review-kit" in out
    # The kit is untouched as a whole: old guidance, old hook, no README.
    assert (repo / "CLAUDE.md").read_text() == original
    assert (repo / _REMINDER).exists()
    assert not (repo / _README_REL).exists()
    # The rest of the run landed, the workflow swap included.
    assert (repo / ".github/workflows" / _WORKFLOW).exists()
    assert not (repo / _OLD_GATE_WORKFLOW).exists()
    assert (repo / ".github/workflows/tests.yaml").exists()


_MALFORMED = {
    "begin-only": f"# Notes\n\n{_BEGIN}\nold text\n",
    "end-only": f"# Notes\n\nold text\n{_END}\n",
    "end-before-begin": f"# Notes\n\n{_END}\nold text\n{_BEGIN}\n",
}


@pytest.mark.parametrize("name", ["CLAUDE.md", "AGENTS.md"])
@pytest.mark.parametrize("shape", sorted(_MALFORMED))
def test_malformed_markers_stop_the_named_kit(
    repo: pathlib.Path, capsys: pytest.CaptureFixture[str], name: str, shape: str
) -> None:
    _write(repo, "CLAUDE.md", "# Notes\n")
    _write(repo, name, _MALFORMED[shape])
    before = _tree(repo)

    assert _cmd_bootstrap(["--connector-review-kit"]) == 2

    err = capsys.readouterr().err
    assert "malformed managed review block" in err
    assert name in err
    assert _tree(repo) == before


@pytest.mark.parametrize("name", ["CLAUDE.md", "AGENTS.md"])
@pytest.mark.parametrize("shape", sorted(_MALFORMED))
def test_malformed_markers_skip_the_whole_kit_on_resync(
    repo: pathlib.Path, capsys: pytest.CaptureFixture[str], name: str, shape: str
) -> None:
    """A bad AGENTS.md holds CLAUDE.md back too: the two never disagree."""
    _write(repo, "CLAUDE.md", "# Notes\n")
    _write(repo, name, _MALFORMED[shape])

    assert _cmd_bootstrap(["--resync"]) == 0

    assert "skipped: cannot safely merge" in capsys.readouterr().out
    assert (repo / name).read_text() == _MALFORMED[shape]
    other = "AGENTS.md" if name == "CLAUDE.md" else "CLAUDE.md"
    if (repo / other).exists():
        assert _BEGIN not in (repo / other).read_text()
    assert not (repo / _README_REL).exists()


def test_agents_md_gets_the_same_block_when_it_exists(repo: pathlib.Path) -> None:
    _write(repo, "CLAUDE.md", "# Claude notes\n")
    _write(repo, "AGENTS.md", "# Agent notes\n")

    assert _cmd_bootstrap(["--resync"]) == 0

    assert (repo / "CLAUDE.md").read_text() == f"# Claude notes\n\n{_BLOCK}"
    assert (repo / "AGENTS.md").read_text() == f"# Agent notes\n\n{_BLOCK}"


def test_agents_md_block_is_replaced_in_place(repo: pathlib.Path) -> None:
    _write(repo, "AGENTS.md", f"# Agent notes\n\n{_BEGIN}\nold\n{_END}\n\n## After\n")

    assert _cmd_bootstrap(["--resync"]) == 0

    assert (repo / "AGENTS.md").read_text() == f"# Agent notes\n\n{_BLOCK}## After\n"


def test_agents_md_legacy_block_is_replaced(repo: pathlib.Path) -> None:
    _write(
        repo, "AGENTS.md", f"# Agent notes\n\n{_KNOWN_LEGACY_CONNECTOR_REVIEW_BLOCK}"
    )

    assert _cmd_bootstrap(["--resync"]) == 0

    assert (repo / "AGENTS.md").read_text() == f"# Agent notes\n\n{_BLOCK}"


def test_agents_md_is_never_created(repo: pathlib.Path) -> None:
    assert _cmd_bootstrap(["--resync"]) == 0
    assert (repo / "CLAUDE.md").exists()
    assert not (repo / "AGENTS.md").exists()


def test_agents_md_symlinked_to_claude_md_gets_one_block(repo: pathlib.Path) -> None:
    """A common layout. Both names are one file, so it must be merged once."""
    _write(repo, "CLAUDE.md", "# Notes\n")
    (repo / "AGENTS.md").symlink_to("CLAUDE.md")

    assert _cmd_bootstrap(["--resync"]) == 0

    assert (repo / "AGENTS.md").is_symlink()
    assert (repo / "CLAUDE.md").read_text() == f"# Notes\n\n{_BLOCK}"


def test_agents_md_symlinked_inside_the_repo_is_merged(repo: pathlib.Path) -> None:
    """A link that stays in the repository is followed and merged."""
    _write(repo, "CLAUDE.md", "# Notes\n")
    _write(repo, "docs/agents.md", "# Agent notes\n")
    (repo / "AGENTS.md").symlink_to("docs/agents.md")

    assert _cmd_bootstrap(["--resync"]) == 0

    assert (repo / "AGENTS.md").is_symlink()
    assert (repo / "docs/agents.md").read_text() == f"# Agent notes\n\n{_BLOCK}"


def test_agents_md_symlinked_outside_the_repo_stops_the_named_kit(
    repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The write would follow the link and replace a file outside the checkout
    (lens F-9430d5), so the kit refuses and writes nothing."""
    outside = _write(repo.parent, "outside.md", "# Someone else's file\n")
    _write(repo, "CLAUDE.md", "# Notes\n")
    (repo / "AGENTS.md").symlink_to(outside)
    before = _tree(repo)

    assert _cmd_bootstrap(["--connector-review-kit"]) == 2

    err = capsys.readouterr().err
    assert "AGENTS.md" in err
    assert "outside the repository" in err
    assert outside.read_text() == "# Someone else's file\n"
    assert _tree(repo) == before


def test_agents_md_symlinked_outside_the_repo_skips_the_kit_on_resync(
    repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    outside = _write(repo.parent, "outside.md", "# Someone else's file\n")
    _write(repo, "CLAUDE.md", "# Notes\n")
    (repo / "AGENTS.md").symlink_to(outside)

    assert _cmd_bootstrap(["--resync"]) == 0

    out = capsys.readouterr().out
    assert "skipped: cannot safely merge" in out
    assert "outside the repository" in out
    assert outside.read_text() == "# Someone else's file\n"
    # The two never disagree: CLAUDE.md is held back with AGENTS.md.
    assert (repo / "CLAUDE.md").read_text() == "# Notes\n"
    assert not (repo / _README_REL).exists()


def test_bare_run_leaves_claude_md_and_agents_md_alone(repo: pathlib.Path) -> None:
    """Only --connector-review-kit, or the --resync that implies it, opts in."""
    _write(repo, "AGENTS.md", "# Agent notes\n")

    assert _cmd_bootstrap([]) == 0

    assert not (repo / "CLAUDE.md").exists()
    assert (repo / "AGENTS.md").read_text() == "# Agent notes\n"
    assert not (repo / ".claude/settings.json").exists()
    assert not (repo / _README_REL).exists()


# ---------------------------------------------------------------------------
# The repository's principles folder
# ---------------------------------------------------------------------------


def test_resync_creates_the_principles_readme(repo: pathlib.Path) -> None:
    assert _cmd_bootstrap(["--resync"]) == 0
    assert (repo / _README_REL).read_text() == render(_README_TEMPLATE)


def test_principles_readme_is_never_overwritten(
    repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The folder is the repository's own; bootstrap only seeds it."""
    _write(repo, _README_REL, "# Ours\n")
    _write(repo, ".github/connector-reviewer/principles/rp01-paging.md", "x\n")

    assert _cmd_bootstrap(["--resync", "--json"]) == 0

    manifest = _manifest(capsys)
    assert (repo / _README_REL).read_text() == "# Ours\n"
    assert _README_REL in manifest["unchanged"]
    assert (
        repo / ".github/connector-reviewer/principles/rp01-paging.md"
    ).read_text() == "x\n"


def test_principles_readme_is_added_beside_existing_principles(
    repo: pathlib.Path,
) -> None:
    _write(repo, ".github/connector-reviewer/principles/rp01-paging.md", "x\n")
    assert _cmd_bootstrap(["--resync"]) == 0
    assert (repo / _README_REL).exists()


# A principle written in the README's format. Invented for this test; it stands
# for the principles app repositories have written for themselves.
_RP01 = (
    "---\n"
    "id: RP01\n"
    "applies-to: [app]\n"
    "---\n"
    "# Widget pages are fetched to the end\n"
    "\n"
    "Principle: The page loop stops only on an empty page.\n"
    "Why: Stopping on a short page silently drops the rest of the catalog.\n"
    "Ask:\n"
    "- Does a changed loop still stop only on an empty page?\n"
    "Don't flag: Retry and timeout settings.\n"
    "Severity: High when a page can be skipped; otherwise medium.\n"
)


def test_resync_leaves_the_repositorys_principles_byte_for_byte(
    repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Bootstrap writes into the principles folder (the README is absent) and
    removes the old local rules folder in the same pass, and still every other
    file in the folder is left exactly as it was and is reported nowhere."""
    principles = ".github/connector-reviewer/principles"
    own = {
        f"{principles}/rp01-widget-paging.md": _RP01,
        f"{principles}/rp02-draft.md": "not in the format yet\n",
        f"{principles}/notes.txt": "kept by hand\n",
    }
    for rel, text in own.items():
        _write(repo, rel, text)
    _seed_local_rules(repo, {"widget.md": _placeholder()})

    assert _cmd_bootstrap(["--resync", "--json"]) == 0

    manifest = _manifest(capsys)
    for rel, text in own.items():
        assert (repo / rel).read_bytes() == text.encode(), rel
        assert rel not in manifest["touched"], rel
        assert rel not in manifest["unchanged"], rel
    assert (repo / _README_REL).read_text() == render(_README_TEMPLATE)
    assert _README_REL in manifest["touched"]
    # The old rules folder did go, so the removal ran in this pass.
    assert not (repo / LOCAL_REVIEW_RULES_DIR).exists()
    # Nothing else appeared in the folder.
    assert sorted(
        p.relative_to(repo).as_posix() for p in (repo / principles).iterdir()
    ) == sorted([*own, _README_REL])


def test_nothing_bootstrap_manages_or_retires_is_a_principle(
    tmp_path: pathlib.Path,
) -> None:
    """Bootstrap's only path in the principles folder is the README: no list it
    writes, retires or drift-checks names anything else there."""
    principles = ".github/connector-reviewer/principles/"
    paths = [
        *(f".github/workflows/{name}" for name in MANAGED_WORKFLOWS),
        *(dest for dest, _ in MANAGED_ACTION_FILES),
        *RETIRED_FILES,
        *RETIRED_ACTION_FILES,
        *RETIRED_CONNECTOR_REVIEW_FILES,
        *(path.relative_to(tmp_path).as_posix() for path in discover(tmp_path)),
    ]
    assert not [p for p in paths if p.startswith(principles)]
    assert _README_REL.startswith(principles)
    assert LOCAL_REVIEW_RULES_DIR.split("/")[0] == ".mothership"


def test_resync_is_a_no_op_for_the_shim_already_in_a_repository(
    repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Some repositories got the shim by hand, before bootstrap shipped it. A
    resync there must not rewrite it, and C002 must not report it."""
    shim = f".github/workflows/{_WORKFLOW}"
    text = render(_WORKFLOW)
    _write(repo, shim, text)

    assert _cmd_bootstrap(["--resync", "--json"]) == 0

    manifest = _manifest(capsys)
    assert shim in manifest["unchanged"]
    assert shim not in manifest["touched"]
    assert (repo / shim).read_text() == text
    assert not [m for m in _c002(repo) if _WORKFLOW in m]


def test_principles_readme_says_what_goes_in_the_folder() -> None:
    readme = render(_README_TEMPLATE)
    for phrase in (
        "`rp01-<slug>.md`",
        "`RP01`",
        "---\nid: RP01\napplies-to: [app, contract]\n---\n",
        "# One sentence naming what must stay true",
        "Principle: <one or two sentences>",
        "Why: <the failure this prevents>",
        "Ask:\n- <a question the reviewer should answer about the diff; at most three>",
        "Don't flag: <what CI already enforces, or what is out of scope>",
        "Severity: <what makes it high; otherwise medium>",
        "https://github.com/atlanhq/application-sdk/tree/main/.github/"
        "connector-reviewer/principles",
        "`rpNN-*.md`",
    ):
        assert phrase in readme, phrase
    for area in ("`app`", "`generated`", "`tests`", "`contract`", "`ci`"):
        assert f"- {area}:" in readme


# ---------------------------------------------------------------------------
# Retiring the kit's plain files
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rel", RETIRED_CONNECTOR_REVIEW_FILES)
def test_resync_removes_a_retired_kit_file(
    repo: pathlib.Path, capsys: pytest.CaptureFixture[str], rel: str
) -> None:
    _write(repo, rel, "legacy\n")

    assert _cmd_bootstrap(["--resync", "--json"]) == 0

    assert not (repo / rel).exists()
    # The resync lane stages exactly the manifest's touched paths.
    assert rel in _manifest(capsys)["touched"]


@pytest.mark.parametrize("rel", RETIRED_CONNECTOR_REVIEW_FILES)
def test_bare_run_leaves_a_retired_kit_file(repo: pathlib.Path, rel: str) -> None:
    """A bare run never changes local Claude configuration.

    It matters for the reminder: its hook entry is only removed with the kit,
    so deleting the script here would leave a hook pointing at nothing.
    """
    _write(repo, rel, "legacy\n")
    assert _cmd_bootstrap([]) == 0
    assert (repo / rel).read_text() == "legacy\n"


def test_absent_kit_files_are_not_reported(
    repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _cmd_bootstrap(["--resync", "--json"]) == 0
    manifest = _manifest(capsys)
    for rel in (*RETIRED_CONNECTOR_REVIEW_FILES, ".claude/settings.json"):
        assert rel not in manifest["touched"]
        assert rel not in manifest["unchanged"]


def test_removing_the_kit_leaves_no_empty_directories(repo: pathlib.Path) -> None:
    for rel in RETIRED_CONNECTOR_REVIEW_FILES:
        _write(repo, rel, "legacy\n")
    _write(repo, "scripts/keep.sh", "echo keep\n")

    assert _cmd_bootstrap(["--resync"]) == 0

    assert not (repo / ".claude/hooks").exists()
    assert not (repo / ".claude/skills/connector-review").exists()
    assert not (repo / ".mothership").exists()
    # A directory that still holds something of the repository's stays.
    assert (repo / "scripts/keep.sh").read_text() == "echo keep\n"
    assert (repo / ".claude/skills/remediate/SKILL.md").exists()


# ---------------------------------------------------------------------------
# .claude/settings.json: only the reminder's hook entry goes
# ---------------------------------------------------------------------------


def _settings(repo: pathlib.Path) -> dict:
    return json.loads((repo / ".claude/settings.json").read_text())


def _seed_settings(repo: pathlib.Path, settings: object) -> str:
    text = json.dumps(settings, indent=2) + "\n"
    _write(repo, ".claude/settings.json", text)
    return text


def test_reminder_entry_is_removed_and_every_other_setting_kept(
    repo: pathlib.Path,
) -> None:
    other_hook = {"hooks": [{"type": "command", "command": "scripts/hello.sh"}]}
    _seed_settings(
        repo,
        {
            "permissions": {"allow": ["Bash(uv run pytest:*)"]},
            "env": {"FOO": "bar"},
            "hooks": {
                "SessionStart": [other_hook, _REMINDER_ENTRY],
                "PreToolUse": [{"matcher": "Bash", "hooks": []}],
            },
        },
    )

    assert _cmd_bootstrap(["--resync"]) == 0

    assert _settings(repo) == {
        "permissions": {"allow": ["Bash(uv run pytest:*)"]},
        "env": {"FOO": "bar"},
        "hooks": {
            "SessionStart": [other_hook],
            "PreToolUse": [{"matcher": "Bash", "hooks": []}],
        },
    }


def test_reminder_is_removed_from_a_group_that_runs_something_else(
    repo: pathlib.Path,
) -> None:
    keep = {"type": "command", "command": "scripts/hello.sh"}
    _seed_settings(
        repo,
        {
            "hooks": {
                "SessionStart": [
                    {"matcher": "startup", "hooks": [keep, *_REMINDER_ENTRY["hooks"]]}
                ]
            }
        },
    )

    assert _cmd_bootstrap(["--resync"]) == 0

    assert _settings(repo) == {
        "hooks": {"SessionStart": [{"matcher": "startup", "hooks": [keep]}]}
    }


def test_empty_hooks_structure_bootstrap_created_is_cleaned_up(
    repo: pathlib.Path,
) -> None:
    """The kit added `hooks.SessionStart` for its entry; it takes that back."""
    _seed_settings(
        repo,
        {"model": "some-model", "hooks": {"SessionStart": [_REMINDER_ENTRY]}},
    )

    assert _cmd_bootstrap(["--resync"]) == 0

    assert _settings(repo) == {"model": "some-model"}


def test_settings_file_bootstrap_created_is_removed(
    repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A file that held nothing but the kit's hook exists only because of it."""
    _seed_settings(repo, {"hooks": {"SessionStart": [_REMINDER_ENTRY]}})

    assert _cmd_bootstrap(["--resync", "--json"]) == 0

    assert not (repo / ".claude/settings.json").exists()
    assert ".claude/settings.json" in _manifest(capsys)["touched"]


def test_reminder_is_recognised_with_a_project_directory_prefix(
    repo: pathlib.Path,
) -> None:
    entry = {
        "hooks": [{"type": "command", "command": f'"$CLAUDE_PROJECT_DIR"/{_REMINDER}'}]
    }
    _seed_settings(repo, {"env": {"A": "1"}, "hooks": {"SessionStart": [entry]}})

    assert _cmd_bootstrap(["--resync"]) == 0

    assert _settings(repo) == {"env": {"A": "1"}}


def test_older_kit_hooks_are_still_removed(repo: pathlib.Path) -> None:
    """The first kit's gate hooks, for a repository that skipped a version."""
    _seed_settings(
        repo,
        {
            "hooks": {
                "SessionStart": [
                    {"matcher": "keep-me"},
                    {
                        "hooks": [
                            {
                                "command": '"$CLAUDE_PROJECT_DIR"/'
                                ".claude/hooks/review-rules-freshness.sh"
                            }
                        ]
                    },
                ],
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {
                                "command": '"$CLAUDE_PROJECT_DIR"/'
                                ".claude/hooks/check-review-before-commit.sh"
                            },
                            {"command": "scripts/keep-me.sh"},
                        ],
                    }
                ],
            }
        },
    )

    assert _cmd_bootstrap(["--resync"]) == 0

    assert _settings(repo) == {
        "hooks": {
            "SessionStart": [{"matcher": "keep-me"}],
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"command": "scripts/keep-me.sh"}]}
            ],
        }
    }


@pytest.mark.parametrize(
    "settings",
    [
        {"env": {"FOO": "bar"}},
        {"hooks": {}},
        {"hooks": {"SessionStart": []}},
        {"hooks": {"SessionStart": [{"hooks": [{"command": "scripts/hello.sh"}]}]}},
        {"hooks": "not-an-object"},
        ["not", "an", "object"],
    ],
)
def test_settings_without_the_kit_hook_are_left_byte_for_byte(
    repo: pathlib.Path, capsys: pytest.CaptureFixture[str], settings: object
) -> None:
    """Nothing to remove means nothing to rewrite, odd shapes and all."""
    # Four-space indent: a rewrite would be visible even with equal content.
    text = json.dumps(settings, indent=4) + "\n"
    _write(repo, ".claude/settings.json", text)

    assert _cmd_bootstrap(["--resync", "--json"]) == 0

    assert (repo / ".claude/settings.json").read_text() == text
    assert ".claude/settings.json" not in _manifest(capsys)["touched"]


def test_unparseable_settings_skip_the_kit_and_keep_the_reminder(
    repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The file may still name the script, so the script must not go."""
    broken = '{"hooks": {"SessionStart": [  // a comment\n'
    _write(repo, ".claude/settings.json", broken)
    _write(repo, _REMINDER, "#!/usr/bin/env bash\n")

    assert _cmd_bootstrap(["--resync"]) == 0

    out = capsys.readouterr().out
    assert "skipped: cannot safely merge" in out
    assert "settings.json" in out
    assert (repo / ".claude/settings.json").read_text() == broken
    assert (repo / _REMINDER).exists()
    assert not (repo / "CLAUDE.md").exists()


def test_resync_no_longer_adds_the_reminder_or_a_settings_file(
    repo: pathlib.Path,
) -> None:
    assert _cmd_bootstrap(["--resync"]) == 0
    assert not (repo / ".claude/settings.json").exists()
    assert not (repo / _REMINDER).exists()
    assert not (repo / "scripts").exists()


def test_resync_leaves_gitignore_to_the_repository(repo: pathlib.Path) -> None:
    """The kit used to append the rule-cache line. Nothing writes that cache
    now, so the kit no longer adds the line, and never removes one either: a
    local cache directory would then show up as untracked files."""
    _write(repo, ".gitignore", "custom-cache/\n.mothership/.cache/\n")
    assert _cmd_bootstrap(["--resync"]) == 0
    assert (repo / ".gitignore").read_text() == "custom-cache/\n.mothership/.cache/\n"

    (repo / ".gitignore").write_text("custom-cache/\n")
    assert _cmd_bootstrap(["--resync"]) == 0
    assert (repo / ".gitignore").read_text() == "custom-cache/\n"


# ---------------------------------------------------------------------------
# Local review rules: the whole folder goes, real rules included
# ---------------------------------------------------------------------------


def _files_under(folder: pathlib.Path, root: pathlib.Path) -> list[str]:
    """Every non-directory under *folder*, links included, repo-relative.

    Walked without following links, so a link to a directory is one entry.
    """
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(folder):
        here = pathlib.Path(dirpath)
        links = [name for name in dirnames if (here / name).is_symlink()]
        for name in [*filenames, *links]:
            found.append((here / name).relative_to(root).as_posix())
    return sorted(found)


def _assert_folder_removed(
    repo: pathlib.Path, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    """Run bootstrap with *argv* and check the folder went, file by file."""
    files = _files_under(repo / LOCAL_REVIEW_RULES_DIR, repo)
    assert files

    assert _cmd_bootstrap([*argv, "--json"]) == 0

    out = capsys.readouterr().out
    touched = json.loads(out.strip().splitlines()[-1])["touched"]
    for rel in files:
        assert rel in touched, rel
        assert f"removed: {repo / rel}  (retired file)" in out, rel
    assert not (repo / ".mothership").exists()
    assert "kept:" not in out
    assert not any(line.startswith("skipped:") for line in out.splitlines())


@pytest.mark.parametrize(
    "rules",
    [
        pytest.param({"widget-repo-001.md": _placeholder()}, id="placeholder"),
        pytest.param({"widget-paging-001.md": _REAL_RULE}, id="real-rule"),
        pytest.param(
            {"widget-repo-001.md": _placeholder(), "widget-paging-001.md": _REAL_RULE},
            id="real-rule-beside-placeholder",
        ),
        pytest.param(
            {"widget-repo-001.md": _placeholder() + "- Widget ids are cased.\n"},
            id="reworded-placeholder",
        ),
    ],
)
def test_resync_removes_the_whole_rules_folder(
    repo: pathlib.Path, capsys: pytest.CaptureFixture[str], rules: dict[str, str]
) -> None:
    """Every repository has principles now: no rule in the folder is kept."""
    _seed_local_rules(repo, rules)
    _assert_folder_removed(repo, capsys, ["--resync"])


def test_any_other_file_in_the_rules_folder_goes_too(
    repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_local_rules(
        repo,
        {"widget-paging-001.md": _REAL_RULE},
        manifest="schema: 2\nowner: widget-team\nrules:\n  - rules/gone.md\n",
    )
    _write(repo, f"{LOCAL_REVIEW_RULES_DIR}/NOTES.md", "why we have these\n")
    _write(repo, f"{LOCAL_REVIEW_RULES_DIR}/rules/drafts/next.md", "draft\n")
    _write(repo, f"{LOCAL_REVIEW_RULES_DIR}/fixtures/sample.json", "{}\n")
    _assert_folder_removed(repo, capsys, ["--resync"])


def test_manifest_alone_is_removed(
    repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(repo, f"{LOCAL_REVIEW_RULES_DIR}/MANIFEST.yaml", "schema: 2\nrules: []\n")
    _assert_folder_removed(repo, capsys, ["--resync"])


def test_connector_review_kit_alone_removes_the_folder(
    repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_local_rules(repo, {"widget-paging-001.md": _REAL_RULE})
    _assert_folder_removed(repo, capsys, ["--connector-review-kit"])


def test_other_mothership_content_is_kept(
    repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_local_rules(repo, {"widget-paging-001.md": _REAL_RULE})
    others = {
        ".mothership/review.yaml": "ours: true\n",
        ".mothership/.cache/review-rulesets/platform/x.md": "cached\n",
        ".mothership/review-rulesets/other/rules/x.md": "ours\n",
        ".mothership/review-rulesets/README.md": "kept\n",
    }
    for rel, text in others.items():
        _write(repo, rel, text)

    assert _cmd_bootstrap(["--resync", "--json"]) == 0

    touched = _manifest(capsys)["touched"]
    assert not (repo / LOCAL_REVIEW_RULES_DIR).exists()
    for rel, text in others.items():
        assert (repo / rel).read_text() == text, rel
        assert rel not in touched, rel
    assert all(
        rel.startswith(f"{LOCAL_REVIEW_RULES_DIR}/")
        for rel in touched
        if rel.startswith(".mothership/")
    )


def test_link_to_a_file_outside_the_repo_is_removed_not_followed(
    repo: pathlib.Path, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    outside = _write(tmp_path, "outside/shared-rule.md", "not the repo's\n")
    folder = _seed_local_rules(repo, {"widget-paging-001.md": _REAL_RULE})
    link = folder / "rules" / "shared-rule.md"
    link.symlink_to(outside)

    _assert_folder_removed(repo, capsys, ["--resync"])

    assert not link.is_symlink()
    assert outside.read_text() == "not the repo's\n"


def test_link_to_a_directory_outside_the_repo_is_not_walked(
    repo: pathlib.Path, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    outside = tmp_path / "outside-rules"
    _write(outside, "a.md", "a\n")
    _write(outside, "nested/b.md", "b\n")
    before = _tree(outside)
    folder = _seed_local_rules(repo, {"widget-paging-001.md": _REAL_RULE})
    link = folder / "rules" / "shared"
    link.symlink_to(outside, target_is_directory=True)

    _assert_folder_removed(repo, capsys, ["--resync"])

    assert not link.is_symlink()
    assert _tree(outside) == before


def test_dangling_link_in_the_folder_is_removed(
    repo: pathlib.Path, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = _seed_local_rules(repo, {"widget-paging-001.md": _REAL_RULE})
    (folder / "rules" / "gone.md").symlink_to(tmp_path / "missing.md")
    _assert_folder_removed(repo, capsys, ["--resync"])


def test_folder_that_is_a_link_inside_the_repo_goes_as_the_link_alone(
    repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = repo / "docs" / "review-rules"
    _write(target, "rules/x.md", "the repo's docs\n")
    folder = repo / LOCAL_REVIEW_RULES_DIR
    folder.parent.mkdir(parents=True)
    folder.symlink_to(target, target_is_directory=True)

    assert _cmd_bootstrap(["--resync", "--json"]) == 0

    assert LOCAL_REVIEW_RULES_DIR in _manifest(capsys)["touched"]
    assert not folder.is_symlink()
    assert not (repo / ".mothership").exists()
    assert (target / "rules" / "x.md").read_text() == "the repo's docs\n"


def _link_outside(
    repo: pathlib.Path, tmp_path: pathlib.Path, link_rel: str
) -> pathlib.Path:
    """Make *link_rel* a link to a directory outside *repo* holding the rules."""
    outside = tmp_path / "outside"
    below = LOCAL_REVIEW_RULES_DIR[len(link_rel) :].lstrip("/")
    rules = outside / below if below else outside
    _write(rules, "MANIFEST.yaml", "schema: 2\nrules:\n  - rules/x.md\n")
    _write(rules, "rules/x.md", _REAL_RULE)
    link = repo / link_rel
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside, target_is_directory=True)
    return outside


_OUTSIDE_LINKS = pytest.mark.parametrize(
    "link_rel",
    [LOCAL_REVIEW_RULES_DIR, ".mothership/review-rulesets", ".mothership"],
)


@_OUTSIDE_LINKS
def test_resync_skips_the_kit_when_the_folder_links_outside_the_repo(
    repo: pathlib.Path,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    link_rel: str,
) -> None:
    """Like an AGENTS.md linked outside the repository: the kit is skipped
    whole, so the old kit stays together, and the rest of the resync lands."""
    outside = _link_outside(repo, tmp_path, link_rel)
    before = _tree(outside)
    _write(repo, _REMINDER, "legacy\n")

    assert _cmd_bootstrap(["--resync"]) == 0

    out = capsys.readouterr().out
    skipped = [line for line in out.splitlines() if line.startswith("skipped:")]
    assert len(skipped) == 1
    assert "outside the repository" in skipped[0]
    assert "connector review kit was left untouched" in skipped[0]
    assert (repo / link_rel).is_symlink()
    assert _tree(outside) == before
    assert (repo / _REMINDER).read_text() == "legacy\n"
    assert not (repo / "CLAUDE.md").exists()
    assert (repo / ".github/workflows" / _WORKFLOW).exists()


@_OUTSIDE_LINKS
def test_connector_review_kit_refuses_a_folder_linked_outside_the_repo(
    repo: pathlib.Path,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    link_rel: str,
) -> None:
    outside = _link_outside(repo, tmp_path, link_rel)
    before = _tree(outside)

    assert _cmd_bootstrap(["--connector-review-kit"]) == 2

    assert "outside the repository" in capsys.readouterr().err
    assert _tree(outside) == before
    assert not (repo / "CLAUDE.md").exists()
    assert not (repo / ".github").exists()


def test_no_local_rules_folder_is_silent(
    repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _cmd_bootstrap(["--resync", "--json"]) == 0
    out = capsys.readouterr().out
    assert ".mothership" not in out


def test_empty_directories_left_by_a_checkout_are_removed_silently(
    repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / LOCAL_REVIEW_RULES_DIR / "rules").mkdir(parents=True)
    assert _cmd_bootstrap(["--resync", "--json"]) == 0
    out = capsys.readouterr().out
    assert not (repo / ".mothership").exists()
    assert "removed:" not in out
    manifest = json.loads(out.strip().splitlines()[-1])
    assert not [p for p in manifest["touched"] if p.startswith(".mothership")]


def test_bare_run_leaves_the_rules_folder(repo: pathlib.Path) -> None:
    folder = _seed_local_rules(repo, {"widget-paging-001.md": _REAL_RULE})
    before = _tree(folder)
    assert _cmd_bootstrap([]) == 0
    assert _tree(folder) == before


def test_resync_inside_application_sdk_touches_none_of_this(
    repo: pathlib.Path,
) -> None:
    """application-sdk keeps the shared rule sets under the same folder.

    Bootstrap is a no-op in its own repository, so a resync run there by
    mistake must not delete them, or write a reviewer workflow or a block.
    """
    _write(
        repo,
        "packages/conformance/pyproject.toml",
        '[project]\nname = "atlan-application-sdk-conformance"\nversion = "0"\n',
    )
    _seed_old_kit(repo)
    _seed_local_rules(repo, {"widget-repo-001.md": _placeholder()})
    before = _tree(repo)

    assert _cmd_bootstrap(["--resync"]) == 0

    assert _tree(repo) == before


# ---------------------------------------------------------------------------
# Usage text
# ---------------------------------------------------------------------------


def test_usage_describes_the_new_kit_not_the_old_one() -> None:
    assert "--connector-review-kit" in BOOTSTRAP_USAGE
    assert ".github/connector-reviewer/principles/" in BOOTSTRAP_USAGE
    assert "AGENTS.md" in BOOTSTRAP_USAGE
    assert not re.search(r"\bL[1-4]\b", BOOTSTRAP_USAGE)


# ---------------------------------------------------------------------------
# End to end: one resync moves a repository from the old reviewer to the new
# ---------------------------------------------------------------------------


def _seed_old_kit(root: pathlib.Path) -> None:
    """A repository as the mothership kit left it."""
    for rel in _MOTHERSHIP_KIT_FILES:
        _write(root, rel, "legacy\n")
    _seed_settings(
        root,
        {
            "permissions": {"allow": ["Bash(uv run pytest:*)"]},
            "hooks": {"SessionStart": [_REMINDER_ENTRY]},
        },
    )
    _write(
        root,
        "CLAUDE.md",
        "# Widget app\n\nNotes the team wrote.\n\n"
        f"{_BEGIN}\n"
        "## Mandatory pre-commit review (L2-L4 rules; L1 before the PR and in PR"
        " CI)\n\n"
        "Before any `git commit`, run the `connector-review` skill for the"
        " applicable\nL2-L4 rules.\n\n"
        "Shared L2/L4 rules are fetched into `.mothership/.cache/review-rulesets/`"
        " and\nL3 rules live in `.mothership/review-rulesets/connector-app/`.\n"
        f"{_END}\n",
    )
    _write(root, ".gitignore", "custom-cache/\n.mothership/.cache/\n")


@pytest.fixture
def placeholder_repo(repo: pathlib.Path) -> pathlib.Path:
    _seed_old_kit(repo)
    _seed_local_rules(repo, {"widget-repo-001.md": _placeholder()})
    return repo


@pytest.fixture
def real_rule_repo(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> pathlib.Path:
    root = tmp_path / "atlan-gadget-app"
    root.mkdir()
    _seed_old_kit(root)
    _seed_local_rules(
        root,
        {
            "gadget-repo-001.md": _placeholder("Gadget", "GADGET-REPO-001"),
            "gadget-paging-001.md": _REAL_RULE,
        },
    )
    _write(root, "AGENTS.md", "# Agent notes\n")
    monkeypatch.chdir(root)
    return root


def _assert_moved_to_the_connector_reviewer(root: pathlib.Path) -> None:
    # In: the workflow, the block, the principles README.
    assert (root / ".github/workflows" / _WORKFLOW).read_text() == render(_WORKFLOW)
    assert (root / _README_REL).read_text() == render(_README_TEMPLATE)
    claude = (root / "CLAUDE.md").read_text()
    assert claude == f"# Widget app\n\nNotes the team wrote.\n\n{_BLOCK}"

    # Out: every plain file of the old kit, and its hook entry.
    for rel in _MOTHERSHIP_KIT_FILES:
        assert not (root / rel).exists(), rel
    assert json.loads((root / ".claude/settings.json").read_text()) == {
        "permissions": {"allow": ["Bash(uv run pytest:*)"]}
    }

    # Nothing written into the repository speaks the old reviewer's language.
    written = {
        rel: text
        for rel, text in _tree(root).items()
        if rel != ".gitignore" and not rel.startswith(LOCAL_REVIEW_RULES_DIR)
    }
    for rel, text in written.items():
        assert not _RETIRED_VOCABULARY.search(text), rel

    assert _c002(root) == []


def test_resync_moves_a_placeholder_only_repo_to_the_connector_reviewer(
    placeholder_repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _cmd_bootstrap(["--resync", "--json"]) == 0
    first = _manifest(capsys)

    _assert_moved_to_the_connector_reviewer(placeholder_repo)
    assert not (placeholder_repo / ".mothership").exists()
    assert not (placeholder_repo / "AGENTS.md").exists()
    # The resync lane commits exactly what the manifest names.
    for rel in (
        *_MOTHERSHIP_KIT_FILES,
        f"{LOCAL_REVIEW_RULES_DIR}/rules/widget-repo-001.md",
        f"{LOCAL_REVIEW_RULES_DIR}/MANIFEST.yaml",
        f".github/workflows/{_WORKFLOW}",
        _README_REL,
        "CLAUDE.md",
        ".claude/settings.json",
    ):
        assert rel in first["touched"], rel

    # And it is a fixpoint: a second resync changes nothing.
    before = _tree(placeholder_repo)
    assert _cmd_bootstrap(["--resync", "--json"]) == 0
    assert _manifest(capsys)["touched"] == []
    assert _tree(placeholder_repo) == before


def test_resync_moves_a_repo_with_a_real_rule_and_removes_its_rules_folder(
    real_rule_repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _cmd_bootstrap(["--resync", "--json"]) == 0
    out = capsys.readouterr().out
    touched = json.loads(out.strip().splitlines()[-1])["touched"]

    _assert_moved_to_the_connector_reviewer(real_rule_repo)
    # The real rule goes with the placeholder and the manifest.
    assert not (real_rule_repo / ".mothership").exists()
    for name in ("rules/gadget-repo-001.md", "rules/gadget-paging-001.md"):
        assert f"{LOCAL_REVIEW_RULES_DIR}/{name}" in touched, name
    assert f"{LOCAL_REVIEW_RULES_DIR}/MANIFEST.yaml" in touched
    assert "kept:" not in out
    # AGENTS.md existed, so it carries the same block.
    assert (real_rule_repo / "AGENTS.md").read_text() == f"# Agent notes\n\n{_BLOCK}"

    before = _tree(real_rule_repo)
    assert _cmd_bootstrap(["--resync", "--json"]) == 0
    assert _manifest(capsys)["touched"] == []
    assert _tree(real_rule_repo) == before


def test_c002_reports_the_old_gate_until_bootstrap_runs(
    placeholder_repo: pathlib.Path,
) -> None:
    """Before the run C002 names both retired gate files; after, nothing."""
    _cmd_bootstrap([])  # everything else in place, then put the gate back
    _write(placeholder_repo, _OLD_GATE_WORKFLOW, "name: Connector Review\n")
    _write(placeholder_repo, _OLD_GATE_SCRIPT, "print('gate')\n")

    messages = _c002(placeholder_repo)
    assert len(messages) == 2
    assert all("retired" in message for message in messages)

    _cmd_bootstrap([])
    assert _c002(placeholder_repo) == []
