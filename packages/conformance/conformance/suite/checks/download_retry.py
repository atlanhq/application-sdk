"""C004 UnretriedToolDownload — CI fetches a tool over the network with no retry.

Why this rule exists
--------------------
CI that installs its toolchain at job time (``curl``/``wget`` for pkl, trivy,
gitleaks, daprd, helm, an ``install.sh`` piped into a shell) takes a live
dependency on a third-party CDN every single run. Those CDNs — github.com
release assets especially — serve 5xx bursts that last *minutes*, not seconds.

On 2026-08-12 one such burst produced HTTP 503s on ``apple/pkl`` release assets
and ``refused stream`` HTTP/2 errors on ``astral-sh/python-build-standalone``,
which failed four separate merge-queue entries across two repos. Every one of
those downloads was a one-shot fetch: no retry, no cache. The fix in each case
was one flag.

What counts as a violation
--------------------------
A ``curl``/``wget`` that *installs something* — it writes the response to a file
(``-o``/``--output``/``-O``) or pipes it into a shell or ``tar`` — and carries no
retry. A bare fetch whose output is inspected rather than executed (a version
lookup against an API, a health probe) is not flagged: those are usually inside
a poll loop that already tolerates failure.

Recognised as retried:

* wrapped in ``with-retry.sh`` (the SDK's own wrapper);
* ``curl --retry`` / ``--retry-all-errors`` / ``--retry-connrefused``;
* ``wget --tries`` / ``--retry-on-http-error`` / ``--retry-connrefused``.

Tuning-only companion flags — ``curl --retry-delay`` / ``--retry-max-time``,
``wget --waitretry`` — shape a retry's pacing but do not *enable* one: a
``wget --waitretry=10`` with no ``--tries`` still makes exactly one attempt.
They never count as retried.

Exempt: URLs on localhost/127.0.0.1 (a service health probe, not a download).

Note on ``curl --retry`` alone: without ``--retry-all-errors``, curl retries
transient *transport* errors but NOT an HTTP 503, which is the failure mode this
rule exists for. The message says so rather than silently accepting a flag that
does not cover the actual incident.

Retry flags are evaluated **per command segment**, not per logical line: the
line is split on ``&&``/``||``/``;`` and each segment's flags apply to the
download(s) inside it. Otherwise a complete retry on one ``curl``
(``--retry-all-errors``) would mask an incomplete retry on a sibling ``curl``
in the same line, and a wrapper at the head of a compound command would read
as covering fetchers the wrapper never invokes. A bare ``|`` does NOT split:
a pipeline is one command for retry purposes — splitting it would orphan the
fetcher from the shell/tar it pipes into, and the fetcher's flags apply to
the whole pipeline.

Line continuations are joined before matching, because the wrapper and the
command it wraps are conventionally written on separate physical lines::

    "${{ github.action_path }}/../../scripts/with-retry.sh" \\
      curl -fsSL -o /tmp/pkl "https://github.com/apple/pkl/..."

Known limits (deliberate — this is a textual check, not a shell parser):

* A fetcher reached through a variable (``"$TOOL" -o ...``) is not detected;
  detection keys on a literal ``curl``/``wget`` token. Under-reporting is the
  right failure direction for a rule whose findings a human triages.
* Within one command segment carrying several downloads, flags are shared
  (``curl --retry 5 -o a U1; curl -o b U2`` evaluates per segment, so the
  second curl is flagged; but ``curl --retry 5 -o a U1 -o b U2`` treats both
  outputs as retried, which is how curl actually behaves).
* A wrapped command whose wrapped segment also *contains* the download
  (``with-retry.sh sh -c 'curl …'``) is correctly seen as retried; a wrapper
  in one segment and the fetcher in another (``with-retry.sh make setup &&
  curl -o a U``) flags the curl — the wrapper wraps ``make``, not the curl.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from conformance.suite.checks._ast_common import make_cli_main, safe_read_text
from conformance.suite.schema.findings import Finding

SERIES = "C"
RULE_ID = "C004"

__all__ = ["SERIES", "RULE_ID", "discover", "scan_path", "scan_text", "main"]

_FETCHER_RE = re.compile(r"(?:^|[\s;&|(`$])(?P<tool>curl|wget)(?=\s)")

# Writes the response somewhere, rather than reading it inline.
_OUTPUT_FLAG_RE = re.compile(r"(?:^|\s)(?:-o|-O|--output(?:-document)?)(?:[\s=]|$)")

# Pipes the response into something that executes or unpacks it.
#
# `(?:\w+=\S+\s+)*` allows inline environment assignments between the pipe and
# the interpreter — `| DAPR_INSTALL_DIR="/usr/local/bin" /bin/bash -s ...` is
# how Dapr's documented installer is invoked, and without this the single most
# incident-prone line in the fleet reads as "not a download".
_EXECUTING_PIPE_RE = re.compile(
    r"\|\s*(?:sudo\s+)?(?:env\s+)?(?:\w+=\S+\s+)*"
    r"(?:(?:/usr)?/bin/)?(?:(?:ba|z|k)?sh|tar)\b"
)

# Toolchain installers that fetch over the network without going through
# curl/wget. `uv python install` pulls a python-build-standalone tarball from
# github.com releases and has no retry of its own — it is the download that
# started the incident behind this rule.
_MANAGED_INSTALLER_RE = re.compile(r"(?:^|[\s;&|(`])uv\s+python\s+install\b")

# Flags that *enable* a retry. Tuning-only companions (`--retry-delay`,
# `--retry-max-time`, `--waitretry`) are deliberately absent: they shape a
# retry's pacing but a `wget --waitretry=10` with no `--tries` still makes
# exactly one attempt, so they must not count as retried.
_RETRY_FLAG_RE = re.compile(
    r"(?:^|\s)--(?:retry|retry-all-errors|retry-connrefused|"
    r"retry-on-http-error|tries)(?:[\s=]|$)"
)
_RETRY_WRAPPER_RE = re.compile(r"with-retry\.sh")

# Splits a logical line into shell command segments: `a && b || c; d`. A bare
# `|` does NOT split — a pipeline is one command for retry purposes, and
# splitting it would orphan the fetcher from the shell/tar it pipes into.
# Naive on purpose (no quote awareness) — this is a textual check, not a shell
# parser, and a separator inside a quoted URL merely splits one segment into
# two that still carry the same flags.
_SEGMENT_SPLIT_RE = re.compile(r"\s*(?:&&|\|\||;)\s*")

# Scripts conventionally hoist the wrapper path into a variable and invoke it as
# `"$WITH_RETRY" wget ...`. Without resolving that, every such (correct) call
# reads as unwrapped — a false positive on exactly the code the rule wants.
_WRAPPER_ASSIGN_RE = re.compile(
    r"^\s*(?P<var>\w+)=[\"']?[^\"'\n]*with-retry\.sh", re.MULTILINE
)

# Discarding the body is definitionally not installing anything: `-o /dev/null`
# with `-w "%{http_code}"` is the idiomatic health probe, and those live inside
# their own poll loops.
_DEV_NULL_OUTPUT_RE = re.compile(r"(?:-o|-O|--output(?:-document)?)[\s=]+/dev/null\b")

# curl needs --retry-all-errors for --retry to cover an HTTP 503 at all.
_CURL_RETRY_ALL_ERRORS_RE = re.compile(r"(?:^|\s)--retry-all-errors(?:\s|$)")

_LOCALHOST_RE = re.compile(r"https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])")

_URL_RE = re.compile(r"https?://[^\s\"'`)]+")


@dataclass(frozen=True)
class LogicalLine:
    """One shell command, with physical continuations joined."""

    text: str
    line: int


def iter_logical_lines(text: str) -> Iterator[LogicalLine]:
    """Yield logical lines, joining trailing-backslash continuations.

    The reported line number is the FIRST physical line of the join, which is
    where a reader looks to find the command.
    """
    buffer: list[str] = []
    start = 0
    for lineno, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not buffer:
            start = lineno
        if stripped.endswith("\\"):
            buffer.append(stripped[:-1])
            continue
        buffer.append(stripped)
        yield LogicalLine(text=" ".join(buffer).strip(), line=start)
        buffer = []
    if buffer:
        yield LogicalLine(text=" ".join(buffer).strip(), line=start)


def _is_comment(text: str) -> bool:
    """True for a YAML/shell/Dockerfile comment line."""
    return text.lstrip().startswith("#")


def wrapper_variables(text: str) -> frozenset[str]:
    """Names of shell variables assigned a path ending in ``with-retry.sh``."""
    return frozenset(m.group("var") for m in _WRAPPER_ASSIGN_RE.finditer(text))


def command_segments(text: str) -> list[str]:
    """Split a logical line into shell command segments.

    `curl -o a U1 && curl -o b U2` is two commands; flags in one must not
    satisfy the other. Splitting on `&&`/`||`/`;` is deliberately naive — this
    is a textual check, and a separator inside a quoted URL just yields two
    segments that still carry the same flags. A bare `|` does not split: the
    fetcher and the shell/tar it pipes into are one command for retry purposes.
    """
    return [seg.strip() for seg in _SEGMENT_SPLIT_RE.split(text) if seg.strip()]


def is_tool_download(text: str) -> bool:
    """True when the command fetches something it will then run or install."""
    if not _FETCHER_RE.search(text):
        return False
    if _LOCALHOST_RE.search(text) or _DEV_NULL_OUTPUT_RE.search(text):
        return False
    return bool(_OUTPUT_FLAG_RE.search(text) or _EXECUTING_PIPE_RE.search(text))


def is_wrapped(text: str, wrapper_vars: frozenset[str] = frozenset()) -> bool:
    """True when the command is invoked through the with-retry wrapper."""
    if _RETRY_WRAPPER_RE.search(text):
        return True
    return any(
        re.search(r"\$\{?" + re.escape(var) + r"\}?", text) for var in wrapper_vars
    )


def is_retried(text: str, wrapper_vars: frozenset[str] = frozenset()) -> bool:
    """True when the command carries a retry wrapper or retry flag.

    Per segment: a download is retried when *its own* segment is wrapped or
    carries an enabling retry flag — a sibling segment's flags do not count.
    """
    segments = command_segments(text)
    if len(segments) <= 1:
        return is_wrapped(text, wrapper_vars) or bool(_RETRY_FLAG_RE.search(text))
    return all(
        is_wrapped(seg, wrapper_vars) or bool(_RETRY_FLAG_RE.search(seg))
        for seg in segments
        if is_tool_download(seg)
    )


def retry_is_complete(text: str, wrapper_vars: frozenset[str] = frozenset()) -> bool:
    """True unless this is a `curl --retry` with no `--retry-all-errors`.

    A retry wrapper covers everything (it re-runs the whole command on any
    non-zero exit), so only a bare curl-flag retry needs the extra check.
    Evaluated per segment: a complete retry on one curl does not excuse an
    incomplete retry on a sibling curl in the same logical line.
    """
    segments = command_segments(text)
    if len(segments) <= 1:
        return _segment_retry_is_complete(text, wrapper_vars)
    return all(
        _segment_retry_is_complete(seg, wrapper_vars)
        for seg in segments
        if is_tool_download(seg)
    )


def _segment_retry_is_complete(
    segment: str, wrapper_vars: frozenset[str] = frozenset()
) -> bool:
    """Single-segment completeness check (the pre-segmentation behaviour)."""
    if is_wrapped(segment, wrapper_vars):
        return True
    if not re.search(r"(?:^|[\s;&|(`$])curl(?=\s)", segment):
        return True
    return bool(_CURL_RETRY_ALL_ERRORS_RE.search(segment))


def _first_url(text: str) -> str:
    match = _URL_RE.search(text)
    return match.group(0) if match else "the download"


def is_managed_installer(text: str) -> bool:
    """True for a toolchain installer that downloads without curl/wget."""
    return bool(_MANAGED_INSTALLER_RE.search(text))


def scan_text(text: str, file: str) -> list[Finding]:
    """Scan a workflow / action / shell / Dockerfile body for C004 findings."""
    findings: list[Finding] = []
    wrapper_vars = wrapper_variables(text)
    for logical in iter_logical_lines(text):
        if _is_comment(logical.text):
            continue

        if is_managed_installer(logical.text) and not is_retried(
            logical.text, wrapper_vars
        ):
            findings.append(
                Finding(
                    rule_id=RULE_ID,
                    file=file,
                    line=logical.line,
                    column=1,
                    message=(
                        "`uv python install` downloads a python-build-standalone "
                        "tarball from github.com releases on every run, with no retry. "
                        "Prefer taking Python from the runner tool cache — "
                        "atlanhq/application-sdk/.github/actions/setup-deps@main, or "
                        "actions/setup-python plus UV_PYTHON_PREFERENCE=system — so the "
                        "happy path makes no network request at all."
                    ),
                    snippet=None,
                )
            )
            continue

        if not is_tool_download(logical.text):
            continue

        url = _first_url(logical.text)
        if not is_retried(logical.text, wrapper_vars):
            message = (
                f"Tool download from {url} has no retry — a single transient 5xx "
                f"fails the job. Wrap it in .github/scripts/with-retry.sh, or add "
                f"`--retry 5 --retry-delay 5 --retry-all-errors` (curl) / "
                f"`--tries=5 --waitretry=10 --retry-on-http-error=429,500,502,503,504` (wget)."
            )
        elif not retry_is_complete(logical.text, wrapper_vars):
            message = (
                f"Tool download from {url} uses `curl --retry` without "
                f"`--retry-all-errors`, so it retries transport errors but NOT an "
                f"HTTP 503 — the failure mode this guards against. Add "
                f"`--retry-all-errors`."
            )
        else:
            continue

        findings.append(
            Finding(
                rule_id=RULE_ID,
                file=file,
                line=logical.line,
                column=1,
                message=message,
                snippet=None,
            )
        )
    return findings


def scan_path(path: Path, root: Path) -> list[Finding]:
    """Scan a single file, producing repo-root-relative URIs."""
    text = safe_read_text(path)
    if text is None:
        return []
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    return scan_text(text, str(rel))


def discover(root: Path) -> list[Path]:
    """Discover CI files that can install a toolchain.

    Workflows and composite actions under ``.github/``, shell scripts they
    invoke, and Dockerfiles (a build-time ``RUN curl`` is the same exposure as
    a job-time one — it failed the same way in the incident behind this rule).
    """
    paths: list[Path] = []

    github_dir = root / ".github"
    if github_dir.is_dir():
        for pattern in (
            "workflows/*.yml",
            "workflows/*.yaml",
            "actions/**/action.yml",
            "actions/**/action.yaml",
            "**/*.sh",
        ):
            paths.extend(github_dir.glob(pattern))

    for pattern in ("Dockerfile", "*.Dockerfile", "**/Dockerfile"):
        paths.extend(p for p in root.glob(pattern) if p.is_file())

    return sorted(set(paths))


def _walk_ci_files(path: Path) -> list[Path]:
    """Enumerate candidate files under a directory passed on the CLI."""
    out: list[Path] = []
    for pattern in ("*.yml", "*.yaml", "*.sh", "Dockerfile", "*.Dockerfile"):
        out.extend(path.rglob(pattern))
    return sorted(set(out))


main = make_cli_main(
    scan_text,
    description="C004: scan CI files for tool downloads with no retry.",
    discover=_walk_ci_files,
    default_scan_paths=(".github",),
)
"""CLI entry point for C004 check."""


if __name__ == "__main__":
    sys.exit(main())
