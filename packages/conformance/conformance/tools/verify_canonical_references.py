"""Verify every rule's ``canonical_reference`` against checkouts of the apps it names.

Usage
-----
::

    python -m conformance.tools.verify_canonical_references \\
        --sdk-root . \\
        --app atlan-openapi-app=../atlan-openapi-app \\
        --app atlan-mysql-app=../atlan-mysql-app \\
        --app atlan-metabase-app=../atlan-metabase-app

Exits 0 when every claim holds, 1 when any is stale, and 2 when a reference
names an app with no ``--app`` checkout.  Run on a schedule by
``.github/workflows/canonical-reference-check.yaml``.

Why
---
A ``canonical_reference`` is the file a remediation agent mirrors a fix from.
``tests/test_catalog.py`` can only check that the value is path-shaped; nothing
in this repo can see the three reference apps.  A refactor in one of them
leaves the reference pointing at code that no longer exists, and the next
agent mirrors a fix from nothing.  This tool reads each reference the way an
agent does and checks what it can:

* **Paths.**  Every path-shaped token must exist in the repo it is attributed
  to.  A token is attributed to the nearest preceding repo name in the same
  reference (``atlan-mysql-app app/handler.py``); ``application_sdk/…``
  always means this SDK.  A path is resolved from the repo root, or from a
  directory the same reference already cited (``tests/unit/ — …
  (test_client.py)``).  A bare filename with no directory resolves if a file
  of that name exists anywhere in the repo.
* **Line claims.**  ``path:N`` or ``path:N-M`` must point inside the file.
* **Symbols.**  A backticked identifier (```fetch_metadata```,
  ```BaseE2ETest.entrypoint```) cited in a reference with at least one
  resolved file must occur in one of those files.  This is what catches a
  rename: the file is still there but the function the reference sends an
  agent to is not.

* **SDK modules.**  A dotted ``application_sdk.x.y`` must import from this
  repo's tree, and an attribute after it must occur in that module.

A backticked path is a literal (a ``.gitignore`` entry, a dialect string), not
a file to open, and is not checked as one.  A reference that names a path or
symbol *as absent* ("none of the three has a tests/sdr/") goes in
:data:`NOT_CHECKED` with the reason.
"""

from __future__ import annotations

import argparse
import keyword
import re
import sys
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

#: The repos a reference may name.  ``application_sdk`` is this repo: the
#: ``application_sdk/`` package plus the files beside it (``contract-toolkit/``).
REFERENCE_APPS = ("atlan-openapi-app", "atlan-mysql-app", "atlan-metabase-app")
SDK = "application_sdk"

#: Tokens a reference names on purpose although they are not there — the
#: reference says the repo does *not* have them, or they are runtime output —
#: keyed by ``(rule_id, token)`` with the reason.  Every entry is a claim this
#: tool stops checking, so keep each one tied to the sentence that needs it.
NOT_CHECKED: Mapping[tuple[str, str], str] = {
    ("E003", "residual/failures.jsonl"): "runtime output the app writes",
    ("K017", "openapi_metadata.json"): "runtime output the transform writes",
    ("K007", "data/toolkit_baseline.json"): "conformance package data, not the app",
    ("T013", "tests/sdr/"): "named as absent",
    ("T013", "tests/full_dag/"): "named as absent",
    ("D012", "uv.toml"): "named as absent",
    ("K001", "NativeApp.pkl"): "named as absent",
    ("K001", "NativeAppBundle.pkl"): "named as absent",
    ("K002", "Connectors.Type"): "a toolkit type App.pkl uses, not app source",
    ("L015", "logging"): "named as absent",
    ("P055", "metabase_questions"): "named as never populated",
    ("P059", "json.dumps"): "named as absent",
    ("B009", "application_sdk.test_utils"): "named as removed from the SDK",
    ("D003", "sqlalchemy.dialects"): "an entry-point group, not a name in source",
}

_FILE_EXT = r"(?:py|pkl|ya?ml|json|jsonl|toml|sql|lock|cfg|txt|md|sh)"

