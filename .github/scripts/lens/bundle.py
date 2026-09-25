"""Group changed files into review units — deterministically.

open-code-review asks a model to cluster files once a PR has 4+ of them; lens
does it in code, which costs nothing and never varies between runs:

- A PR that fits one bundle's budget (`max_files`, `max_diff_tokens`) is one
  bundle: the whole change in one context, the cheapest possible review.
- Otherwise files cluster by their module directory — a test file joins the
  cluster of the source it tests (`tests/unit/x/test_y.py` ↔ `x/y.py`) — and
  clusters are packed into as few bundles as the limits allow.
- No bundle passes `max_files` or `max_diff_tokens`, so no single sub-review
  can blow its context.

Each bundle is reviewed by its own bounded agent with isolated context, so a
large PR degrades into more small reviews, not one huge unstable one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath

from .diff import FileDiff
from .llm import estimate_tokens


@dataclass
class Bundle:
    label: str
    files: list[FileDiff] = field(default_factory=list)

    @property
    def paths(self) -> list[str]:
        return [f.path for f in self.files]

    @property
    def changed_lines(self) -> int:
        return sum(f.additions + f.deletions for f in self.files)

    def diff_tokens(self) -> int:
        return sum(estimate_tokens(f.render()) for f in self.files)


def _module_key(path: str) -> str:
    p = PurePosixPath(path)
    parts = list(p.parts)
    if parts and parts[0] == "tests":
        # tests/unit/storage/test_batch.py -> storage ; tests/test_x.py -> tests
        rest = [
            x
            for x in parts[1:-1]
            if x not in ("unit", "integration", "e2e", "functional")
        ]
        stem = p.stem.removeprefix("test_")
        return "/".join(rest) or stem
    if parts and parts[0] == "application_sdk":
        return "/".join(parts[1:-1]) or p.stem
    if len(parts) >= 3 and parts[0] == ".github":
        return "/".join(parts[:2])
    return "/".join(parts[:-1]) or p.stem


def group(
    files: list[FileDiff],
    *,
    max_files: int = 8,
    max_diff_tokens: int = 9000,
) -> list[Bundle]:
    """As few bundles as the limits allow, related files kept together.

    Every bundle pays the static prefix and its own tool loop, and files
    split across bundles lose each other's context — so a PR that fits one
    bundle's budget IS one bundle (measured: a 5-file, 91-line PR had been
    split three ways by a file-count rule). Past that, module clusters are
    packed greedily, in path order, up to the limits."""
    if not files:
        return []
    total = sum(estimate_tokens(f.render()) for f in files)
    if len(files) <= max_files and total <= max_diff_tokens:
        return [Bundle("change", list(files))]

    by_key: dict[str, Bundle] = {}
    for f in files:
        key = _module_key(f.path)
        by_key.setdefault(key, Bundle(key)).files.append(f)

    clusters: list[Bundle] = []
    for key in sorted(by_key):
        clusters.extend(_split(by_key[key], max_files, max_diff_tokens))

    out: list[Bundle] = []
    for c in clusters:
        last = out[-1] if out else None
        if (
            last
            and len(last.files) + len(c.files) <= max_files
            and last.diff_tokens() + c.diff_tokens() <= max_diff_tokens
        ):
            last.files.extend(c.files)
            last.label = f"{last.label}+{c.label}"
        else:
            out.append(Bundle(c.label, list(c.files)))
    return out


def _split(b: Bundle, max_files: int, max_tokens: int) -> list[Bundle]:
    chunks: list[Bundle] = []
    cur = Bundle(b.label)
    cur_tokens = 0
    for f in b.files:
        t = estimate_tokens(f.render())
        if cur.files and (len(cur.files) >= max_files or cur_tokens + t > max_tokens):
            chunks.append(cur)
            cur = Bundle(f"{b.label}#{len(chunks) + 1}")
            cur_tokens = 0
        cur.files.append(f)
        cur_tokens += t
    if cur.files:
        chunks.append(cur)
    return chunks
