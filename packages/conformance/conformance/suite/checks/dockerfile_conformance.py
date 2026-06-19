"""I-series container image conformance checks.

I001 — DockerfileWrongBaseImage
    The final-stage FROM must be exactly
    ``registry.atlan.com/public/app-runtime-base:3``.  Any other base image —
    wrong registry, wrong tag (including ``:latest``), or raw upstream Python —
    is flagged.

I002 — DockerfileEntrypointOverride
    CMD and ENTRYPOINT must not appear in the Dockerfile.  The base image
    co-launches ``daprd`` and handles graceful shutdown; overriding either
    instruction silently removes that behaviour.

I003 — DockerfileAppModuleMissing
    ``ENV ATLAN_APP_MODULE=<module>:<AppClass>`` must be set to a non-empty
    value.  The runtime uses this to locate and instantiate the application
    class.

I004 — DockerfileAppModeHardcoded
    ``ENV ATLAN_APP_MODE`` must not be set in the Dockerfile.  Runtime mode
    must come from the deployment environment, not be baked into the image.

I005 — DockerfileRootUser
    No ``USER root`` or ``USER 0`` instruction may appear in the final stage.
    The base image already establishes ``appuser``; switching to root in the
    final stage violates the non-root execution policy.

Discovery
---------
Scans ``Dockerfile`` at the repo root.  If absent, the I-series no-ops (no
finding for a missing file — that concern belongs to a different rule).

Inline suppression
------------------
Add ``# conformance: ignore[I001] <reason>`` on the line directly before the
offending instruction.  For file-level findings (I003, when
``ATLAN_APP_MODULE`` is absent), place the directive on the first line of the
Dockerfile.  Justification text after the rule ID is required; a bare
``# conformance: ignore[I001]`` with no text is not accepted.

Known coverage limits (intentional):

* **Multi-stage builds:** all rules (I001–I005) operate only on the final
  stage (instructions after the last ``FROM``).  Builder stages may use any
  base image, CMD/ENTRYPOINT, ENV values, or root USER — Docker discards all
  of those at stage boundary and only the final stage determines the runtime
  container.
* **ENV parsing:** the key=value form and the deprecated space-separated form
  are both recognised.  Heredoc values and dynamically computed values are not
  resolved.
* **Line continuations:** handled for top-level parsing; very long multi-line
  instructions are accumulated correctly.
* **Parser directives** (``# syntax=``, ``# escape=``): not handled — the
  default escape character (``\\``) is assumed.
"""

from __future__ import annotations

import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

from conformance.suite.checks._ast_common import make_cli_main
from conformance.suite.schema.findings import Finding

SERIES = "I"
RULE_I001 = "I001"
RULE_I002 = "I002"
RULE_I003 = "I003"
RULE_I004 = "I004"
RULE_I005 = "I005"

_REQUIRED_BASE = "registry.atlan.com/public/app-runtime-base:3"

__all__ = [
    "SERIES",
    "discover",
    "main",
    "scan_path",
    "scan_text",
]


# ---------------------------------------------------------------------------
# Dockerfile instruction parser
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Instruction:
    keyword: str  # uppercase, e.g. "FROM", "ENV", "USER"
    args: str  # everything after the keyword on the (possibly continued) line
    line: int  # 1-based line number where the keyword starts


def _normalise_text(text: str) -> str:
    """Strip a leading UTF-8 BOM and normalise CRLF/CR line endings to LF.

    Called once in ``scan_text`` so both ``_parse_dockerfile`` and
    ``_parse_directives`` always receive the same normalised string —
    keeping instruction line numbers and directive line numbers in sync.

    Without BOM stripping, a BOM-prefixed first line causes
    ``parts[0].upper()`` to return ``"\\ufeffFROM"`` and all I-series rules
    silently no-op.  Without CRLF normalisation, ``\\r`` leaks into shlex
    tokens, breaking keyword matching and ENV parsing.
    """
    text = text.lstrip("﻿")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _parse_dockerfile(text: str) -> list[_Instruction]:
    """Parse a normalised Dockerfile text into a flat list of instructions.

    Handles line continuations (``\\``) and skips blank lines and comment
    lines.  Parser directives (``# syntax=``) are not handled — the default
    backslash escape character is assumed.

    Expects text already normalised by ``_normalise_text`` (BOM stripped,
    CRLF → LF).
    """
    instructions: list[_Instruction] = []
    raw_lines = text.splitlines()
    i = 0
    while i < len(raw_lines):
        stripped = raw_lines[i].strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue

        start_line = i + 1  # 1-based
        accumulated = stripped
        while accumulated.endswith("\\"):
            accumulated = accumulated[:-1]
            i += 1
            if i < len(raw_lines):
                accumulated = accumulated + " " + raw_lines[i].strip()

        parts = accumulated.split(None, 1)
        if parts:
            keyword = parts[0].upper()
            args = parts[1].strip() if len(parts) > 1 else ""
            instructions.append(
                _Instruction(keyword=keyword, args=args, line=start_line)
            )
        i += 1
    return instructions


