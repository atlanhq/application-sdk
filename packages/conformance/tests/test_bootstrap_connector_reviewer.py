"""Bootstrap installs the connector reviewer and retires the mothership kit (FND-5317).

One resync has to move a repository from the old reviewer to the new one:

- in: the ``connector-reviewer.yaml`` workflow, the managed review block in
  ``CLAUDE.md`` (and ``AGENTS.md`` where one exists), and a README in the
  repository's own principles folder;
- out: the review gate workflow and its script, the rule-fetch script, the
  session reminder and its hook entry, the local review skill,
  ``.mothership/reviewer.yaml``, and local review rules that are only the
  generic placeholder.

A folder of real local rules is knowledge about that connector. It is never
deleted: those rules are moved to principles by hand.
"""

from __future__ import annotations

import json
import pathlib
import re
from importlib.resources import files

import pytest
import yaml

from conformance.bootstrap.args import BOOTSTRAP_USAGE
from conformance.bootstrap.command import _KNOWN_LEGACY_CONNECTOR_REVIEW_BLOCK
from conformance.bootstrap.local_rules import (
    is_placeholder_rule,
    placeholder_rule_files,
)
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
        "## Mandatory before opening a pull request: conformance on your change\n"
        in _BLOCK
    )
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
        "put it on the next line; the reviewer does not read it.",
        # dismiss/recheck/waive are read from the whole comment, so a note on
        # the next line must not start with one of them.
        "Do not start that note with `dismiss`, `recheck` or `waive`.",
        "Add `verify-budget` after it",
    ):
        assert phrase in flat, phrase


def test_block_explains_contested_findings_and_the_status() -> None:
    """A contested finding is medium: it no longer blocks merge, but the status
    stays red until every finding of any severity is closed."""
    flat = _flat(_BLOCK)
    for phrase in (
        'A finding marked "⚖️ contested" was raised as high',
        "It is now medium, so it does not block merge and the author may dismiss it.",
        "The status is green only when every finding, of any severity,",
        "Each finding names the principle it cites.",
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
    retires the old placeholder rules in the same pass, and still every other
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
    # The placeholder rules did go, so the retire path ran in this pass.
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
# Local review rules: the placeholder goes, a real rule keeps the folder
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "rule_id"),
    [
        ("Widget", "WIDGET-REPO-001"),
        ("OpenWidget", "OPENWIDGET-REPO-001"),
        ("Widget Cloud Lineage", "WIDGET-CLOUD-LINEAGE-REPO-001"),
        ("Widget ERP", "WIDGET_ERP-REPO-012"),
    ],
)
def test_placeholder_is_recognised_whatever_the_product(
    name: str, rule_id: str
) -> None:
    assert is_placeholder_rule(_placeholder(name, rule_id))


def test_placeholder_is_recognised_whatever_the_whitespace() -> None:
    text = _placeholder()
    assert is_placeholder_rule(text.rstrip("\n"))
    assert is_placeholder_rule(text.replace("\n", "\r\n"))
    assert is_placeholder_rule(text + "\n\n")
    # Re-wrapped by a formatter: the same words on different lines.
    assert is_placeholder_rule(text.replace(" contract\n  shown", " contract shown"))


@pytest.mark.parametrize(
    "text",
    [
        _REAL_RULE,
        "",
        "# Review Widget-specific behavior\n",
        # A sentence added to the generic text.
        _placeholder() + "- Widget ids are case-sensitive.\n",
        # A word changed in the generic text.
        _placeholder().replace("lineage", "profiling"),
        # A field changed in the front matter.
        _placeholder().replace("severity: HIGH", "severity: LOW"),
        _placeholder().replace("globs: []", "globs: ['app/**']"),
        # Renamed by hand: the id no longer has the placeholder's shape.
        _placeholder(rule_id="WIDGET-IDENTITY-001"),
        # A different title.
        _placeholder().replace("# Review Widget-specific behavior", "# Widget ids"),
        # Two rules in one file.
        _placeholder() + _placeholder(),
    ],
)
def test_anything_else_is_a_real_rule(text: str) -> None:
    assert not is_placeholder_rule(text)


