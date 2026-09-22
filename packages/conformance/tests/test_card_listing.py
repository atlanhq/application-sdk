"""Tests for K022 CardDescriptionMissing and K023 CardIconBlank.

Both are cross-artifact listing guards driven by ``scan_all``:

* K022 fires when the generated ``atlan.yaml`` supplies no card text — no
  top-level ``short_description`` and no entrypoint ``description``.
* K023 fires when ``icon_url`` is absent or empty, at app or entrypoint level.

Test helpers
------------
``_app_repo``: scaffolds a minimal app repo under ``tmp_path`` — a ``contract/``
dir (the discover guard) plus an optional ``atlan.yaml`` body — then returns the
``scan_all`` findings for it.
"""

from __future__ import annotations

from pathlib import Path

from conformance.suite.checks.card_listing import discover, scan_all
from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import EnforcementTier, RuleScope
from conformance.suite.schema.findings import Finding

_ICON = "https://assets.atlan.com/assets/metabase.svg"

_ATLAN_COMPLETE = (
    "name: metabase\n"
    "app_id: 019e3a59-89d2-7310-acea-e8eae494483f\n"
    "display_name: Metabase Assets\n"
    f"icon_url: {_ICON}\n"
    "short_description: Crawl Metabase collections, dashboards and questions\n"
)

_ATLAN_NO_DESCRIPTION = (
    f"name: metabase\ndisplay_name: Metabase Assets\nicon_url: {_ICON}\n"
)

_ATLAN_BLANK_DESCRIPTION = f'name: metabase\nicon_url: {_ICON}\nshort_description: ""\n'

_ATLAN_ENTRYPOINT_DESCRIPTION = (
    "name: metabase\n"
    f"icon_url: {_ICON}\n"
    "entrypoints:\n"
    "- name: metabase\n"
    "  description: Crawl Metabase collections\n"
    f"  icon_url: {_ICON}\n"
    "type: connector\n"
)

_ATLAN_ENTRYPOINT_NO_DESCRIPTION = (
    "name: metabase\n"
    f"icon_url: {_ICON}\n"
    "entrypoints:\n"
    "- name: metabase\n"
    "  display_name: Metabase Assets\n"
    f"  icon_url: {_ICON}\n"
    "type: connector\n"
)

_ATLAN_BLANK_ICON = (
    "name: metabase\nicon_url:\nshort_description: Crawl Metabase collections\n"
)

_ATLAN_NO_ICON = "name: metabase\nshort_description: Crawl Metabase collections\n"

_ATLAN_BLANK_ENTRYPOINT_ICON = (
    "name: metabase\n"
    f"icon_url: {_ICON}\n"
    "short_description: Crawl Metabase collections\n"
    "entrypoints:\n"
    "- name: metabase\n"
    "  icon_url:\n"
    "type: connector\n"
)


def _app_repo(
    tmp_path: Path, *, contract: bool = True, atlan: str | None = None
) -> list[Finding]:
    """Scaffold a repo under *tmp_path* and return its ``scan_all`` findings."""
    if contract:
        (tmp_path / "contract").mkdir()
        (tmp_path / "contract" / "app.pkl").write_text('name = "x"\n', encoding="utf-8")
    if atlan is not None:
        (tmp_path / "atlan.yaml").write_text(atlan, encoding="utf-8")
    return scan_all(discover(tmp_path), tmp_path)


def _ids(findings: list[Finding]) -> list[str]:
    """Rule IDs of unsuppressed findings (matching runner gate semantics)."""
    return [f.rule_id for f in findings if not f.suppressed]


# ---------------------------------------------------------------------------
# Rule metadata
# ---------------------------------------------------------------------------


def test_k022_rule_metadata() -> None:
    rule = get_rule("K022")
    assert rule.name == "CardDescriptionMissing"
    assert rule.tier is EnforcementTier.WARN
    assert rule.scope is RuleScope.APP
    assert rule.category == "contract-toolkit"


def test_k023_rule_metadata() -> None:
    rule = get_rule("K023")
    assert rule.name == "CardIconBlank"
    assert rule.tier is EnforcementTier.WARN
    assert rule.scope is RuleScope.APP
    assert rule.category == "contract-toolkit"


# ---------------------------------------------------------------------------
# discover gating
# ---------------------------------------------------------------------------


def test_no_contract_dir_is_not_an_app_repo(tmp_path: Path) -> None:
    assert discover(tmp_path) == []
    assert _app_repo(tmp_path, contract=False, atlan=_ATLAN_NO_DESCRIPTION) == []


def test_missing_atlan_yaml_is_k004s_concern(tmp_path: Path) -> None:
    assert _ids(_app_repo(tmp_path)) == []


# ---------------------------------------------------------------------------
# K022 — card description
# ---------------------------------------------------------------------------


def test_k022_silent_when_short_description_present(tmp_path: Path) -> None:
    assert "K022" not in _ids(_app_repo(tmp_path, atlan=_ATLAN_COMPLETE))


def test_k022_fires_when_no_description_anywhere(tmp_path: Path) -> None:
    assert "K022" in _ids(_app_repo(tmp_path, atlan=_ATLAN_NO_DESCRIPTION))


def test_k022_fires_on_empty_short_description(tmp_path: Path) -> None:
    assert "K022" in _ids(_app_repo(tmp_path, atlan=_ATLAN_BLANK_DESCRIPTION))


def test_k022_satisfied_by_entrypoint_description(tmp_path: Path) -> None:
    assert "K022" not in _ids(_app_repo(tmp_path, atlan=_ATLAN_ENTRYPOINT_DESCRIPTION))


def test_k022_fires_when_entrypoints_carry_no_description(tmp_path: Path) -> None:
    findings = _app_repo(tmp_path, atlan=_ATLAN_ENTRYPOINT_NO_DESCRIPTION)
    assert "K022" in _ids(findings)


# ---------------------------------------------------------------------------
# K023 — card icon
# ---------------------------------------------------------------------------


def test_k023_silent_when_icon_present(tmp_path: Path) -> None:
    assert "K023" not in _ids(_app_repo(tmp_path, atlan=_ATLAN_COMPLETE))


def test_k023_fires_on_valueless_icon_url(tmp_path: Path) -> None:
    assert "K023" in _ids(_app_repo(tmp_path, atlan=_ATLAN_BLANK_ICON))


def test_k023_fires_when_icon_url_absent(tmp_path: Path) -> None:
    assert "K023" in _ids(_app_repo(tmp_path, atlan=_ATLAN_NO_ICON))


def test_k023_fires_on_blank_entrypoint_icon(tmp_path: Path) -> None:
    assert "K023" in _ids(_app_repo(tmp_path, atlan=_ATLAN_BLANK_ENTRYPOINT_ICON))


def test_k023_silent_when_entrypoint_icon_set(tmp_path: Path) -> None:
    assert "K023" not in _ids(_app_repo(tmp_path, atlan=_ATLAN_ENTRYPOINT_DESCRIPTION))


# ---------------------------------------------------------------------------
# Suppression + aggregation
# ---------------------------------------------------------------------------


def test_both_rules_fire_together(tmp_path: Path) -> None:
    assert sorted(_ids(_app_repo(tmp_path, atlan="name: metabase\n"))) == [
        "K022",
        "K023",
    ]


def test_directive_suppresses_finding(tmp_path: Path) -> None:
    atlan = "# conformance: ignore[K022] card text lives on the GM row\n" + (
        _ATLAN_NO_DESCRIPTION
    )
    assert "K022" not in _ids(_app_repo(tmp_path, atlan=atlan))
