"""K019 FormKeyMissingFromManifestArgs — check implementation.

A form key declared in ``contract/app.pkl``'s ``uiConfig`` block that has no
matching ``{{form-key}}`` placeholder anywhere in the generated manifests is
inert twice over, and the second symptom is the one that misroutes the bug:

1. **It never reaches the run.** The Automation Engine substitutes ``{{...}}``
   only for keys present in the args template; a key with no placeholder never
   lands in ``workflow_args``, so the field falls back to its default.
2. **It never persists.** The connection's ``DefaultParameters`` are populated
   *from* the manifest args template — fields absent from the template are not
   stored, and the frontend rehydrate strips them on the next Update. The user
   sets the value, saves, reopens the form, and it is gone. That reads as a
   frontend bug, which is why this class gets filed against the wrong team.

Measured on ``atlan-snowflake-app`` (WARE-1323): five of six regex form keys
were exposed in the crawler form but missing from the args template. The gap
survived roughly six months because every layer worked in isolation and nothing
pinned the chain together.

Both sides are text-extractable, so this rule needs no ``pkl eval``: the left
side is ``["<key>"] = new Config.<Widget>`` inside the ``uiConfig`` block, the
right side is any ``{{<key>}}`` in a generated ``manifest.json``.

The direction is **form key → placeholder only**. A placeholder with no form key
(``{{credential}}``, ``{{agent-json}}``) is an SDK-injected system arg, not a
user-facing form field, and is never reported.
"""

from __future__ import annotations

import re
from pathlib import Path

from conformance.suite.checks.entrypoint_alignment._contract_entrypoints import (
    scan_contract as scan_contract_entrypoints,
)
from conformance.suite.schema.findings import Finding

from ..legacy_contract._directives_pkl import (
    _make_pkl_finding_suppressed,
    _parse_pkl_directives,
)
from ._manifest_refs import manifest_paths_for_contract

_RULE_ID = "K019"

_CONTRACT_PKL = "contract/app.pkl"

# ["some-form-key"] = new Config.<Widget>
_FORM_KEY_RE = re.compile(
    r'\[\s*"(?P<key>[A-Za-z0-9][A-Za-z0-9_-]*)"\s*\]\s*=\s*new\s+Config\.(?P<widget>\w+)'
)

# Widgets that carry no value for the workflow, so having no ``{{...}}`` arg is
# correct rather than a defect. Derived by cross-tabulating every widget type in
# the fleet against whether its key is wired (34 apps, 2026-08-31):
#
# * ``InfoBanner`` — presentational (title/content/bannerType), 0 wired of 1.
# * ``Sage`` / ``SageV2`` — the preflight-check runner. Its checks execute in the
#   UI and the DAG carries a single canonical ``preflight_check`` arg, so apps
#   legitimately declare several UIRule-selected variants
#   (``preflight-check-with-tags``, ``preflight-check-account-usage``, …) that
#   share it. Counting each variant as unwired reported 7 findings across two
#   apps, all of them by-design.
#
# Every other widget type in the fleet is either always wired or mixed with a
# real defect behind the unwired cases, so none of them are excluded.
_NON_VALUE_WIDGETS = frozenset({"InfoBanner", "Sage", "SageV2"})

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")

_UI_CONFIG_RE = re.compile(r"^\s*uiConfig\b.*?\{", re.MULTILINE)


def _ui_config_span(source: str) -> tuple[int, int] | None:
    """Return the ``[start, end)`` character span of the ``uiConfig { … }`` block.

    Brace-matched rather than "from ``uiConfig`` to EOF" so the rule stays
    correct if a contract ever declares another block after ``uiConfig``.
    Returns ``None`` when there is no ``uiConfig`` block or its braces are
    unbalanced — both mean "nothing to check".
    """
    m = _UI_CONFIG_RE.search(source)
    if m is None:
        return None
    start = m.end() - 1  # position of the opening brace
    depth = 0
    for i in range(start, len(source)):
        ch = source[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return (start, i + 1)
    return None


def _declared_form_keys(source: str) -> dict[str, int]:
    """Return ``{form-key: 1-based line}`` for every value-bearing ``uiConfig`` widget.

    Only the *first* declaration of a key is recorded — a key repeated across
    two wizard tasks is one form field and should yield one finding. Widgets in
    :data:`_NON_VALUE_WIDGETS` are skipped: they hold nothing the workflow needs,
    so an absent placeholder is correct.
    """
    span = _ui_config_span(source)
    if span is None:
        return {}
    start, end = span
    keys: dict[str, int] = {}
    for m in _FORM_KEY_RE.finditer(source, start, end):
        if m.group("widget") in _NON_VALUE_WIDGETS:
            continue
        key = m.group("key")
        if key not in keys:
            keys[key] = source.count("\n", 0, m.start()) + 1
    return keys


def _manifest_placeholders(root: Path, contract) -> set[str] | None:  # noqa: ANN001
    """Union of every ``{{...}}`` placeholder across the app's manifests.

    Scans the raw manifest text rather than only the ``extract`` node's args: a
    form key may legitimately be wired into a downstream node instead (athena
    threads ``{{connection}}`` into the publish node's ``connection_entity``),
    and reporting that as missing would be a false positive.

    Returns ``None`` when no manifest could be read at all — the app is not in a
    state this rule can judge.
    """
    found: set[str] = set()
    read_any = False
    for path in manifest_paths_for_contract(root, contract):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        read_any = True
        found.update(m.group(1) for m in _PLACEHOLDER_RE.finditer(text))
    return found if read_any else None


def scan_all(paths: list[Path], root: Path) -> list[Finding]:  # noqa: ARG001
    """Report ``uiConfig`` form keys with no placeholder in any manifest.

    No-ops when ``contract/app.pkl`` is absent or declares no ``uiConfig``, when
    the P016 contract scan finds no entrypoints, or when no manifest is
    readable — all conservative, matching the package's WARN posture.
    """
    pkl_path = root / _CONTRACT_PKL
    try:
        source = pkl_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    form_keys = _declared_form_keys(source)
    if not form_keys:
        return []

    contract = scan_contract_entrypoints(root)
    if contract.mode == "absent":
        return []

    placeholders = _manifest_placeholders(root, contract)
    if placeholders is None:
        return []

    directives = _parse_pkl_directives(source)
    findings: list[Finding] = []

    for key, line in sorted(form_keys.items(), key=lambda kv: kv[1]):
        if key in placeholders:
            continue
        suppressed, justification = _make_pkl_finding_suppressed(
            rule_id=_RULE_ID, line=line, directives=directives
        )
        findings.append(
            Finding(
                rule_id=_RULE_ID,
                file=_CONTRACT_PKL,
                line=line,
                column=1,
                message=(
                    f"Form key {key!r} is declared in uiConfig but no generated "
                    f"manifest.json references '{{{{{key}}}}}'. The Automation "
                    "Engine substitutes only keys present in the args template, so "
                    "the value never reaches the run — and because the connection's "
                    "DefaultParameters are populated from that same template, it is "
                    "never persisted either: the form loses the value on the next "
                    "Update (WARE-1323). Wire the key into the extract node's args "
                    "in contract/app.pkl and regenerate with "
                    "`pkl eval -m . contract/app.pkl`, or remove the widget if the "
                    "field is genuinely unused. Never hand-edit the generated "
                    "manifest.json. Suppress with "
                    f"'// conformance: ignore[{_RULE_ID}] <reason>'."
                ),
                suppressed=suppressed,
                suppression_justification=justification,
                discriminator=key,
            )
        )

    return findings