# ---------------------------------------------------------------------------
# Inline suppression (Dockerfile-specific)
# ---------------------------------------------------------------------------

# Mirrors the Python _SUPPRESS_RE pattern from _ast_common/_directives.py.
# Justification text is required (bare ignore without reason is rejected).
_SUPPRESS_RE = re.compile(
    r"#\s*conformance\s*:\s*ignore\s*(?:\[([^\]]*)\])?\s*(.*)",
    re.IGNORECASE,
)


def _parse_directives(text: str) -> dict[int, tuple[frozenset[str] | None, str | None]]:
    """Return ``{1-based line_num: (rule_ids or None, justification)}`` for each valid directive.

    A directive without justification text is silently rejected (same policy
    as the Python _directives.py parser) so that bare
    ``# conformance: ignore[I001]`` with no explanation carries no effect.

    All Dockerfile comments are by definition on their own line, so there is
    no "inline vs. comment-only" distinction needed here.
    """
    result: dict[int, tuple[frozenset[str] | None, str | None]] = {}
    for lineno, line in enumerate(text.splitlines(), start=1):
        m = _SUPPRESS_RE.search(line)
        if not m:
            continue
        ids_str = (m.group(1) or "").strip()
        justification = (m.group(2) or "").strip()
        if not justification:
            continue  # reject bare directives without reason text
        rule_ids: frozenset[str] | None = (
            frozenset(r.strip().upper() for r in ids_str.split(",") if r.strip())
            if ids_str
            else None
        )
        result[lineno] = (rule_ids, justification)
    return result


def _make_finding(
    rule_id: str,
    file: str,
    line: int,
    message: str,
    directives: dict[int, tuple[frozenset[str] | None, str | None]],
) -> Finding:
    """Build a Finding, honouring a directive on the preceding comment line.

    Checks line - 1 (the comment line before the instruction) for a
    conformance: ignore directive that names rule_id.  For file-level
    findings placed at line 1 (e.g. I003), also checks line 1 itself so
    that a directive at the top of the file can suppress it.
    """
    suppressed = False
    justification: str | None = None
    check_lines = [line - 1] if line > 1 else []
    check_lines.append(line)  # also check the line itself (covers line-1 findings)
    for check_line in check_lines:
        if check_line in directives:
            rule_ids, just = directives[check_line]
            if rule_ids is None or rule_id in rule_ids:
                suppressed = True
                justification = just
                break
    return Finding(
        rule_id=rule_id,
        file=file,
        line=line,
        column=1,
        message=message,
        suppressed=suppressed,
        suppression_justification=justification,
    )


# ---------------------------------------------------------------------------
# ENV instruction parsing
# ---------------------------------------------------------------------------


def _env_vars(args: str) -> dict[str, str]:
    """Extract ``{key: value}`` from an ENV instruction's argument string.

    Handles the key=value form (including multiple pairs) and the deprecated
    single-pair space-separated form ``ENV KEY VALUE``.
    """
    result: dict[str, str] = {}
    if "=" in args:
        try:
            parts = shlex.split(args)
        except ValueError:
            parts = args.split()
        for part in parts:
            if "=" in part:
                k, _, v = part.partition("=")
                result[k.strip()] = v
    else:
        kv = args.split(None, 1)
        if len(kv) == 2:
            result[kv[0]] = kv[1].strip()
        elif kv:
            result[kv[0]] = ""
    return result


# ---------------------------------------------------------------------------
# Stage detection (for multi-stage build awareness)
# ---------------------------------------------------------------------------


def _final_stage_start_idx(instructions: list[_Instruction]) -> int:
    """Return the index of the first instruction of the final build stage.

    For single-stage builds, returns 0.  For multi-stage builds, returns the
    index of the last FROM instruction so that callers can slice
    ``instructions[final_stage_start_idx:]`` to get only the final stage.
    """
    last = 0
    for i, instr in enumerate(instructions):
        if instr.keyword == "FROM":
            last = i
    return last


# ---------------------------------------------------------------------------
# Per-rule checks
# ---------------------------------------------------------------------------


