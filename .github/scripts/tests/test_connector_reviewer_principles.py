"""The fleet principles the connector reviewer reads stay valid (FND-5315).

The principles are data other code depends on. The reviewer parses them, and
each ``Don't flag:`` field tells it which conformance rules already cover a
topic. So this file checks the shipped set, the format of every file, every
cited rule id, and the old-rule mapping in the README.

The files are read with the reviewer's own loader
(``connector_reviewer.principles``), so what passes here is what a review
loads. The format checks also read the raw text, and hold the shipped files to
a tighter shape than the loader accepts, so a loader that accepted too much
would not make them pass. The loader's own behaviour is tested in
``test_connector_reviewer.py``.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from connector_reviewer.principles import (
    AREAS,
    Principle,
    load_principles,
    parse_principle,
)

_REPO = Path(__file__).resolve().parents[3]
FLEET_DIR = _REPO / ".github" / "connector-reviewer" / "principles"

#: The labelled fields, in the order every shipped file must have them.
LABELS = ("Principle:", "Why:", "Ask:", "Don't flag:", "Severity:")
#: A shipped principle is at most this many lines, front matter included.
MAX_LINES = 26
_RULES_DIR = _REPO / "packages" / "conformance" / "conformance" / "suite" / "rules"

EXPECTED_IDS = [f"FP{n:02d}" for n in range(1, 18)]
OLD_RULE_COUNT = 25
README = FLEET_DIR / "README.md"

_CITED_RULE = re.compile(r"`([A-Z]\d{3})`")
_OLD_RULE_ROW = re.compile(
    r"^\| `((?:APP|PLATFORM)-[A-Z-]+-\d{3})` \| ([^|]*) \| ([^|]*)\|$", re.MULTILINE
)
_NO_RULE = "No conformance rule"


@pytest.fixture(scope="module")
def fleet(tmp_path_factory: pytest.TempPathFactory) -> list[Principle]:
    """The shipped fleet principles, loaded the way a review loads them. A
    file the loader skips shows up as a note, and none may be skipped."""
    loaded = load_principles(FLEET_DIR, tmp_path_factory.mktemp("app"))
    assert loaded.notes == []
    return loaded.fleet


def _parse(path: Path) -> Principle:
    return parse_principle(path.read_text(encoding="utf-8"), "fleet")


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
    expected = [_file_for(p.id).name for p in fleet]
    assert shipped == sorted([*expected, "README.md"])


@pytest.mark.parametrize("principle_id", EXPECTED_IDS)
def test_id_matches_file_name(principle_id: str) -> None:
    path = _file_for(principle_id)
    assert _parse(path).id == principle_id
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

    parsed = _parse(path)
    assert parsed.title == lines[close + 1][2:]
    assert set(parsed.applies_to) <= set(AREAS)
    assert len(set(parsed.applies_to)) == len(parsed.applies_to)
    assert 1 <= len(parsed.ask) <= 3
    assert len([line for line in body if line.startswith("- ")]) == len(parsed.ask)
    assert all((parsed.principle, parsed.why, parsed.dont_flag, parsed.severity))


# ---------------------------------------------------------------------------
# Conformance rule ids cited in the files
# ---------------------------------------------------------------------------


def _rule_scopes() -> dict[str, str]:
    """Rule id -> SDK/APP/BOTH, read from each ``RuleDefinition(...)`` in the
    catalog source. Read as text, like the lens card checks, so this test does
    not depend on the conformance package being installed."""
    scopes: dict[str, str] = {}
    for source in sorted(_RULES_DIR.glob("*.py")):
        text = source.read_text(encoding="utf-8")
        starts = list(re.finditer(r'id="([A-Z]\d{3})"', text))
        for index, match in enumerate(starts):
            end = starts[index + 1].start() if index + 1 < len(starts) else len(text)
            scope = re.search(r"scope=RuleScope\.(\w+)", text[match.start() : end])
            scopes[match.group(1)] = scope.group(1) if scope else "?"
    return scopes


@pytest.mark.parametrize("name", [*EXPECTED_IDS, "README"])
def test_cited_conformance_rules_exist_and_run_on_apps(name: str) -> None:
    path = README if name == "README" else _file_for(name)
    cited = sorted(set(_CITED_RULE.findall(path.read_text(encoding="utf-8"))))
    scopes = _rule_scopes()
    assert scopes, "conformance rule catalog not found"
    unknown = [rule for rule in cited if rule not in scopes]
    assert unknown == [], f"{path.name} cites rule ids that are not in the catalog"
    # A rule scoped to the SDK never runs on a connector app, so it enforces
    # nothing there and "conformance already covers this" would be false.
    sdk_only = [rule for rule in cited if scopes[rule] == "SDK"]
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
        areas = ", ".join(p.applies_to) if p.applies_to else "everywhere"
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
    rulesets = _REPO / ".mothership" / "review-rulesets"
    old = _old_rule_ids(rulesets) if rulesets.is_dir() else set()
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
    ids: set[str] = set()
    for path in files:
        match = re.search(r"^id:\s*(\S+)\s*$", path.read_text("utf-8"), re.M)
        assert match, f"{path}: no id in front matter"
        ids.add(match.group(1))
    return ids
