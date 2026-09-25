"""lens bench: review known changes, score against their answer keys, compare runs.

    python -m lens bench --repo atlanhq/application-sdk --root ../.. [--baseline old.json] [--out new.json]

A case (``.github/lens/bench/cases/*.toml``) pins a change by commit SHAs and
says what a good review finds:

    [source]
    base = "<sha>"
    head = "<sha>"
    title = "..."            # the PR's intent, as lens would read it
    body = "..."

    [[expect]]               # a finding lens should make
    id = "leftover-exc-info"
    path = "application_sdk/app/context.py"
    quote_any = ["exc_info=True"]     # matched on the quoted code, not on wording
    keywords_any = []                 # or on words in the title/body, when no quote fits
    min_severity = "low"              # at least this…
    max_severity = "medium"           # …and at most this (severity calibration)
    required = true                   # false = a bonus: scored, not counted against recall

    [[must_not_flag]]        # a trap: a finding here counts against precision
    path = "..."
    quote_any = ["..."]

    [approach]
    verdict_any = ["sound", "concerns"]
    mention_any = [["consumer", "caller"]]   # each inner list: at least one word must appear

Each case is reviewed fresh — no PR state, nothing posted — with lens's
real pipeline and model. The score is computed in code, so two runs of the
same config differ only by the model's own variance.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .diff import snippet_lines
from .findings import SEVERITIES, Finding, PRState
from .github import GitHub
from .review import run


def _norm(s: str) -> str:
    return " ".join(s.split()).lower()


def _sev_rank(s: str) -> int:
    return SEVERITIES.index(s) if s in SEVERITIES else len(SEVERITIES)


class BenchGitHub:
    """Real GitHub reads for a pinned base/head, no PR state, no writes."""

    def __init__(self, gh: GitHub, source: dict[str, Any]) -> None:
        self.gh = gh
        self.src = source

    def pr(self, number: int) -> dict[str, Any]:
        return {
            "head": {"sha": self.src["head"]},
            "base": {"sha": self.src["base"]},
            "title": self.src.get("title", ""),
            "body": self.src.get("body", ""),
        }

    def issue_comments(self, number: int) -> list[dict[str, Any]]:
        return []  # always a fresh first review

    def compare_status(self, a: str, b: str) -> str:
        return "diverged"

    def diff(self, a: str, b: str) -> str:
        return self.gh.diff(a, b)

    def file_at(self, path: str, ref: str) -> str | None:
        return self.gh.file_at(path, ref)

    # Writes are never made (run(post=False)); these exist only for safety.
    def upsert_comment(self, *a: Any, **k: Any) -> str:
        return ""

    def review(self, *a: Any, **k: Any) -> None:
        return None

    def set_status(self, *a: Any, **k: Any) -> None:
        return None

    def comment(self, *a: Any, **k: Any) -> None:
        return None


def _matches(f: Finding, spec: dict[str, Any]) -> bool:
    if spec.get("path") and f.path != spec["path"]:
        return False
    ev = _norm(" ".join(snippet_lines(f.evidence)))
    for q in spec.get("quote_any") or []:
        qn = _norm(" ".join(snippet_lines(q)))
        if qn and (qn in ev or (ev and ev in qn)):
            return True
    text = _norm(f"{f.title} {f.body}")
    kws = spec.get("keywords_any") or []
    return bool(kws) and any(_norm(k) in text for k in kws)


@dataclass
class CaseScore:
    case: str
    caught: list[str] = field(default_factory=list)
    missed: list[str] = field(default_factory=list)
    bonus_caught: list[str] = field(default_factory=list)
    severity_off: list[str] = field(default_factory=list)
    traps_hit: list[str] = field(default_factory=list)
    unexpected: list[str] = field(default_factory=list)
    findings: int = 0
    approach_ok: bool | None = None
    approach_notes: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    calls: int = 0
    failed_requests: int = 0
    seconds: float = 0.0
    error: str = ""

    @property
    def recall(self) -> float:
        n = len(self.caught) + len(self.missed)
        return len(self.caught) / n if n else 1.0

    @property
    def precision(self) -> float:
        good = self.findings - len(self.traps_hit) - len(self.unexpected)
        return good / self.findings if self.findings else 1.0


def score_case(
    name: str, case: dict[str, Any], state: PRState, unplaced: list[Finding]
) -> CaseScore:
    sc = CaseScore(case=name)
    found = [f for f in state.findings if f.status == "open"] + unplaced
    sc.findings = len(found)
    used: set[str] = set()
    for exp in case.get("expect", []):
        hits = [f for f in found if _matches(f, exp)]
        eid = exp.get("id", exp.get("path", "?"))
        if hits:
            used.update(f.id for f in hits)
            (sc.caught if exp.get("required", True) else sc.bonus_caught).append(eid)
            # The most severe matching finding is the one judged. Ranks run critical=0 .. low=3,
            # so "less severe than the floor" is a HIGHER rank.
            sev = min(hits, key=lambda f: _sev_rank(f.severity)).severity
            floor, ceiling = exp.get("min_severity"), exp.get("max_severity")
            too_low = bool(floor) and _sev_rank(sev) > _sev_rank(floor)
            too_high = bool(ceiling) and _sev_rank(sev) < _sev_rank(ceiling)
            if too_low or too_high:
                sc.severity_off.append(
                    f"{eid}: got {sev}, expected {floor or 'any'}..{ceiling or 'any'}"
                )
        elif exp.get("required", True):
            sc.missed.append(eid)
    for trap in case.get("must_not_flag", []):
        for f in found:
            if _matches(f, trap):
                sc.traps_hit.append(
                    f"{f.id} {f.path}:{f.line or f.head_line} {f.title[:60]}"
                )
                used.add(f.id)
    sc.unexpected = [
        f"{f.id} {f.severity} {f.path}:{f.line or f.head_line} {f.title[:70]}"
        for f in found
        if f.id not in used
    ]
    ap = case.get("approach")
    if ap:
        got = state.approach or {}
        text = _norm(
            " ".join(
                [got.get("problem", ""), got.get("approach", "")]
                + [
                    f"{c.get('title', '')} {c.get('why', '')}"
                    for c in got.get("concerns") or []
                ]
            )
        )
        ok = True
        if ap.get("verdict_any") and got.get("verdict") not in ap["verdict_any"]:
            ok = False
            sc.approach_notes.append(
                f"verdict {got.get('verdict')!r} not in {ap['verdict_any']}"
            )
        for group in ap.get("mention_any") or []:
            if not any(_norm(w) in text for w in group):
                ok = False
                sc.approach_notes.append(
                    f"approach check never mentions any of {group}"
                )
        sc.approach_ok = ok
    return sc


def run_bench(
    *,
    gh: GitHub,
    root: Path,
    cfg: Any,
    rules: Any,
    client_factory: Any,
    cases_dir: Path,
) -> list[CaseScore]:
    import time  # noqa: PLC0415 - local: the rest of lens never needs wall time here

    out: list[CaseScore] = []
    for path in sorted(cases_dir.glob("*.toml")):
        case = tomllib.loads(path.read_text(encoding="utf-8"))
        t0 = time.monotonic()
        try:
            res = run(
                gh=BenchGitHub(gh, case["source"]),
                number=0,
                root=root,
                cfg=cfg,
                rules=rules,
                client_factory=client_factory,
                force=True,
                post=False,
            )
            sc = score_case(path.stem, case, res.state or PRState(), res.unplaced)
            led = (res.state.ledger if res.state else {}) or {}
            sc.cost_usd = float(led.get("spent_usd", 0.0))
            sc.calls = int(led.get("calls", 0))
            sc.failed_requests = int(led.get("failed_requests", 0))
            if res.incomplete:
                sc.error = "; ".join(res.incomplete)[:200]
        except Exception as e:  # noqa: BLE001 - one broken case must not stop the bench
            sc = CaseScore(case=path.stem, error=f"{type(e).__name__}: {e}"[:200])
        sc.seconds = round(time.monotonic() - t0, 1)
        out.append(sc)
    return out


def to_json(scores: list[CaseScore], config_hash: str) -> dict[str, Any]:
    caught = sum(len(s.caught) for s in scores)
    missed = sum(len(s.missed) for s in scores)
    found = sum(s.findings for s in scores)
    bad = sum(len(s.traps_hit) + len(s.unexpected) for s in scores)
    return {
        "config": config_hash,
        "recall": round(caught / (caught + missed), 3) if caught + missed else 1.0,
        "precision": round((found - bad) / found, 3) if found else 1.0,
        "severity_off": sum(len(s.severity_off) for s in scores),
        "approach_ok": sum(1 for s in scores if s.approach_ok),
        "approach_cases": sum(1 for s in scores if s.approach_ok is not None),
        "cost_usd": round(sum(s.cost_usd for s in scores), 4),
        "cases": [
            {
                **{k: v for k, v in s.__dict__.items()},
                "recall": round(s.recall, 3),
                "precision": round(s.precision, 3),
            }
            for s in scores
        ],
    }


def render(report: dict[str, Any], baseline: dict[str, Any] | None = None) -> str:
    def delta(key: str) -> str:
        if not baseline or key not in baseline:
            return ""
        d = report[key] - baseline[key]
        return (
            f" ({'+' if d >= 0 else ''}{d:.3f})"
            if isinstance(d, float)
            else f" ({'+' if d >= 0 else ''}{d})"
        )

    lines = [
        f"## lens bench · config {report['config']}",
        "",
        "| recall | precision | severity off | approach ok | cost |",
        "|---|---|---|---|---|",
        f"| {report['recall']:.0%}{delta('recall')} | {report['precision']:.0%}{delta('precision')} "
        f"| {report['severity_off']}{delta('severity_off')} | {report['approach_ok']}/{report['approach_cases']} "
        f"| ${report['cost_usd']:.3f} |",
        "",
        "| case | caught | missed | severity off | traps | unexpected | approach | cost | calls | failed | time |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    base_cases = {c["case"]: c for c in (baseline or {}).get("cases", [])}
    for c in report["cases"]:
        was = base_cases.get(c["case"], {})
        newly = [x for x in c["caught"] if x not in was.get("caught", c["caught"])]
        lost = [x for x in was.get("caught", []) if x not in c["caught"]]
        change = (f" **+{', '.join(newly)}**" if newly else "") + (
            f" **−{', '.join(lost)}**" if lost else ""
        )
        lines.append(
            f"| {c['case']} | {', '.join(c['caught']) or '-'}{change} | {', '.join(c['missed']) or '-'} "
            f"| {'; '.join(c['severity_off']) or '-'} | {len(c['traps_hit'])} | {len(c['unexpected'])} "
            f"| {'✅' if c['approach_ok'] else ('-' if c['approach_ok'] is None else '❌ ' + '; '.join(c['approach_notes']))} "
            f"| ${c['cost_usd']:.3f} | {c['calls']} | {c['failed_requests']} | {c['seconds']}s |"
            + (f"\n| ↳ error | {c['error']} |||||||||" if c["error"] else "")
        )
    unexpected = [(c["case"], u) for c in report["cases"] for u in c["unexpected"]]
    if unexpected:
        lines += [
            "",
            "**Unexpected findings — label each as a real issue (add an `expect`) or a false positive (add a `must_not_flag`):**",
        ]
        lines += [f"- `{case}` {u}" for case, u in unexpected]
    return "\n".join(lines) + "\n"


def write(report: dict[str, Any], out: str | None) -> None:
    if out:
        Path(out).write_text(json.dumps(report, indent=2, default=str))