def _check_i001(
    instructions: list[_Instruction],
    file: str,
    directives: dict[int, tuple[frozenset[str] | None, str | None]],
) -> list[Finding]:
    """I001: final-stage FROM must be ``registry.atlan.com/public/app-runtime-base:3``."""
    final_from: _Instruction | None = None
    for instr in instructions:
        if instr.keyword == "FROM":
            final_from = instr
    if final_from is None:
        return []

    # Strip --platform flag (``=val`` or space-separated form) and AS alias.
    args_clean = re.sub(
        r"--platform(?:=\S+|\s+\S+)\s*", "", final_from.args, flags=re.IGNORECASE
    ).strip()
    image = args_clean.split()[0] if args_clean.split() else ""

    # Accept the canonical base image with or without a digest pin.
    # Digest pinning is strictly stronger than the v3 major tag alone —
    # it still guarantees the same base while adding content-addressability.
    _DIGEST_RE = re.compile(r"@sha256:[a-f0-9]{64}$", re.IGNORECASE)
    if image == _REQUIRED_BASE or (
        image.startswith(_REQUIRED_BASE + "@") and _DIGEST_RE.search(image)
    ):
        return []

    image_lower = image.lower()
    if image_lower.startswith("registry.atlan.com/public/app-runtime-base:"):
        tag = image.split(":")[-1]
        msg = (
            f"FROM uses tag ':{tag}' for app-runtime-base; must be exactly "
            f"'{_REQUIRED_BASE}' (v3 major tag only). "
            "':latest', dev-branch tags, and pinned patch versions are rejected."
        )
    elif "app-runtime-base" in image_lower:
        msg = (
            f"FROM '{image}' references app-runtime-base from the wrong registry "
            f"or path; must be '{_REQUIRED_BASE}'."
        )
    else:
        msg = (
            f"FROM '{image}' is not the approved base image; must be "
            f"'{_REQUIRED_BASE}'. Raw upstream images, cgr.dev images, and "
            "other registries are not permitted for app containers."
        )
    return [_make_finding(RULE_I001, file, final_from.line, msg, directives)]


def _check_i002(
    instructions: list[_Instruction],
    file: str,
    directives: dict[int, tuple[frozenset[str] | None, str | None]],
) -> list[Finding]:
    """I002: CMD and ENTRYPOINT must not be overridden in the final stage.

    Builder-stage CMD/ENTRYPOINT are discarded by Docker at stage boundary
    and must not be flagged.
    """
    final_start = _final_stage_start_idx(instructions)
    findings: list[Finding] = []
    for instr in instructions[final_start:]:
        if instr.keyword in ("CMD", "ENTRYPOINT"):
            msg = (
                f"{instr.keyword} must not be overridden in the app Dockerfile. "
                "The base image (app-runtime-base) co-launches the Dapr sidecar "
                "and handles graceful drain on SIGTERM. Overriding "
                f"{instr.keyword} silently bypasses both, causing in-flight "
                "requests to be dropped during rolling restarts in prod."
            )
            findings.append(_make_finding(RULE_I002, file, instr.line, msg, directives))
    return findings


def _check_i003(
    instructions: list[_Instruction],
    file: str,
    directives: dict[int, tuple[frozenset[str] | None, str | None]],
) -> list[Finding]:
    """I003: ENV ATLAN_APP_MODULE=<module>:<class> must be set in the final stage.

    Docker uses the *last* assignment when the same ENV key is set multiple
    times in a stage.  The check therefore accumulates all occurrences and
    evaluates only the effective (last) value — early-exit on the first match
    would produce false negatives (``good → $UNDEFINED`` passes) and false
    positives (``empty → valid`` fires).

    Rejects the effective value when it is:
    - absent from the final stage entirely (builder-stage ENV doesn't carry over)
    - empty or whitespace-only (``ENV ATLAN_APP_MODULE=" "``)
    - an unresolved build-arg reference (``ENV ATLAN_APP_MODULE=$UNDEFINED_ARG``)
    - not in ``module:Class`` shape
    """
    final_start = _final_stage_start_idx(instructions)

    # Accumulate to find the effective (last) value Docker will use.
    last_value: str | None = None
    last_line: int = 1
    for instr in instructions[final_start:]:
        if instr.keyword == "ENV":
            env = _env_vars(instr.args)
            if "ATLAN_APP_MODULE" in env:
                last_value = env["ATLAN_APP_MODULE"]
                last_line = instr.line

    if last_value is None:
        return [
            _make_finding(
                RULE_I003,
                file,
                1,
                "ENV ATLAN_APP_MODULE is not set. The platform runtime uses this "
                "variable to locate and instantiate the application class "
                "(e.g. 'ENV ATLAN_APP_MODULE=myapp.app:MyApp'). "
                "The container will fail to start without it.",
                directives,
            )
        ]

    value = last_value.strip()
    if not value:
        return [
            _make_finding(
                RULE_I003,
                file,
                last_line,
                "ENV ATLAN_APP_MODULE is set to an empty value. The platform "
                "runtime needs a concrete 'module:ClassName' value to start "
                "(e.g. 'ENV ATLAN_APP_MODULE=myapp.app:MyApp').",
                directives,
            )
        ]

    if value.startswith("$"):
        return [
            _make_finding(
                RULE_I003,
                file,
                last_line,
                f"ENV ATLAN_APP_MODULE='{value}' looks like an unresolved "
                "build-arg reference.  The runtime needs a concrete "
                "'module:ClassName' value, not a shell variable.  Set a "
                "literal value (e.g. 'ENV ATLAN_APP_MODULE=myapp.app:MyApp') "
                "or ensure the ARG default is always defined.",
                directives,
            )
        ]

    # Soft shape check: must contain ':' with non-empty parts on both sides.
    parts = value.split(":", 1)
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        return [
            _make_finding(
                RULE_I003,
                file,
                last_line,
                f"ENV ATLAN_APP_MODULE='{value}' does not follow the required "
                "'module:ClassName' format (e.g. 'myapp.app:MyApp').  "
                "The platform runtime splits on ':' to locate the module and "
                "instantiate the named class.",
                directives,
            )
        ]

    return []


