"""T016 — e2e-full CI compose overlay must inherit ATLAN_DEPLOYMENT_NAME.

The full-DAG e2e worker derives its Temporal task queue as
``atlan-{ATLAN_APPLICATION_NAME}-{ATLAN_DEPLOYMENT_NAME}`` and the harness
(``BaseE2ETest.agent_spec``) derives the extract-node queue it dispatches to
from the *same* two env vars. To keep worker and harness on one queue when the
e2e suite fans out across parallel matrix legs, the SDK's ``sdr-e2e`` composite
action derives a **per-leg** ``ATLAN_DEPLOYMENT_NAME`` (base +
sanitised matrix-leg suffix, e.g. ``e2e-full-ci-<run_id>-mysql-full-dag``) and
exports it to ``$GITHUB_ENV`` — see ``sdr-e2e/derive_deployment_name.py``. Both
sides then read that one value.

A connector's e2e compose overlay under ``.github/`` breaks that contract when
it **hard-codes** ``ATLAN_DEPLOYMENT_NAME`` in a service's ``environment``
(e.g. ``ATLAN_DEPLOYMENT_NAME=e2e-full-ci-${GITHUB_RUN_ID}``): the explicit
compose value overrides the runner env, so the worker container drops the leg
suffix and registers on ``atlan-mysql-e2e-full-ci-<run_id>`` while the harness
still dispatches to ``atlan-mysql-e2e-full-ci-<run_id>-<leg>``. Two different
queues → "No Workers Running" and the top-level AE run hangs until timeout
(observed on atlan-mysql-app before this rule existed).

* ``T016`` — E2EDeploymentNameNotInherited: an e2e CI compose overlay assigns
  ``ATLAN_DEPLOYMENT_NAME`` to a literal that does not reference the inherited
  ``${ATLAN_DEPLOYMENT_NAME...}`` env var. The compliant forms are a
  pass-through list entry (``- ATLAN_DEPLOYMENT_NAME``, no ``=``) or an
  inherit-with-fallback interpolation
  (``- ATLAN_DEPLOYMENT_NAME=${ATLAN_DEPLOYMENT_NAME:-e2e-full-ci-${GITHUB_RUN_ID}}``).

Discovery
---------
Scans every ``*.yml``/``*.yaml`` under ``.github/`` that (a) looks like a
docker-compose file (a top-level ``services:`` key) and (b) mentions
``ATLAN_DEPLOYMENT_NAME`` at all. This deliberately targets CI compose overlays
(``.github/e2e/…``) and skips GitHub *workflow* files (which have no
``services:`` block) and local-dev compose files at the repo root (where
hard-coding ``ATLAN_DEPLOYMENT_NAME=local`` is legitimate).

Inline suppression
------------------
Add ``# conformance: ignore[T016] <reason>`` on the assignment's line (or the
comment-only line directly above it) — the same directive grammar as every
other series; YAML's ``#`` comments slot in naturally (see
``_ast_common.parse_toml_suppressions``).

Known limits (intentional — biased toward zero false positives):

* Only string-literal assignments in the flat compose ``environment`` block are
  inspected. A value built by a compose ``x-`` anchor / YAML alias, or one
  injected via ``env_file``, is not statically visible and is not flagged.
* The presence of *any* ``${ATLAN_DEPLOYMENT_NAME`` reference in the value is
  treated as inheriting — the check does not attempt to prove the fallback
  default is itself well-formed.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from conformance.suite.checks._ast_common import (
    make_cli_main,
    make_toml_finding,
    parse_toml_suppressions,
)
from conformance.suite.schema.findings import Finding

SERIES = "T"
RULE_T016 = "T016"

_ENV_VAR = "ATLAN_DEPLOYMENT_NAME"
# The inherited-env reference that makes an assignment compliant. Matches both
# ${ATLAN_DEPLOYMENT_NAME} and ${ATLAN_DEPLOYMENT_NAME:-<default>}.
_INHERIT_MARKER = "${" + _ENV_VAR

# A docker-compose file declares a top-level ``services:`` key (column 0). This
# distinguishes a compose overlay from a GitHub workflow YAML, which never does.
_SERVICES_RE = re.compile(r"^services:\s*(#.*)?$", re.MULTILINE)

# List form:  - ATLAN_DEPLOYMENT_NAME[=<value>]   (optionally whole-quoted)
_LIST_RE = re.compile(
    rf"""^\s*-\s*['"]?{re.escape(_ENV_VAR)}
        (?:=(?P<val>.*?))?          # optional =<value>; absent => pass-through
        ['"]?\s*$""",
    re.VERBOSE,
)
# Mapping form:  ATLAN_DEPLOYMENT_NAME: <value>
_MAP_RE = re.compile(rf"^\s*{re.escape(_ENV_VAR)}\s*:\s*(?P<val>.*?)\s*$")

__all__ = ["SERIES", "discover", "main", "scan_path", "scan_text"]


def _is_compose_file(text: str) -> bool:
    return _SERVICES_RE.search(text) is not None


def _assignment(line: str) -> tuple[str, str] | None:
    """Return ``(form, value)`` for an ``ATLAN_DEPLOYMENT_NAME`` line, else None.

    ``form`` is ``"list"`` (``- ATLAN_DEPLOYMENT_NAME=<value>``) or ``"map"``
    (``ATLAN_DEPLOYMENT_NAME: <value>``). Returns ``None`` when the line is not
    an ``ATLAN_DEPLOYMENT_NAME`` assignment, or is the bare pass-through list
    form (``- ATLAN_DEPLOYMENT_NAME`` with no ``=``) which inherits the value
    from the runner env and is always compliant. The form matters because a
    null-valued *map* entry (``ATLAN_DEPLOYMENT_NAME: null``) is a compose
    pass-through, whereas a *list* ``=null`` is a literal string assignment.
    """
    m = _LIST_RE.match(line)
    if m is not None:
        val = m.group("val")
        return None if val is None else ("list", val)  # no '=' => pass-through
    m = _MAP_RE.match(line)
    if m is not None:
        return ("map", m.group("val"))
    return None


def _clean_value(value: str) -> str:
    """Strip a trailing inline comment and surrounding quotes from *value*.

    Deployment-name values never legitimately contain ``#``, so a ``#`` marks
    the start of a YAML comment.
    """
    idx = value.find("#")
    if idx != -1:
        value = value[:idx]
    return value.strip().strip("'\"").strip()


# YAML null scalars. In a docker-compose ``environment`` MAP, a null value
# (``ATLAN_DEPLOYMENT_NAME: null`` / ``: ~``) means "pass the variable through
# from the host/runner env" — i.e. inherit, the same semantics as the bare
# pass-through list form. (In LIST form, ``=null`` is a literal string "null"
# and is correctly flagged.)
_YAML_NULL_TOKENS: frozenset[str] = frozenset({"null", "~"})


def _is_inherited(value: str) -> bool:
    return _INHERIT_MARKER in value


def _message() -> str:
    return (
        f"e2e CI compose overlay hard-codes {_ENV_VAR} instead of inheriting the "
        f"per-leg value the sdr-e2e action exports to $GITHUB_ENV. The explicit "
        f"compose value overrides the runner env, so the worker registers on "
        f"atlan-<app>-<deployment> without the matrix-leg suffix while the harness "
        f"(BaseE2ETest.agent_spec) dispatches the extract activity to the "
        f"leg-suffixed queue — two different Temporal queues, so no worker polls "
        f"the harness's queue and the run hangs until timeout. Inherit the derived "
        f"value with a local-dev fallback: "
        f"{_ENV_VAR}=${{{_ENV_VAR}:-e2e-full-ci-${{GITHUB_RUN_ID}}}} "
        f"(or a bare pass-through list entry, '- {_ENV_VAR}')."
    )


def scan_text(text: str, file: str) -> list[Finding]:
    """Scan one compose-overlay *text* for T016 findings."""
    if not _is_compose_file(text):
        return []
    if _ENV_VAR not in text:
        return []

    suppressions = parse_toml_suppressions(text)
    findings: list[Finding] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        assignment = _assignment(line)
        if assignment is None:
            continue
        form, value = assignment
        cleaned = _clean_value(value)
        if not cleaned:
            # Empty value behaves like a pass-through: inherits the runner env.
            continue
        if form == "map" and cleaned.lower() in _YAML_NULL_TOKENS:
            # A null-valued compose env MAP entry passes the var through from the
            # runner env (inherit) — same as the bare list pass-through form.
            continue
        if _is_inherited(cleaned):
            continue
        findings.append(
            make_toml_finding(
                rule_id=RULE_T016,
                file=file,
                line=lineno,
                column=1,
                message=_message(),
                suppressions=suppressions,
            )
        )
    return findings


def discover(root: Path) -> list[Path]:
    """Discover e2e CI compose overlays under ``.github/``.

    A candidate is a ``*.yml``/``*.yaml`` file that both looks like a
    docker-compose file (top-level ``services:``) and mentions
    ``ATLAN_DEPLOYMENT_NAME`` — overlays that never touch the var inherit it
    automatically and have nothing to grade.
    """
    gh = root / ".github"
    if not gh.is_dir():
        return []
    candidates = sorted(gh.rglob("*.yml")) + sorted(gh.rglob("*.yaml"))
    paths: list[Path] = []
    for path in candidates:
        if "__pycache__" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if _ENV_VAR in text and _is_compose_file(text):
            paths.append(path)
    return sorted(paths)


def scan_path(path: Path, root: Path) -> list[Finding]:
    """Scan a single compose-overlay file for T016 findings."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    return scan_text(text, str(rel))


main = make_cli_main(
    scan_text,
    description=(
        "T016: e2e CI compose overlays must inherit ATLAN_DEPLOYMENT_NAME from "
        "the sdr-e2e per-leg derivation, not hard-code it."
    ),
    discover=discover,
    default_scan_paths=(".github",),
)
"""CLI entry point for the T016 e2e-deployment-name check."""


if __name__ == "__main__":
    sys.exit(main())
