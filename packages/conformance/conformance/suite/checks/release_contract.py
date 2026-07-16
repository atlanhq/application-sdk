"""K011/K012 release-readiness guards — app_id in atlan.yaml + generate poe task.

K011 ``AppIdMissingFromContract``: the generated ``atlan.yaml`` must carry a
top-level ``app_id`` (the Global Marketplace identity the publish step POSTs to
the GM; an empty value returns 404 and the release never reaches the
marketplace).

K012 ``GeneratePoeTaskMissing``: ``pyproject.toml`` must define a ``generate``
poe task (the SDK Certify step runs ``uv run poe generate`` and hard-fails
without it, aborting the publish with ``Unrecognized task 'generate'``).

Both are APP-scoped and gated on the presence of a ``contract/`` directory —
the same "is this a pkl-contract-driven app repo?" signal the sibling K checks
(``legacy_contract``, ``generated_freshness``) use. The SDK repo has no
``contract/`` dir, so the check no-ops there (and the runner's scope filter
drops any K finding on the SDK regardless).

This is a cross-artifact check — it reads two fixed root-level files
(``atlan.yaml``, ``pyproject.toml``), not a discovered per-file set — so it
implements ``scan_all`` and a no-op ``scan_path``, mirroring K006
(``manifest_contract``).
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

from conformance.suite.checks._ast_common import (
    make_cli_main,
    make_toml_finding,
    parse_toml_suppressions,
)
from conformance.suite.schema.findings import Finding

SERIES = "K"

__all__ = ["SERIES", "discover", "main", "scan_all", "scan_path"]

# Top-level ``app_id:`` key in atlan.yaml. A top-level YAML key starts in
# column 0 (no leading whitespace), so ``^`` under re.MULTILINE anchors it; a
# nested ``app_id:`` under some other mapping is not the manifest-level
# identity the publish step reads and must not satisfy the rule.
_APP_ID_RE = re.compile(r"^app_id:[ \t]*\S+", re.MULTILINE)

# ``[tool.poe.tasks]`` table header — used only to anchor the K012 finding on a
# meaningful line; absence just falls back to line 1.
_POE_TASKS_HEADER_RE = re.compile(r"^[ \t]*\[tool\.poe\.tasks\]", re.MULTILINE)


def discover(root: Path) -> list[Path]:
    """Return ``[root]`` for a pkl-contract-driven app repo, else ``[]``.

    A ``contract/`` directory is the same app-repo signal the sibling K checks
    use; the SDK has none, so the check no-ops there. The root (not a file
    set) is returned because the check reads two fixed root-level artifacts.
    """
    return [root] if (root / "contract").is_dir() else []


def _line_of(text: str, pattern: re.Pattern[str], default: int = 1) -> int:
    """1-based line of the first ``pattern`` match in ``text`` (``default`` if none)."""
    match = pattern.search(text)
    if match is None:
        return default
    return text.count("\n", 0, match.start()) + 1


def scan_all(paths: list[Path], root: Path) -> list[Finding]:
    """Emit K011 (missing app_id) and K012 (missing generate poe task).

    No-ops when ``discover`` returned nothing (not a contract-driven app repo).
    """
    if not paths:
        return []

    findings: list[Finding] = []

    # K011 — app_id present in the generated atlan.yaml.
    #
    # Only fires when atlan.yaml exists: a contract repo with no atlan.yaml at
    # all has a *missing generated output* (K004's concern), not a missing
    # app_id, and double-flagging the same regeneration gap would be noise.
    atlan = root / "atlan.yaml"
    if atlan.is_file():
        text = atlan.read_text(encoding="utf-8")
        if _APP_ID_RE.search(text) is None:
            findings.append(
                make_toml_finding(
                    rule_id="K011",
                    file="atlan.yaml",
                    line=1,
                    column=1,
                    message=(
                        "atlan.yaml declares no top-level 'app_id'. The marketplace "
                        "publish step POSTs app_id to the Global Marketplace; an "
                        "empty value returns 404 and the released version never "
                        'appears. Add ["app_id"] to the metadata block in '
                        "contract/app.pkl and regenerate (uv run poe generate)."
                    ),
                    # atlan.yaml uses '#' comments, so the shared TOML/# directive
                    # scanner applies unchanged.
                    suppressions=parse_toml_suppressions(text),
                )
            )

    # K012 — generate poe task in pyproject.toml.
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        text = pyproject.read_text(encoding="utf-8")
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            # A malformed pyproject.toml is not this rule's concern (the
            # dependency/coverage checks and the build itself surface it); treat
            # it as "no tasks declared" so we neither crash nor false-negative.
            data = {}
        tool = data.get("tool")
        poe = tool.get("poe") if isinstance(tool, dict) else None
        tasks = poe.get("tasks") if isinstance(poe, dict) else None
        if not isinstance(tasks, dict) or "generate" not in tasks:
            findings.append(
                make_toml_finding(
                    rule_id="K012",
                    file="pyproject.toml",
                    line=_line_of(text, _POE_TASKS_HEADER_RE),
                    column=1,
                    message=(
                        "pyproject.toml defines no [tool.poe.tasks.generate] task. "
                        "The SDK Certify step runs 'uv run poe generate' and "
                        "hard-fails without it, aborting the marketplace publish "
                        "(Unrecognized task 'generate'). Add a generate task "
                        "mirroring the Makefile target."
                    ),
                    suppressions=parse_toml_suppressions(text),
                )
            )

    return findings


def scan_path(path: Path, root: Path) -> list[Finding]:  # noqa: ARG001
    """No-op: K011/K012 are cross-artifact; use :func:`scan_all`."""
    return []


main = make_cli_main(
    scan_all=scan_all,
    discover=discover,
    description=(
        "K011/K012 release-readiness: atlan.yaml app_id + generate poe task presence."
    ),
)


if __name__ == "__main__":
    sys.exit(main())