#: A repo name.  ``atlan-mysql-app#729`` is a PR, not a switch of repo.
_REPO_RE = re.compile(
    r"(?<![\w/-])(?P<repo>" + "|".join(map(re.escape, REFERENCE_APPS)) + r")(?![\w#-])"
    r"|(?<![\w/.])(?P<sdk>application_sdk)(?=\s)"
)

#: Files that carry no extension.
_BARE_NAMES = r"(?:Dockerfile|PklProject|\.gitignore)"

#: A path-shaped token, optionally with a ``:N`` / ``:N-M`` line claim:
#: ``dir/…/file.ext``, ``dir/…/`` (a directory says so with its trailing
#: slash, which is what separates ``tests/unit/`` from ``try/except``), a bare
#: ``file.ext`` (``contract_schema.lock.json``, but not the module
#: ``application_sdk.clients.sql``), or one of :data:`_BARE_NAMES`.
_PATH_RE = re.compile(
    r"(?<![\w./*<>@:$-])"
    r"(?P<path>"
    r"(?:[\w.-]+/)+(?:\w[\w.-]*\."
    + _FILE_EXT
    + r"(?![\w-])|"
    + _BARE_NAMES
    + r"(?![\w.-])"
    r"|(?=[\s),;]|\.(?!\w)|$))"
    r"|(?!application_sdk\.)\.?[\w-]+(?:\.[\w-]+)*\." + _FILE_EXT + r"(?![\w-])"
    r"|" + _BARE_NAMES + r"(?![\w.-])"
    r")"
    r"(?::(?P<start>\d+)(?:-(?P<end>\d+))?)?"
    r"(?![*<>@{$/]|\.\w)"
)

#: A dotted SDK module (``application_sdk.clients.sql``), optionally followed
#: by attributes of it.
_SDK_MODULE_RE = re.compile(r"(?<![\w.])application_sdk(?:\.\w+)+")

_BACKTICK_RE = re.compile(r"`+([^`]+)`+")
#: ``entity_bytes(...)`` / ``main()`` cite the callable.
_CALL_SUFFIX_RE = re.compile(r"\((?:\.\.\.)?\)$")
_SYMBOL_RE = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$")

#: Backticked words that belong to the prose, not to a cited file.
_NOT_SYMBOLS = frozenset({"True", "False", "None", "self", *keyword.kwlist})


@dataclass(frozen=True)
class Claim:
    """One checkable statement a reference makes."""

    rule_id: str
    repo: str
    kind: str
    """``path``, ``lines``, ``module`` or ``symbol``."""
    value: str


@dataclass(frozen=True)
class Failure:
    claim: Claim
    reason: str

    def render(self) -> str:
        c = self.claim
        return f"{c.rule_id}: {c.repo} {c.kind} {c.value!r} — {self.reason}"


@dataclass(frozen=True)
class _Segment:
    repo: str | None
    text: str


def _segments(reference: str) -> Iterator[_Segment]:
    """Split a reference at each repo name; text before the first has no repo."""
    marks = list(_REPO_RE.finditer(reference))
    first = marks[0].start() if marks else len(reference)
    if first:
        yield _Segment(None, reference[:first])
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(reference)
        yield _Segment(m.group("repo") or SDK, reference[m.end() : end])


def _blank_backticks(text: str) -> str:
    """Hide code spans from the path scan: a backticked path is a literal
    (a ``.gitignore`` entry, a dialect string), not a file the reader opens."""
    return _BACKTICK_RE.sub(lambda m: " " * len(m.group(0)), text)


def _sdk_path(token: str) -> str | None:
    """The SDK-relative path for a token that always means this repo."""
    for prefix in ("atlanhq/application-sdk/", "application_sdk/", "contract-toolkit/"):
        if token.startswith(prefix):
            return token.removeprefix("atlanhq/application-sdk/")
    return None


def _is_skipped(token: str, leads: bool) -> bool:
    head = token.split("/", 1)[0]
    # ``registry.atlan.com/…`` is an image, not a path.
    if "/" in token and "." in head and not head.startswith("."):
        return True
    # A one-word directory (``residual/``, ``components/``, ``atlanhq/``) is
    # a runtime or gitignored dir, or prose, far more often than a citation.
    # The token a reference opens with (``atlan-metabase-app tests/ — …``)
    # is the citation itself, whatever its shape.
    return not leads and token.endswith("/") and token.count("/") == 1


