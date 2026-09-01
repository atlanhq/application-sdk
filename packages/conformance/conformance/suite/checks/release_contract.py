"""K011/K012/K014 release-readiness guards — atlan.yaml keys + generate poe task.

K011 ``AppIdMissingFromContract``: the generated ``atlan.yaml`` must carry a
top-level ``app_id`` (the Global Marketplace identity the publish step POSTs to
the GM; an empty value returns 404 and the release never reaches the
marketplace).

K012 ``GeneratePoeTaskMissing``: ``pyproject.toml`` must define a ``generate``
poe task (the SDK Certify step runs ``uv run poe generate`` and hard-fails
without it, aborting the publish with ``Unrecognized task 'generate'``).

K014 ``ReleaseModelUndeclared``: ``atlan.yaml`` must declare a top-level
``release_model`` with an allowed value. A missing key is read as ``cd``, so
omitting it silently opts the app into publish-to-all-tenants on every merge.
The rule takes no side between ``cd`` and ``semver`` — it only requires the
choice to be written down.

All three are APP-scoped and gated on the presence of a ``contract/`` directory —
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
    safe_read_text,
)
from conformance.suite.schema.findings import Finding

SERIES = "K"

__all__ = ["SERIES", "discover", "main", "scan_all", "scan_path"]

# Top-level ``app_id:`` key in atlan.yaml. A top-level YAML key starts in
# column 0 (no leading whitespace), so ``^`` under re.MULTILINE anchors it; a
# nested ``app_id:`` under some other mapping is not the manifest-level
# identity the publish step reads and must not satisfy the rule. YAML permits
# horizontal whitespace before the mapping colon. The value is captured so a
# present-but-empty/null value can be rejected too (see ``_app_id_missing``).
_APP_ID_RE = re.compile(r"^app_id[ \t]*:[ \t]*(.*)$", re.MULTILINE)

# YAML values that carry a non-whitespace token yet still POST an empty/None
# identity to the Global Marketplace and hit the same 404 K011 exists to
# prevent: empty quotes and the YAML null literals. A present ``app_id`` set to
# any of these is treated as missing.
_EMPTY_APP_ID_VALUES = frozenset({'""', "''", "null", "Null", "NULL", "~"})

# ``[tool.poe.tasks]`` table header — used only to anchor the K012 finding on a
# meaningful line; absence just falls back to line 1.
_POE_TASKS_HEADER_RE = re.compile(r"^[ \t]*\[tool\.poe\.tasks\]", re.MULTILINE)

# Top-level ``release_model:`` key in atlan.yaml, anchored in column 0 for the
# same reason as ``_APP_ID_RE``: only a manifest-level key is what the publish
# step reads. A nested ``release_model:`` under some other mapping must not
# satisfy the rule — an indentation-blind match reports an app as compliant
# when the publish step still sees nothing.
_RELEASE_MODEL_RE = re.compile(r"^release_model:[ \t]*(.*)$", re.MULTILINE)

# Values the publish step accepts (``parse_atlan_yaml.py``). ``versioned`` is a
# deprecated alias normalised to ``semver`` on read — still honoured, so it is
# reported for migration rather than treated as broken.
_VALID_RELEASE_MODELS = frozenset({"cd", "semver", "versioned"})
_DEPRECATED_RELEASE_MODELS = frozenset({"versioned"})


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


def _app_id_missing(text: str) -> bool:
    """True when atlan.yaml carries no usable top-level ``app_id``.

    Fires on a dropped key (the observed regression), a bare ``app_id:`` with no
    value, and a present-but-empty value (``""``, ``''``, or a YAML null literal
    ``null``/``Null``/``NULL``/``~``) — each POSTs an empty/None identity to the
    Global Marketplace and returns the same 404.
    """
    match = _APP_ID_RE.search(text)
    if match is None:
        return True
    value = match.group(1).strip()
    return not value or value in _EMPTY_APP_ID_VALUES


def _release_model_problem(text: str) -> str | None:
    """Describe why atlan.yaml's ``release_model`` is unusable, else ``None``.

    Returns a sentence completing "atlan.yaml ..." so the caller can build one
    message. Three failure shapes, all of which leave the app on a release model
    nobody chose or one the publish step rejects:

    * key absent — read as the ``cd`` default (``parse_atlan_yaml.py``), i.e.
      publish-to-all-tenants on every merge
    * key present with no value (bare ``release_model:`` or a YAML null) — the
      key exists, so ``d.get("release_model", "cd")`` returns ``None`` and the
      publish step *rejects* it (``None not in _ALLOWED_RELEASE_MODELS``)
    * value outside the allowed set — the publish step errors out
    * ``versioned`` — a deprecated alias for ``semver``; honoured, but worth
      migrating before the alias is dropped

    An explicit ``cd`` is accepted: the rule requires a declared choice, not a
    particular one.

    ``_RELEASE_MODEL_RE.search`` takes the *first* top-level ``release_model:``,
    while ``yaml.safe_load`` keeps the *last* of a duplicated key — a duplicated
    key can pass conformance while the publish step reads the later value. That
    is pathological (a duplicate top-level key is its own bug), so the first
    match is treated as the declared value.
    """
    match = _RELEASE_MODEL_RE.search(text)
    if match is None:
        return (
            "declares no top-level 'release_model'. A missing key is read as "
            "'cd', so every merge to main publishes this app to channel='all'"
        )

    # Strip an inline comment before evaluating: `semver  # why` is a valid
    # declaration, and the comment is not part of the value.
    value = match.group(1).split("#", 1)[0].strip().strip("\"'")
    if not value or value in {"null", "Null", "NULL", "~"}:
        return (
            "declares 'release_model' with no value. The key exists, so the "
            "publish step reads it as None and rejects it (None is not an "
            "allowed value) — give it an explicit model"
        )
    if value not in _VALID_RELEASE_MODELS:
        # Advertise only the non-deprecated values — pointing a fix at
        # ``versioned`` would trade one finding for another.
        allowed = ", ".join(sorted(_VALID_RELEASE_MODELS - _DEPRECATED_RELEASE_MODELS))
        return (
            f"declares release_model '{value}', which the publish step rejects. "
            f"Allowed values: {allowed}"
        )
    if value in _DEPRECATED_RELEASE_MODELS:
        return (
            f"declares release_model '{value}', a deprecated alias for 'semver' "
            "that is normalised on read. Declare 'semver' directly"
        )
    return None


def scan_all(paths: list[Path], root: Path) -> list[Finding]:
    """Emit K011 (app_id), K014 (release_model) and K012 (generate poe task).

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
        text = safe_read_text(atlan) or ""
        if _app_id_missing(text):
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

        # K014 — release_model declared, with an allowed value.
        #
        # Gated on the same "atlan.yaml exists" condition as K011: a contract
        # repo with no atlan.yaml has a missing generated output (K004's
        # concern), not an undeclared release model.
        problem = _release_model_problem(text)
        if problem is not None:
            findings.append(
                make_toml_finding(
                    rule_id="K014",
                    file="atlan.yaml",
                    line=_line_of(text, _RELEASE_MODEL_RE),
                    column=1,
                    message=(
                        f"atlan.yaml {problem}. Declare the intended model "
                        "explicitly ('cd' or 'semver'): in the pkl metadata "
                        "block and regenerate if the contract emits atlan.yaml, "
                        "otherwise directly in atlan.yaml."
                    ),
                    suppressions=parse_toml_suppressions(text),
                )
            )

    # K012 — generate poe task in pyproject.toml.
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        text = safe_read_text(pyproject) or ""
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
        # A present but empty ``generate`` (e.g. ``generate = ""`` or an empty
        # task table) is not a runnable target, so ``uv run poe generate`` still
        # fails — treat a falsy value the same as an absent key.
        if not isinstance(tasks, dict) or not tasks.get("generate"):
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
    """No-op: K011/K012/K014 are cross-artifact; use :func:`scan_all`."""
    return []


main = make_cli_main(
    scan_all=scan_all,
    discover=discover,
    description=(
        "K011/K012/K014 release-readiness: atlan.yaml app_id + release_model, "
        "generate poe task presence."
    ),
)


if __name__ == "__main__":
    sys.exit(main())