def _check_i004(
    instructions: list[_Instruction],
    file: str,
    directives: dict[int, tuple[frozenset[str] | None, str | None]],
) -> list[Finding]:
    """I004: ENV ATLAN_APP_MODE must not be hardcoded in the final stage.

    Builder-stage ENV does not cross stage boundaries, so only the final
    stage is checked.
    """
    final_start = _final_stage_start_idx(instructions)
    findings: list[Finding] = []
    for instr in instructions[final_start:]:
        if instr.keyword == "ENV":
            env = _env_vars(instr.args)
            if "ATLAN_APP_MODE" in env:
                msg = (
                    "ENV ATLAN_APP_MODE must not be hardcoded in the Dockerfile. "
                    "Runtime mode (worker/server) is deployment-specific; the same "
                    "image may be deployed in different modes in different "
                    "environments. Set ATLAN_APP_MODE in the deployment manifest "
                    "instead of baking it into the image."
                )
                findings.append(
                    _make_finding(RULE_I004, file, instr.line, msg, directives)
                )
    return findings


def _check_i005(
    instructions: list[_Instruction],
    file: str,
    directives: dict[int, tuple[frozenset[str] | None, str | None]],
) -> list[Finding]:
    """I005: USER root or USER 0 must not appear in the final stage.

    The user spec may include a group component (``user:group``), e.g.
    ``USER 0:0``, ``USER root:root``, ``USER root:0``, ``USER 0:1000``.
    Only the user portion (before the first ``:``) is checked so that
    group-qualified root specs are not silently bypassed.
    """
    final_start = _final_stage_start_idx(instructions)
    findings: list[Finding] = []
    for instr in instructions[final_start:]:
        if instr.keyword == "USER":
            token = instr.args.split()[0] if instr.args.split() else ""
            # Split user:group — only the user part determines identity.
            user = token.split(":", 1)[0]
            if user.lower() in ("root", "0"):
                msg = (
                    f"USER {token} is not permitted in the final stage. "
                    "The base image (app-runtime-base) already runs as 'appuser'; "
                    f"'USER {token}' reverses that and exposes the container to "
                    "privilege-escalation risks, violating the non-root policy."
                )
                findings.append(
                    _make_finding(RULE_I005, file, instr.line, msg, directives)
                )
    return findings


# ---------------------------------------------------------------------------
# Public scan API
# ---------------------------------------------------------------------------


def scan_text(text: str, file: str) -> list[Finding]:
    """Scan a Dockerfile and return all I-series findings."""
    text = _normalise_text(text)
    instructions = _parse_dockerfile(text)
    directives = _parse_directives(text)
    findings: list[Finding] = []
    findings.extend(_check_i001(instructions, file, directives))
    findings.extend(_check_i002(instructions, file, directives))
    findings.extend(_check_i003(instructions, file, directives))
    findings.extend(_check_i004(instructions, file, directives))
    findings.extend(_check_i005(instructions, file, directives))
    return findings


def scan_path(path: Path, root: Path) -> list[Finding]:
    """Scan a single Dockerfile, producing repo-root-relative URIs."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    return scan_text(text, str(rel))


def discover(root: Path) -> list[Path]:
    """Discover the Dockerfile at the repo root.

    Returns an empty list when no ``Dockerfile`` is present, so the I-series
    simply no-ops on repos that have not yet added a Dockerfile.
    """
    df = root / "Dockerfile"
    return [df] if df.is_file() else []


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

main = make_cli_main(
    scan_text,
    description="I-series: scan Dockerfile for container image conformance violations.",
    discover=discover,
    default_scan_paths=(".",),
)
"""CLI entry point for the I-series Dockerfile conformance check."""


if __name__ == "__main__":
    sys.exit(main())
