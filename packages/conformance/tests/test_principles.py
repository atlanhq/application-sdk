"""The fleet principles the connector reviewer reads stay valid (FND-5315).

The principles are data other code depends on. The reviewer parses them, and
each ``Don't flag:`` field tells it which conformance rules already cover a
topic. So this file checks the shipped set, the format of every file, every
cited rule id, the old-rule mapping in the README, the ``principles-dir``
command, and the reader's own error messages.

The format checks read the raw text as well as going through
``conformance.principles``, so a reader that accepted too much would not make
them pass.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from conformance import cli, principles
from conformance.principles import AREAS, FLEET_DIR, LABELS, MAX_LINES, Principle
from conformance.suite.rules import CATALOG
from conformance.suite.schema.disposition import RuleScope
from tests._helpers import find_sdk_root

EXPECTED_IDS = [f"FP{n:02d}" for n in range(1, 18)]
OLD_RULE_COUNT = 25
README = FLEET_DIR / "README.md"

_CITED_RULE = re.compile(r"`([A-Z]\d{3})`")
_OLD_RULE_ROW = re.compile(
    r"^\| `((?:APP|PLATFORM)-[A-Z-]+-\d{3})` \| ([^|]*) \| ([^|]*)\|$", re.MULTILINE
)
_NO_RULE = "No conformance rule"


@pytest.fixture(scope="module")
def fleet() -> list[Principle]:
    return principles.load(FLEET_DIR)


def _file_for(principle_id: str) -> Path:
    matches = sorted(FLEET_DIR.glob(f"{principle_id.lower()}-*.md"))
    assert len(matches) == 1, f"{principle_id}: expected one file, found {matches}"
    return matches[0]


def _old_rule_rows() -> dict[str, tuple[str, str]]:
    rows = _OLD_RULE_ROW.findall(README.read_text(encoding="utf-8"))
    return {rule: (target.strip(), note.strip()) for rule, target, note in rows}


# ---------------------------------------------------------------------------
# The shipped set
# ---------------------------------------------------------------------------


def test_ships_exactly_the_seventeen_fleet_principles(fleet: list[Principle]) -> None:
    assert [p.id for p in fleet] == EXPECTED_IDS
    shipped = sorted(path.name for path in FLEET_DIR.glob("*.md"))
    assert shipped == sorted([*(p.path.name for p in fleet), "README.md"])


@pytest.mark.parametrize("principle_id", EXPECTED_IDS)
def test_id_matches_file_name(principle_id: str) -> None:
    path = _file_for(principle_id)
    assert principles.parse(path).id == principle_id
    assert re.fullmatch(
        rf"{principle_id.lower()}-[a-z0-9]+(-[a-z0-9]+)*\.md", path.name
    )


# ---------------------------------------------------------------------------
# The format of every file
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("principle_id", EXPECTED_IDS)
def test_file_follows_the_format(principle_id: str) -> None:
    path = _file_for(principle_id)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) <= MAX_LINES

    assert lines[0] == "---"
    close = lines.index("---", 1)
    keys = [line.partition(":")[0] for line in lines[1:close]]
    assert keys in (["id"], ["id", "applies-to"])

    assert lines[close + 1].startswith("# ")
    assert lines[close + 2] == ""

    body = lines[close + 3 :]
    assert all(line.strip() for line in body), "no blank lines among the fields"
    labels = [label for line in body for label in LABELS if line.startswith(label)]
    assert labels == list(LABELS), "the five labels, once each, in order"

    parsed = principles.parse(path)
    assert parsed.title == lines[close + 1][2:]
    assert set(parsed.areas) <= set(AREAS)
    assert len(set(parsed.areas)) == len(parsed.areas)
    assert 1 <= len(parsed.ask) <= 3
    assert len([line for line in body if line.startswith("- ")]) == len(parsed.ask)
    assert all((parsed.principle, parsed.why, parsed.dont_flag, parsed.severity))


# ---------------------------------------------------------------------------
# Conformance rule ids cited in the files
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", [*EXPECTED_IDS, "README"])
def test_cited_conformance_rules_exist_and_run_on_apps(name: str) -> None:
    path = README if name == "README" else _file_for(name)
    cited = sorted(set(_CITED_RULE.findall(path.read_text(encoding="utf-8"))))
    unknown = [rule for rule in cited if rule not in CATALOG]
    assert unknown == [], f"{path.name} cites rule ids that are not in the catalog"
    # A rule scoped to the SDK never runs on a connector app, so it enforces
    # nothing there and "conformance already covers this" would be false.
    sdk_only = [rule for rule in cited if CATALOG[rule].scope is RuleScope.SDK]
    assert sdk_only == [], f"{path.name} cites rules that do not run on app repos"


def test_dont_flag_names_what_ci_covers(fleet: list[Principle]) -> None:
    """Each principle cites the rules that cover it, or says outright that none does."""
    silent = [
        p.id
        for p in fleet
        if not _CITED_RULE.search(p.dont_flag) and _NO_RULE not in p.dont_flag
    ]
    assert silent == []


# ---------------------------------------------------------------------------
# README
# ---------------------------------------------------------------------------


def test_readme_lists_every_fleet_principle(fleet: list[Principle]) -> None:
    """The index also pins each file's areas: a dropped ``applies-to`` line
    would otherwise widen a principle to every file without anyone noticing."""
    text = README.read_text(encoding="utf-8")
    for p in fleet:
        areas = ", ".join(p.areas) if p.areas else "everywhere"
        assert f"| {p.id} | {p.title} | {areas} |" in text, p.id


def test_readme_maps_twenty_five_old_rules(fleet: list[Principle]) -> None:
    rows = _old_rule_rows()
    assert len(rows) == OLD_RULE_COUNT
    known = {p.id for p in fleet}
    for rule, (target, note) in rows.items():
        named = set(re.findall(r"FP\d{2}", target))
        assert named <= known, rule
        if target.startswith(("Dropped", "Moved to a deterministic check")):
            assert note, f"{rule}: say why it was dropped, or which check took it over"
        else:
            assert named, f"{rule}: maps to no principle, check or reason"


def test_readme_maps_every_old_shared_rule() -> None:
    """Every old rule still in the repo has a row. The rule sets are due to be
    deleted, so this skips once none is left; the count is pinned above."""
    root = find_sdk_root()
    rulesets = root / ".mothership" / "review-rulesets" if root else None
    old = _old_rule_ids(rulesets) if rulesets and rulesets.is_dir() else set()
    if not old:
        pytest.skip("the old rule sets under .mothership/review-rulesets are gone")
    unmapped = sorted(old - set(_old_rule_rows()))
    assert unmapped == [], "old rule ids the README table does not map"


def _old_rule_ids(rulesets: Path) -> set[str]:
    """Ids of every rule file a MANIFEST lists or a ``rules/`` directory holds."""
    files = set(rulesets.glob("*/rules/*.md"))
    for manifest in rulesets.glob("*/MANIFEST.yaml"):
        listed = re.findall(
            r"^\s*-\s*(rules/\S+\.md)\s*$", manifest.read_text("utf-8"), re.M
        )
        files.update(manifest.parent / entry for entry in listed)
    ids = set()
    for path in files:
        match = re.search(r"^id:\s*(\S+)\s*$", path.read_text("utf-8"), re.M)
        assert match, f"{path}: no id in front matter"
        ids.add(match.group(1))
    return ids


# ---------------------------------------------------------------------------
# principles-dir
# ---------------------------------------------------------------------------


def test_principles_dir_prints_the_shipped_directory(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        sys, "argv", ["atlan-application-sdk-conformance", "principles-dir"]
    )
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 0
    printed = Path(capsys.readouterr().out.strip())
    assert printed.is_absolute() and printed.is_dir()
    assert printed.resolve() == FLEET_DIR
    assert [p.id for p in principles.load(printed)] == EXPECTED_IDS
    assert (printed / "README.md").is_file()


def test_usage_lists_principles_dir() -> None:
    assert re.search(r"^  principles-dir +Print the absolute path", cli._USAGE, re.M)


# ---------------------------------------------------------------------------
# The reader
# ---------------------------------------------------------------------------

_VALID = """\
---
id: FP01
applies-to: [app, contract]
---
# A title

