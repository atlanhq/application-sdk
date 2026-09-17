"""The rules that apply to these paths — selected, not loaded.

`REVIEW.md` promises the reviewer that the rules for the paths it is reviewing
are already in its context. Until this module the pack carried no rules, and
the seven rule files in `.mothership/rules/` were unreachable from the new lane
— 108 KB, 153 rules, 41 of which reached the model nowhere.

## Retrieval, not volume

The old lane loaded the whole corpus for every review. That was its single
largest input, and retrieval work on review agents is consistent that models
degrade with excess context. So this selects on two axes:

* **Who and where.** `rules.yaml` says which rule files a specialist owns and
  which paths they apply to. A `config-only` PR carries no performance rules;
  a `quality` specialist never sees the security rules.
* **What the diff actually contains.** Within an applicable file, a rule
  arrives in **full** — bad pattern, good pattern, rationale — only when the
  identifiers that rule is about appear in the diff. `time.sleep` in the diff
  brings PERF-003 in full. Everything else in the file arrives as a one-line
  index entry: id, title, severity. The reviewer knows the rule exists and
  what it is called; it does not carry the code examples for a rule the diff
  cannot trip.

A budget caps the full-text set. When it bites, the overflow demotes to the
index and the pack says so — a rule is never dropped silently.

## Identifiers, not keywords

The retrieval key is the identifiers in a rule's *bad-pattern* code block:
`requests.get`, `time.sleep`, `ThreadPoolExecutor`, `json.dumps`. Prose
keywords were tried first and rejected — "async", "error", "storage" appear in
every diff and every rule, so prose matching selects everything, which is the
old lane again with extra steps. Code identifiers are specific enough to mean
something and are exactly what a diff either contains or does not.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

import yaml

RULES_DIR = Path(__file__).resolve().parents[2] / ".mothership/rules"
INDEX_PATH = Path(__file__).resolve().parents[2] / ".mothership/pr-loop/data/rules.yaml"

_HEADING = re.compile(r"^###\s+(?P<title>.+?)\s*$", re.M)
_SEVERITY = re.compile(r"\*\*Severity:\*\*\s*(\w+)", re.I)
_SEVERITY_IN_TITLE = re.compile(
    r"\((Critical|Important|Minor|High|Medium|Low)\)\s*$", re.I
)
_CODE_BLOCK = re.compile(r"```[a-z]*\n(.*?)```", re.S)
_IDENT = re.compile(
    r"\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+\b|\b[A-Z][A-Za-z0-9]{3,}\b|\b[a-z_]{5,}\("
)
#: Inline code in prose: `@dataclass`, `BaseModel`, `default_factory`,
#: `application_sdk/contracts/`. The prose-only rules — most of the security
#: and architecture corpus — carry their identifiers this way rather than in
#: code blocks, and they are exactly the terms a diff either contains or not.
_INLINE = re.compile(r"`([^`\n]{3,60})`")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s")

#: Identifiers too common to mean anything as a retrieval key. A rule whose
#: only identifiers are these matches by index only.
_NOISE = frozenset(
    {
        "self",
        "return",
        "async",
        "await",
        "import",
        "from",
        "class",
        "def",
        "print",
        "logger",
        "logging",
        "typing",
        "pydantic",
        "field",
        "Optional",
        "Any",
        "None",
        "True",
        "False",
        "dict",
        "list",
        "str",
        "int",
        "raise",
        "except",
        "Exception",
        "assert",
        "pytest",
        "mock",
        "Mock",
        "patch",
    }
)


@dataclass(frozen=True)
class Rule:
    file: str
    title: str
    severity: str
    body: str
    identifiers: frozenset[str]

    @property
    def summary(self) -> str:
        """The rule's first sentence of prose — its claim, without its examples.

        An index entry that is only a title tells the reviewer a rule exists;
        one that carries the claim tells it what the rule forbids. For the
        prose-only rules this is most of the value at a fraction of the size.
        """
        prose = _CODE_BLOCK.sub("", self.body)
        prose = re.sub(r"\*\*Severity:\*\*\s*\w+\s*", "", prose)
        prose = re.sub(r"^#{2,}.*$", "", prose, flags=re.M)
        prose = " ".join(prose.split())
        first = _SENTENCE_END.split(prose, maxsplit=1)[0] if prose else ""
        return first[:220].rstrip() + ("…" if len(first) > 220 else "")

    @property
    def index_line(self) -> str:
        sev = f" · {self.severity}" if self.severity else ""
        claim = f" — {self.summary}" if self.summary else ""
        return f"- **{self.title}**{sev}{claim}"


@dataclass(frozen=True)
class RuleFile:
    name: str
    specialists: frozenset[str]
    paths: tuple[str, ...]
    rules: tuple[Rule, ...]


@dataclass
class Selection:
    full: list[Rule] = field(default_factory=list)
    index: list[Rule] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    demoted: int = 0

    @property
    def empty(self) -> bool:
        return not self.full and not self.index


# --------------------------------------------------------------------------
# Parsing the corpus
# --------------------------------------------------------------------------


def parse_rules(text: str, file: str) -> tuple[Rule, ...]:
    """Split one rule file into its `###` rules.

    A rule's retrieval identifiers come from its code blocks — preferentially
    the bad pattern, since that is what a diff would contain if the rule
    applies. `##` headings are sections, not rules, and are skipped.
    """
    matches = list(_HEADING.finditer(text))
    out: list[Rule] = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        title = m.group("title").strip()
        sev_m = _SEVERITY.search(body) or _SEVERITY_IN_TITLE.search(title)
        severity = sev_m.group(1).capitalize() if sev_m else ""
        title = _SEVERITY_IN_TITLE.sub("", title).strip()
        out.append(
            Rule(
                file=file,
                title=title,
                severity=severity,
                body=body,
                identifiers=frozenset(_identifiers(body)),
            )
        )
    return tuple(out)


def _identifiers(body: str) -> Iterable[str]:
    blocks = _CODE_BLOCK.findall(body)
    # The bad pattern is what a diff would contain if the rule applies; the
    # good pattern is what the fix looks like. Prefer the former, fall back to
    # everything when a rule has only one block.
    bad = [b for b in blocks if "bad" in body[: body.find(b)].lower()[-120:]]
    source = "\n".join(bad or blocks)
    for m in _IDENT.finditer(source):
        ident = m.group(0).rstrip("(")
        if ident in _NOISE or ident.split(".")[0] in _NOISE:
            continue
        yield ident
    # Prose-only rules keep their identifiers in backticks. Strip a trailing
    # call or path separator so `run_in_thread()` and `contracts/` match the
    # bare token a diff would contain.
    prose = _CODE_BLOCK.sub("", body)
    for m in _INLINE.finditer(prose):
        term = m.group(1).strip().rstrip("()/")
        if " " in term or term in _NOISE or term.split(".")[0] in _NOISE:
            continue
        if len(term) < 4 and not term.startswith("@"):
            continue
        yield term


def load_corpus(
    rules_dir: Path | str | None = None, index_path: Path | str | None = None
) -> list[RuleFile]:
    """Every indexed rule file, parsed, with its routing."""
    root = Path(rules_dir or RULES_DIR)
    raw = yaml.safe_load(Path(index_path or INDEX_PATH).read_text(encoding="utf-8"))
    out: list[RuleFile] = []
    for name, entry in (raw.get("rules") or {}).items():
        path = root / name
        if not path.exists():
            raise FileNotFoundError(
                f"rules.yaml indexes {name} but {path} does not exist — the "
                "reviewer would be promised rules that are not there"
            )
        out.append(
            RuleFile(
                name=name,
                specialists=frozenset(entry.get("specialists") or ()),
                paths=tuple(entry.get("paths") or ()),
                rules=parse_rules(path.read_text(encoding="utf-8"), name),
            )
        )
    return out


def full_text_budget(index_path: Path | str | None = None) -> int:
    raw = yaml.safe_load(Path(index_path or INDEX_PATH).read_text(encoding="utf-8"))
    return int(raw.get("full_text_budget_chars") or 24_000)


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------


def _path_applies(changed: Sequence[str], globs: Sequence[str]) -> bool:
    for path in changed:
        subject = "/" + path.lstrip("/")
        for glob in globs:
            pattern = glob if glob.startswith(("/", "*")) else "/" + glob
            if fnmatch.fnmatch(subject, pattern) or fnmatch.fnmatch(path, glob):
                return True
    return False


def select(
    corpus: Sequence[RuleFile],
    *,
    specialist: str,
    changed_paths: Sequence[str],
    diff: str,
    budget_chars: int,
) -> Selection:
    """The rules this specialist should hold for this diff.

    Full text is ranked so the budget spends itself on the rules with the most
    identifier hits — the ones the diff most plausibly trips — and demotion
    happens from the least-matched end.
    """
    sel = Selection()
    candidates: list[tuple[int, Rule]] = []
    for rf in corpus:
        if specialist not in rf.specialists:
            continue
        if not _path_applies(changed_paths, rf.paths):
            continue
        sel.files.append(rf.name)
        for rule in rf.rules:
            hits = sum(1 for ident in rule.identifiers if ident in diff)
            if hits:
                candidates.append((hits, rule))
            else:
                sel.index.append(rule)

    spent = 0
    for hits, rule in sorted(candidates, key=lambda hr: -hr[0]):
        if spent + len(rule.body) <= budget_chars:
            sel.full.append(rule)
            spent += len(rule.body)
        else:
            sel.index.append(rule)
            sel.demoted += 1
    return sel


def render(sel: Selection) -> str:
    """The pack's rules section. Empty string when nothing applies."""
    if sel.empty:
        return ""
    lines = ["## Rules that apply to these paths", ""]
    lines.append(
        f"From {', '.join(sel.files)}. Full text below for rules whose "
        "identifiers appear in this diff; the rest listed by name so you know "
        "they exist. Cite a rule's title when a finding rests on it."
    )
    if sel.full:
        lines += ["", "### In full — the diff contains what these rules are about", ""]
        for rule in sel.full:
            lines += [
                f"#### {rule.title}" + (f" · {rule.severity}" if rule.severity else ""),
                "",
                rule.body,
                "",
            ]
    if sel.index:
        lines += ["", "### Also applicable — by name", ""]
        lines += [rule.index_line for rule in sel.index]
    if sel.demoted:
        lines += [
            "",
            f"({sel.demoted} rule(s) matched this diff but the full-text budget "
            "was spent; they appear by name above. Ask for one if a finding "
            "turns on it.)",
        ]
    return "\n".join(lines)
