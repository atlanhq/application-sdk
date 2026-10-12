"""Load .github/vuln-triage/config.toml."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_PATH = ".github/vuln-triage/config.toml"


@dataclass(frozen=True)
class Config:
    label: str = "vuln-auto-merge"
    added_by: str = "@atlan-app-fleet (vuln-triage)"
    stale_after_days: int = 730
    bump_enabled: bool = True
    cooldown_days: int = 7


def load(root: Path) -> Config:
    path = root / CONFIG_PATH
    if not path.exists():
        return Config()
    data = tomllib.loads(path.read_text())
    pr = data.get("pr", {})
    upstream = data.get("upstream", {})
    bump = data.get("bump", {})
    default = Config()
    return Config(
        label=pr.get("label", default.label),
        added_by=pr.get("added_by", default.added_by),
        stale_after_days=int(
            upstream.get("stale_after_days", default.stale_after_days)
        ),
        bump_enabled=bool(bump.get("enabled", default.bump_enabled)),
        cooldown_days=int(bump.get("cooldown_days", default.cooldown_days)),
    )