def _resolve(root: Path, token: str, dirs: Sequence[Path]) -> Path | None:
    rel = token.rstrip("/")
    for base in (root, *dirs):
        candidate = base / rel
        if candidate.exists():
            return candidate
    if "/" not in rel:
        for hit in sorted(root.rglob(rel)):
            if ".git" not in hit.relative_to(root).parts:
                return hit
    return None


def _resolve_module(sdk_root: Path, dotted: str) -> tuple[Path, list[str]] | None:
    """The file for the longest importable prefix of *dotted*, and the rest."""
    parts = dotted.split(".")
    for n in range(len(parts), 0, -1):
        base = sdk_root.joinpath(*parts[:n])
        for candidate in (base.with_suffix(".py"), base / "__init__.py"):
            if candidate.is_file():
                return candidate, parts[n:]
    return None


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return ""


def _contains_symbol(paths: Iterable[Path], symbol: str) -> bool:
    """True if *symbol* occurs in a cited file.

    A dotted symbol matches its parts in order (``self.context.resolve_x`` or
    ``CredentialRef.resolve``), or its leaf alone: ``BaseE2ETest.entrypoint``
    is cited from a subclass that never spells the base's name.
    """
    words = symbol.split(".")
    leaf = re.compile(r"\b" + re.escape(words[-1]) + r"\b")
    for path in paths:
        files = (
            [path]
            if path.is_file()
            else sorted(p for p in path.rglob("*") if p.is_file())
        )
        if any(leaf.search(_read(f)) for f in files):
            return True
    return False


@dataclass
class _ReferenceCheck:
    """The claims of one reference, checked in three passes."""

    rule_id: str
    roots: Mapping[str, Path]
    claims: list[Claim] = field(default_factory=list)
    failures: list[Failure] = field(default_factory=list)
    files: list[Path] = field(default_factory=list)
    dirs: dict[str, list[Path]] = field(default_factory=dict)

    def _claim(self, repo: str, kind: str, value: str) -> Claim:
        claim = Claim(self.rule_id, repo, kind, value)
        self.claims.append(claim)
        return claim

    def _fail(self, claim: Claim, reason: str) -> None:
        self.failures.append(Failure(claim, reason))

    def paths(self, reference: str) -> None:
        for seg in _segments(reference):
            for m in _PATH_RE.finditer(_blank_backticks(seg.text)):
                token = m.group("path")
                leads = not seg.text[: m.start()].strip()
                if (self.rule_id, token) in NOT_CHECKED or _is_skipped(token, leads):
                    continue
                sdk_rel = _sdk_path(token)
                # A path named before any repo cannot be attributed, so it is
                # not a claim about any checkout; test_catalog requires the
                # repo name.
                repo = SDK if sdk_rel is not None else seg.repo
                if repo is not None:
                    self._path(repo, sdk_rel or token, m.group("start"), m.group("end"))

    def _path(self, repo: str, token: str, start: str | None, end: str | None) -> None:
        claim = self._claim(repo, "path", token)
        root = self.roots.get(repo)
        hit = _resolve(root, token, self.dirs.get(repo, ())) if root else None
        if hit is None:
            self._fail(claim, "does not exist" if root else "no checkout supplied")
            return
        if hit.is_dir():
            self.dirs.setdefault(repo, []).append(hit)
        else:
            self.files.append(hit)
        if start is not None:
            self._lines(repo, f"{token}:{start}" + (f"-{end}" if end else ""), hit)

    def _lines(self, repo: str, span: str, hit: Path) -> None:
        claim = self._claim(repo, "lines", span)
        if hit.is_dir():
            self._fail(claim, "line claim on a directory")
            return
        first, _, last = span.rpartition(":")[2].partition("-")
        n = len(_read(hit).splitlines())
        if not 1 <= int(first) <= int(last or first) <= n:
            self._fail(claim, f"file has {n} lines")

    def modules(self, reference: str) -> None:
        sdk_root = self.roots.get(SDK)
        for dotted in dict.fromkeys(_SDK_MODULE_RE.findall(reference)):
            if (self.rule_id, dotted) in NOT_CHECKED:
                continue
            claim = self._claim(SDK, "module", dotted)
            hit = _resolve_module(sdk_root, dotted) if sdk_root else None
            if hit is None:
                self._fail(claim, "no such module")
                continue
            module, attrs = hit
            self.files.append(module)
            attr = attrs[0] if attrs else "__module__"
            if not attr.startswith("__") and not _contains_symbol([module], attr):
                self._fail(claim, f"{attr!r} not found in {module.name}")

    def symbols(self, reference: str) -> None:
        # Checked against everything the reference cited, across repos:
        # "`X` in app/a.py, as app/b.py does" names X in either file.
        cited = [*self.files, *(d for ds in self.dirs.values() for d in ds)]
        if not cited:
            return
        repos = "+".join(sorted(_repos_of(cited, self.roots)))
        spans = (
            _CALL_SUFFIX_RE.sub("", s.strip()) for s in _BACKTICK_RE.findall(reference)
        )
        for raw in dict.fromkeys(spans):
            if self._is_symbol(raw):
                claim = self._claim(repos, "symbol", raw)
                if not _contains_symbol(cited, raw):
                    self._fail(claim, "not found in any file the reference cites")

    def _is_symbol(self, raw: str) -> bool:
        return not (
            raw in _NOT_SYMBOLS
            or (self.rule_id, raw) in NOT_CHECKED
            or not _SYMBOL_RE.match(raw)
            or _PATH_RE.fullmatch(raw)
            or _SDK_MODULE_RE.fullmatch(raw)
        )