Principle: One thing
that wraps.
Why: A reason.
Ask:
- First question?
- Second question?
Don't flag: What a gate covers (`E001`).
Severity: High when bad.
"""


def _write(tmp_path: Path, text: str, name: str = "fp01-a-title.md") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_reads_a_valid_file(tmp_path: Path) -> None:
    path = _write(tmp_path, _VALID)
    assert principles.load(tmp_path) == [
        Principle(
            id="FP01",
            title="A title",
            areas=("app", "contract"),
            principle="One thing that wraps.",
            why="A reason.",
            ask=("First question?", "Second question?"),
            dont_flag="What a gate covers (`E001`).",
            severity="High when bad.",
            path=path,
        )
    ]


def test_without_applies_to_a_principle_applies_everywhere(tmp_path: Path) -> None:
    scoped = principles.parse(_write(tmp_path, _VALID))
    assert scoped.applies_to("contract") and not scoped.applies_to("tests")
    text = _VALID.replace("applies-to: [app, contract]\n", "")
    everywhere = principles.parse(_write(tmp_path, text))
    assert everywhere.areas == ()
    assert all(everywhere.applies_to(area) for area in AREAS)


def test_load_reads_repo_principles_and_skips_the_readme(tmp_path: Path) -> None:
    _write(tmp_path, _VALID.replace("id: FP01", "id: RP02"), "rp02-second.md")
    _write(tmp_path, _VALID.replace("id: FP01", "id: RP01"), "rp01-first.md")
    _write(tmp_path, "# Not a principle\n", "README.md")
    _write(tmp_path, "not markdown", "notes.txt")
    assert [p.id for p in principles.load(tmp_path)] == ["RP01", "RP02"]


@pytest.mark.parametrize(
    ("old", "new", "problem"),
    [
        ("---\nid: FP01", "id: FP01", "must start with a '---' line"),
        ("]\n---\n", "]\n", "not closed by a second '---' line"),
        (
            "id: FP01\n",
            "id: FP01\nowner: someone\n",
            "front matter line 'owner: someone'",
        ),
        ("id: FP01\n", "id: FP01\nid: FP02\n", "front matter has 'id' twice"),
        ("id: FP01\n", "", "front matter has no 'id'"),
        ("id: FP01", "id: FP1", "id 'FP1' is not FP or RP followed by two digits"),
        ("[app, contract]", "app, contract", "applies-to must be an inline list"),
        ("[app, contract]", "[app, src]", "applies-to names 'src'"),
        ("[app, contract]", "[app/**/*.py]", "applies-to names 'app/**/*.py'"),
        ("[app, contract]", "[]", "leave applies-to out"),
        ("[app, contract]", "[app, app]", "names an area twice"),
        ("# A title", "A title", "must be an H1 title"),
        ("# A title\n\n", "# A title\n", "followed by a blank line"),
        ("\nPrinciple:", "\nNote first.\nPrinciple:", "comes before 'Principle:'"),
        ("Why: A reason.\n", "", "'Ask:' is out of order; 'Why:' comes first"),
        ("Why: A reason.\n", "Why: A reason.\nWhy: Again.\n", "'Why:' appears twice"),
        ("Severity: High when bad.\n", "", "'Severity:' is missing"),
        ("Why: A reason.", "Why:", "'Why:' is empty"),
        ("Why: A reason.\n", "Why: A reason.\n\n", "blank line among the fields"),
        ("Why: A reason.", "Why: A reason.\n- a bullet", "only 'Ask:' takes"),
        ("Ask:\n", "Ask: What changed?\n", "'Ask:' must be alone on its line"),
        ("- Second question?", "Second question?", "every line is one '- ' bullet"),
        ("- First question?\n- Second question?\n", "", "'Ask:' has 0 bullets"),
        ("- Second question?", "- Two?\n- Three?\n- Four?", "'Ask:' has 4 bullets"),
    ],
)
def test_a_broken_file_is_named_with_its_problem(
    tmp_path: Path, old: str, new: str, problem: str
) -> None:
    assert old in _VALID
    path = _write(tmp_path, _VALID.replace(old, new, 1))
    with pytest.raises(ValueError) as error:
        principles.load(tmp_path)
    message = str(error.value)
    assert message.startswith(f"{path}: "), message
    assert problem in message


def test_a_file_may_have_twenty_six_lines_but_not_more(tmp_path: Path) -> None:
    spare = MAX_LINES - len(_VALID.splitlines())
    at_limit = _VALID.replace("that wraps.", "\n".join(["wraps."] * (spare + 1)))
    assert len(at_limit.splitlines()) == MAX_LINES
    assert principles.parse(_write(tmp_path, at_limit)).id == "FP01"
    over = at_limit.replace("wraps.\n", "wraps.\nwraps.\n", 1)
    with pytest.raises(ValueError, match="has 27 lines; the limit is 26"):
        principles.parse(_write(tmp_path, over))


def test_file_name_must_match_the_id(tmp_path: Path) -> None:
    path = _write(tmp_path, _VALID, "fp02-a-title.md")
    with pytest.raises(ValueError, match="file name must be 'fp01-<slug>.md'") as error:
        principles.load(tmp_path)
    assert str(path) in str(error.value)


def test_two_files_cannot_share_an_id(tmp_path: Path) -> None:
    _write(tmp_path, _VALID, "fp01-first.md")
    _write(tmp_path, _VALID, "fp01-second.md")
    with pytest.raises(ValueError, match="id FP01 is already used by fp01-first.md"):
        principles.load(tmp_path)


def test_a_missing_directory_is_an_error_not_an_empty_list(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not a directory"):
        principles.load(tmp_path / "absent")
