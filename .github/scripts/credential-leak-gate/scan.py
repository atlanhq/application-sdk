#!/usr/bin/env python3
"""Deterministic credential-leak gate (no LLM).

Scans a local working tree for credentials reaching log / print / format /
CLI-arg sinks — the APP-1602 audit class. This is the deterministic mirror of
the nightly `credential-leak-scan` (atlanhq/connector-pulse): same sink pattern
set and severity rubric (see credential-leak-rules.md in this directory), but
the LLM triage/adversarial stages are replaced by static heuristics so the gate
runs synchronously in the publish path with no token and no network.

Detection model
---------------
1. Regex sweep: each line of each source file (extension allowlist) is matched
   against the documented sink patterns after inline-comment stripping.
2. Static triage (replaces the LLM): a hit is only treated as a real leak when
   the credential identifier is referenced *as code* — interpolated, passed as
   a variable, or concatenated — rather than appearing only inside a log
   message string literal. Lines that route the value through a masking helper
   (mask()/redact()/*** etc.) are dropped. Findings in test/fixture paths are
   capped at MEDIUM. An optional `.credential-leak-allow` file suppresses
   confirmed false positives.

Output
------
Writes a verdict JSON (same shape the publish gate consumes) and exits:
  - 0  -> no blocking (CRITICAL/HIGH) findings  (decision="pass")
  - 1  -> one or more blocking findings         (decision="fail")
Any uncaught exception propagates as a non-zero exit, so the gate fails closed.

Secret values are NEVER recorded or printed — only file, line, pattern id,
matched identifier, and severity.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict, dataclass

# ── Ruleset (mirrors credential-leak-rules.md — keep in sync) ──────────────────

# Credential identifier class. The named group `var` is the matched identifier.
CRED = (
    r"(?P<var>password|passwd|pwd|secret|api[_-]?key|access[_-]?key|"
    r"secret[_-]?key|client[_-]?secret|atlan[_-]?token|atlan[_-]?api[_-]?token|"
    r"bearer|authorization|credential|connection[_-]?string|private[_-]?key)"
)

# (pattern_id, sink_type, regex template). `{cred}` expands to CRED.
_SINKS: list[tuple[str, str, str]] = [
    (
        "logger-call",
        "logger",
        r"(log(ger)?|logging)\.(debug|info|warn(ing)?|error|critical|fatal|trace)\s*\(.*{cred}",
    ),
    ("print-call", "print", r"(^|[^A-Za-z0-9_.])print\s*\(.*{cred}"),
    ("console-call", "console", r"console\.(log|info|debug|warn|error)\s*\(.*{cred}"),
    (
        "go-fmt-call",
        "go-fmt",
        r"(fmt|log)\.(Print|Println|Printf|Errorf|Fatalf|Panicf)\s*\(.*{cred}",
    ),
    (
        "rust-println",
        "rust-macro",
        r"(println!|eprintln!|log::(debug|info|warn|error)!)\s*\(.*{cred}",
    ),
    # NOTE: the nightly scan keeps a standalone `fstring-interp` sink and lets
    # the LLM confirm it reaches a real sink. This deterministic gate omits it:
    # an f-string interpolating a credential is only a leak when it reaches a
    # log/print/CLI sink, and those interpolations are already caught inside the
    # logger-call / print-call / console-call / go-fmt-call patterns above
    # (whose `.*{cred}` matches the interpolated form). Keeping it standalone
    # flags benign value construction (e.g. building a basic-auth header).
    ("shell-echo", "shell", r"(echo|printf)\s.*\$\{?[A-Z_]*{cred}[A-Z_]*\}?"),
    ("helm-set", "cli-arg", r"(--set[= ]|--from-literal[= ])\S*{cred}\S*="),
]
SINKS = [
    (pid, stype, re.compile(tmpl.replace("{cred}", CRED), re.IGNORECASE))
    for pid, stype, tmpl in _SINKS
]

CRED_BARE = re.compile(CRED, re.IGNORECASE)

# When the matched credential token is part of a larger identifier denoting
# metadata (a name / id / guid / reference / etc.), the value is not the secret
# itself — e.g. `credential_name`, `secret_id`, `credential_guid`. Tested
# against the characters immediately following the matched token.
META_SUFFIX = re.compile(
    r"^(s|es)?[_-]?(name|names|id|ids|guid|uuid|ref|refs|reference|arn|type|types|"
    r"prefix|suffix|count|len|length|url|uri|urls|path|paths|file|files|field|fields|"
    r"list|map|store|stores|provider|providers|source|sources|kind|key_name|"
    r"expiry|expires|expiration|created|updated|at|hash|fingerprint|format|"
    r"template|var|vars|env|enabled|present|exists|status|state)\b",
    re.IGNORECASE,
)

# Identifiers that, when leaked, are a full credential -> CRITICAL.
FULL_CRED = {
    "password",
    "passwd",
    "pwd",
    "secret",
    "secretkey",
    "clientsecret",
    "atlantoken",
    "atlanapitoken",
    "privatekey",
    "credential",
    "connectionstring",
}

EXTS = {
    ".py",
    ".go",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".java",
    ".kt",
    ".rs",
    ".sh",
    ".yaml",
    ".yml",
}
HASH_COMMENT = {".py", ".sh", ".yaml", ".yml"}
SLASH_COMMENT = {".go", ".ts", ".tsx", ".js", ".jsx", ".java", ".kt", ".rs"}

# Directories never worth scanning.
SKIP_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
    "vendor",
    ".mypy_cache",
    ".pytest_cache",
    "__pycache__",
    ".ruff_cache",
    "site-packages",
    ".tox",
}

TEST_PATH = re.compile(
    r"(^|/)(tests?|fixtures?|testdata|e2e)(/|$)|"
    r"(^|/)(test_[^/]*|[^/]*_test|conftest)\.[^/]+$|"
    r"\.(spec|test)\.[^/]+$",
    re.IGNORECASE,
)

# Masking/redaction helpers — if present in the line, the value is already
# protected before it reaches the sink (FP classes #4 / #5).
REDACT = re.compile(
    r"(mask|redact|obfuscat|scrub|sanitiz|hidden|\*\*\*|\[redacted\]|<redacted>|x{4,})",
    re.IGNORECASE,
)

# String literals (single/double quoted, non-greedy). Used to decide whether a
# credential identifier is referenced as code vs only inside a message string.
STRING_LIT = re.compile(r"""(['"]).*?\1""")
# f-string / template-literal interpolation of a credential identifier.
INTERP = re.compile(r"""[{$]\s*[^{}]*""" + CRED + r"""[^{}]*[}]?""", re.IGNORECASE)


@dataclass
class Finding:
    id: str
    file: str
    line: int
    pattern_id: str
    variable_name: str
    sink_type: str
    severity: str
    verdict_triage: str
    verdict_gate: str
    reason: str


def _strip_comment(line: str, ext: str) -> str:
    """Remove a trailing inline comment so words in comments don't fire."""
    if ext in HASH_COMMENT:
        idx = line.find("#")
        return line[:idx] if idx != -1 else line
    if ext in SLASH_COMMENT:
        # Avoid eating the `//` in scheme://host.
        m = re.search(r"(?<!:)//", line)
        return line[: m.start()] if m else line
    return line


def _referenced_as_code(line: str) -> bool:
    """True when the credential identifier is used as a value/variable, not just
    as a word inside a log-message string literal."""
    if INTERP.search(line):
        return True
    # Strip quoted string literals; if the identifier still appears, it is being
    # referenced as code (a variable, kwarg, concatenation, CLI arg, ...).
    stripped = STRING_LIT.sub("", line)
    return CRED_BARE.search(stripped) is not None


def _norm(var: str) -> str:
    return re.sub(r"[_-]", "", var).lower()


def _severity(var_norm: str, pattern_id: str, is_test: bool) -> str:
    if pattern_id == "helm-set":
        return "MEDIUM"
    if is_test:
        return "MEDIUM"
    if var_norm in FULL_CRED:
        return "CRITICAL"
    return "HIGH"


def _load_allowlist(root: str) -> tuple[set[str], set[str]]:
    """Return (ignored_files, ignored_file_lines) from `.credential-leak-allow`.

    Entries: `path` ignores a whole file; `path:lineno` ignores one line;
    blank lines and `#` comments are skipped.
    """
    files: set[str] = set()
    lines: set[str] = set()
    path = os.path.join(root, ".credential-leak-allow")
    if not os.path.isfile(path):
        return files, lines
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            entry = raw.strip()
            if not entry or entry.startswith("#"):
                continue
            if re.search(r":\d+$", entry):
                lines.add(entry)
            else:
                files.add(entry)
    return files, lines


def _iter_source_files(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if os.path.splitext(name)[1].lower() in EXTS:
                full = os.path.join(dirpath, name)
                yield full, os.path.relpath(full, root)


def scan(root: str) -> dict:
    ignored_files, ignored_lines = _load_allowlist(root)
    findings: list[Finding] = []
    files_scanned = 0
    candidates = 0
    counter = 0

    for full, rel in _iter_source_files(root):
        if rel in ignored_files:
            continue
        ext = os.path.splitext(rel)[1].lower()
        is_test = bool(TEST_PATH.search(rel))
        try:
            with open(full, encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        except (OSError, UnicodeError):
            continue
        files_scanned += 1
        for lineno, raw in enumerate(lines, start=1):
            code = _strip_comment(raw.rstrip("\n"), ext)
            if not CRED_BARE.search(code):
                continue
            for pattern_id, sink_type, rx in SINKS:
                m = rx.search(code)
                if not m:
                    continue
                candidates += 1
                if f"{rel}:{lineno}" in ignored_lines:
                    break
                if REDACT.search(code):
                    break  # value masked before the sink — not a leak
                var = m.groupdict().get("var") or ""
                var_norm = _norm(var)
                # Metadata, not a secret value: `credential_name`, `secret_id`,
                # `credential_guid`, ... (suffix immediately after the token).
                if META_SUFFIX.search(code[m.end("var") :]):
                    break
                # Static triage: only a real leak if referenced as code.
                if pattern_id not in (
                    "shell-echo",
                    "helm-set",
                ) and not _referenced_as_code(code):
                    break
                counter += 1
                sev = _severity(var_norm, pattern_id, is_test)
                findings.append(
                    Finding(
                        id=f"CL-{counter:04d}",
                        file=rel,
                        line=lineno,
                        pattern_id=pattern_id,
                        variable_name=var,
                        sink_type=sink_type,
                        severity=sev,
                        verdict_triage="LEAK",
                        verdict_gate="survived",
                        reason=(
                            f"credential identifier '{var}' reaches a {sink_type} "
                            f"sink ({pattern_id})"
                            + (" in a test/fixture path" if is_test else "")
                        ),
                    )
                )
                break  # one finding per line is enough

    sev_count = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for f in findings:
        sev_count[f.severity] = sev_count.get(f.severity, 0) + 1
    blocking = sev_count["CRITICAL"] + sev_count["HIGH"]

    return {
        "scan_root": root,
        "stages": {
            "regex_scan": {"files_scanned": files_scanned, "candidates": candidates},
            "triage": {"leaks": len(findings)},
        },
        "findings": [asdict(f) for f in findings],
        "summary": {
            "total_findings": len(findings),
            "critical": sev_count["CRITICAL"],
            "high": sev_count["HIGH"],
            "medium": sev_count["MEDIUM"],
            "low": sev_count["LOW"],
            "blocking": blocking,
        },
        "decision": "fail" if blocking > 0 else "pass",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Deterministic credential-leak gate")
    ap.add_argument("--root", required=True, help="Directory to scan")
    ap.add_argument("--report", required=True, help="Verdict JSON output path")
    args = ap.parse_args()

    verdict = scan(args.root)
    with open(args.report, "w", encoding="utf-8") as fh:
        json.dump(verdict, fh, indent=2)

    s = verdict["summary"]
    print(
        f"Scanned {verdict['stages']['regex_scan']['files_scanned']} files, "
        f"{verdict['stages']['regex_scan']['candidates']} candidate lines."
    )
    print(
        f"Findings: {s['total_findings']} "
        f"({s['critical']} critical, {s['high']} high, "
        f"{s['medium']} medium, {s['low']} low) — "
        f"{s['blocking']} blocking. Decision: {verdict['decision']}."
    )
    for f in verdict["findings"]:
        if f["severity"] in ("CRITICAL", "HIGH"):
            print(
                f"  [{f['severity']}] {f['file']}:{f['line']} "
                f"({f['pattern_id']}, var={f['variable_name']})"
            )
    return 1 if verdict["decision"] == "fail" else 0


if __name__ == "__main__":
    sys.exit(main())
