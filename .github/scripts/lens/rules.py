"""Rule cards, matched to files by glob — the model sees only what applies.

`.github/lens/rules.toml` is an ordered list of `[[path]]` entries; the first
whose glob matches a file decides that file's cards (open-code-review's
first-match semantics: predictable, and a specific entry placed above a
general one simply wins). A bundle's cards are the union over its files,
each rendered once and tagged with the files it applies to.

This replaces handing every reviewer ~111 KB of rulebooks regardless of
what changed.
"""

from __future__ import annotations

import fnmatch
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path


def _glob_to_re(glob: str) -> re.Pattern[str]:
    """`**` crosses directories, `*` does not; `**/` may match zero dirs."""
    out, i = "", 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif glob.startswith("**", i):
            out += ".*"
            i += 2
        elif glob[i] == "*":
            out += "[^/]*"
            i += 1
        elif glob[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(glob[i])
            i += 1
    return re.compile(f"^{out}$")


def glob_match(path: str, glob: str) -> bool:
    return (
        bool(_glob_to_re(glob).match(path))
        or fnmatch.fnmatch(path, glob)
        and "/" not in glob
    )


@dataclass
class RuleSet:
    entries: list[tuple[str, list[str]]]
    cards: dict[str, str]

    def cards_for(self, path: str) -> list[str]:
        for glob, card_ids in self.entries:
            if glob_match(path, glob):
                return [c for c in card_ids if c in self.cards]
        return []

    def render_for(self, paths: list[str]) -> str:
        """Each applicable card once, with the files it governs."""
        owners: dict[str, list[str]] = {}
        order: list[str] = []
        for p in paths:
            for c in self.cards_for(p):
                if c not in owners:
                    owners[c] = []
                    order.append(c)
                owners[c].append(p)
        blocks = []
        for c in order:
            files = ", ".join(owners[c])
            blocks.append(
                f'<rules card="{c}" for="{files}">\n{self.cards[c].strip()}\n</rules>'
            )
        return "\n".join(blocks)


def load_rules(config_dir: Path) -> RuleSet:
    rules_file = config_dir / "rules.toml"
    cards_dir = config_dir / "cards"
    entries: list[tuple[str, list[str]]] = []
    if rules_file.exists():
        data = tomllib.loads(rules_file.read_text(encoding="utf-8"))
        entries = [(e["glob"], list(e.get("cards", []))) for e in data.get("path", [])]
    cards: dict[str, str] = {}
    if cards_dir.is_dir():
        for f in sorted(cards_dir.glob("*.md")):
            cards[f.stem] = f.read_text(encoding="utf-8")
    return RuleSet(entries=entries, cards=cards)