def test_placeholder_only_folder_is_removed(
    repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_local_rules(repo, {"widget-repo-001.md": _placeholder()})

    assert _cmd_bootstrap(["--resync", "--json"]) == 0

    touched = _manifest(capsys)["touched"]
    assert f"{LOCAL_REVIEW_RULES_DIR}/rules/widget-repo-001.md" in touched
    assert f"{LOCAL_REVIEW_RULES_DIR}/MANIFEST.yaml" in touched
    assert not (repo / ".mothership").exists()


def test_placeholder_rules_without_a_manifest_are_removed(repo: pathlib.Path) -> None:
    _write(repo, f"{LOCAL_REVIEW_RULES_DIR}/rules/widget-repo-001.md", _placeholder())
    assert _cmd_bootstrap(["--resync"]) == 0
    assert not (repo / ".mothership").exists()


def test_other_rulesets_beside_the_placeholder_are_left(repo: pathlib.Path) -> None:
    _seed_local_rules(repo, {"widget-repo-001.md": _placeholder()})
    _write(repo, ".mothership/review-rulesets/other/rules/x.md", "ours\n")

    assert _cmd_bootstrap(["--resync"]) == 0

    assert not (repo / LOCAL_REVIEW_RULES_DIR).exists()
    assert (repo / ".mothership/review-rulesets/other/rules/x.md").read_text() == (
        "ours\n"
    )


def _assert_folder_kept(
    repo: pathlib.Path, capsys: pytest.CaptureFixture[str], reason: str
) -> None:
    folder = repo / LOCAL_REVIEW_RULES_DIR
    before = _tree(folder)
    assert before

    assert _cmd_bootstrap(["--resync", "--json"]) == 0

    out = capsys.readouterr().out
    assert _tree(folder) == before
    kept = [line for line in out.splitlines() if line.startswith("kept: ")]
    assert len(kept) == 1
    assert str(folder) in kept[0]
    assert reason in kept[0]
    assert "by hand" in kept[0]
    # Not a refusal: the resync lane holds a repository on a `skipped:` line.
    assert not any(line.startswith("skipped:") for line in out.splitlines())
    manifest = json.loads(out.strip().splitlines()[-1])
    assert not [p for p in manifest["touched"] if p.startswith(".mothership/review")]


def test_folder_with_a_real_rule_is_left_whole(
    repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_local_rules(repo, {"widget-paging-001.md": _REAL_RULE})
    _assert_folder_kept(repo, capsys, "widget-paging-001.md")


def test_real_rule_beside_a_placeholder_keeps_both(
    repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The placeholder is not picked out: the folder is moved as one."""
    _seed_local_rules(
        repo,
        {"widget-repo-001.md": _placeholder(), "widget-paging-001.md": _REAL_RULE},
    )
    _assert_folder_kept(repo, capsys, "widget-paging-001.md")


def test_reworded_placeholder_keeps_the_folder(
    repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_local_rules(
        repo,
        {"widget-repo-001.md": _placeholder() + "- Widget ids are case-sensitive.\n"},
    )
    _assert_folder_kept(repo, capsys, "widget-repo-001.md")


def test_manifest_with_more_than_the_rule_list_keeps_the_folder(
    repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_local_rules(
        repo,
        {"widget-repo-001.md": _placeholder()},
        manifest="schema: 2\nowner: widget-team\nrules:\n  - rules/widget-repo-001.md\n",
    )
    _assert_folder_kept(repo, capsys, "MANIFEST.yaml")


def test_manifest_listing_a_rule_not_on_disk_keeps_the_folder(
    repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_local_rules(
        repo,
        {"widget-repo-001.md": _placeholder()},
        manifest=(
            "schema: 2\nrules:\n  - rules/widget-repo-001.md\n  - rules/gone.md\n"
        ),
    )
    _assert_folder_kept(repo, capsys, "MANIFEST.yaml")


def test_unrecognised_file_keeps_the_folder(
    repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_local_rules(repo, {"widget-repo-001.md": _placeholder()})
    _write(repo, f"{LOCAL_REVIEW_RULES_DIR}/NOTES.md", "why we have these\n")
    _assert_folder_kept(repo, capsys, "NOTES.md")


def test_manifest_without_any_rule_keeps_the_folder(
    repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(repo, f"{LOCAL_REVIEW_RULES_DIR}/MANIFEST.yaml", "schema: 2\nrules: []\n")
    _assert_folder_kept(repo, capsys, "no rule file")


def test_no_local_rules_folder_is_silent(
    repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert placeholder_rule_files(repo) == ([], "")
    (repo / LOCAL_REVIEW_RULES_DIR / "rules").mkdir(parents=True)
    assert placeholder_rule_files(repo) == ([], "")
    assert _cmd_bootstrap(["--resync"]) == 0
    assert "kept:" not in capsys.readouterr().out


def test_bare_run_leaves_placeholder_rules(repo: pathlib.Path) -> None:
    folder = _seed_local_rules(repo, {"widget-repo-001.md": _placeholder()})
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


def test_resync_moves_a_repo_with_a_real_rule_and_keeps_its_rules_folder(
    real_rule_repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = real_rule_repo / LOCAL_REVIEW_RULES_DIR
    rules_before = _tree(folder)

    assert _cmd_bootstrap(["--resync", "--json"]) == 0
    out = capsys.readouterr().out

    _assert_moved_to_the_connector_reviewer(real_rule_repo)
    # The folder with a real rule is untouched, placeholder and manifest included.
    assert _tree(folder) == rules_before
    assert f"kept: {folder}" in out
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
