"""Guard: only the lock-refresh lane waits for a green branch before its PR opens (FND-3517).

``prCreation: status-success`` keeps a ``window-empty`` lock refusal from ever
becoming a PR, at the cost of opening good refreshes one fleet pass (~4h) late.
That delay is acceptable for the unattended lock refresh and never for a
first-party release, so the setting must stay confined to ``lockFileMaintenance``.
"""

from __future__ import annotations

import json
from pathlib import Path

_PRESET = Path(__file__).resolve().parents[3] / "renovate-config/default.json"
_GATED = ("prCreation", "internalChecksAsSuccess")


def _preset() -> dict:
    return json.loads(_PRESET.read_text())


def test_lock_lane_opens_its_pr_only_on_green() -> None:
    lane = _preset()["lockFileMaintenance"]
    assert lane["prCreation"] == "status-success"
    # Without this, a branch whose only green status is renovate/artifacts reads
    # as pending and no good refresh ever opens.
    assert lane["internalChecksAsSuccess"] is True


def test_gate_is_not_set_fleet_wide() -> None:
    preset = _preset()
    assert not any(key in preset for key in _GATED)


def test_no_package_rule_inherits_the_gate() -> None:
    # Covers the atlan framework dependencies rules: first-party releases must
    # keep the default prCreation (immediate) and reach the fleet in minutes.
    for rule in _preset()["packageRules"]:
        assert not any(key in rule for key in _GATED), rule