def verify_reference(
    rule_id: str, reference: str, roots: Mapping[str, Path]
) -> tuple[list[Claim], list[Failure]]:
    """Check one reference.  Returns every claim it makes and those that failed."""
    check = _ReferenceCheck(rule_id, roots)
    check.paths(reference)
    check.modules(reference)
    check.symbols(reference)
    return check.claims, check.failures


def _repos_of(files: Iterable[Path], roots: Mapping[str, Path]) -> set[str]:
    out: set[str] = set()
    for f in files:
        for name, root in roots.items():
            if f.is_relative_to(root):
                out.add(name)
    return out


def verify_catalog(
    references: Iterable[tuple[str, str]], roots: Mapping[str, Path]
) -> tuple[list[Claim], list[Failure]]:
    claims: list[Claim] = []
    failures: list[Failure] = []
    for rule_id, reference in references:
        c, f = verify_reference(rule_id, reference, roots)
        claims.extend(c)
        failures.extend(f)
    return claims, failures


def _catalog_references() -> list[tuple[str, str]]:
    from conformance.suite.schema import load_catalog

    return [
        (r.id, r.canonical_reference) for r in load_catalog() if r.canonical_reference
    ]


def _parse_app(value: str) -> tuple[str, Path]:
    name, sep, path = value.partition("=")
    if not sep or name not in REFERENCE_APPS:
        raise argparse.ArgumentTypeError(
            f"expected <app>=<path> with <app> one of {', '.join(REFERENCE_APPS)}: {value!r}"
        )
    return name, Path(path)


_DESCRIPTION = (
    "Verify every rule's canonical_reference against reference-app checkouts."
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=_DESCRIPTION)
    parser.add_argument(
        "--sdk-root", type=Path, required=True, help="checkout of this repo"
    )
    parser.add_argument(
        "--app",
        type=_parse_app,
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="checkout of a reference app (repeatable)",
    )
    args = parser.parse_args(argv)

    roots: dict[str, Path] = {SDK: args.sdk_root, **dict(args.app)}
    missing = [
        r
        for r in (*REFERENCE_APPS, SDK)
        if not roots.get(r, Path("/nonexistent")).is_dir()
    ]
    if missing:
        print(f"missing checkout(s): {', '.join(missing)}", file=sys.stderr)
        return 2

    claims, failures = verify_catalog(_catalog_references(), roots)
    rules = len({c.rule_id for c in claims})
    for failure in failures:
        print(failure.render())
    print(
        f"{len(claims)} claims across {rules} rules; {len(failures)} stale",
        file=sys.stderr,
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
