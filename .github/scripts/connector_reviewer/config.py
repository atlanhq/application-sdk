"""connector-reviewer configuration, read from `.github/connector-reviewer/config.toml`.

That file ships with this package in application-sdk, not in the repo under
review, so one config serves the whole fleet and no app repo can retune it.

Every number that decides cost or convergence lives here, so retuning is a
one-line PR with no code change. The config is hashed into the PR state:
a config change (like a model change) forces a full re-review instead of
silently mixing verdicts from two different reviewers.
"""

from __future__ import annotations

import hashlib
import math
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .agent import AgentLimits
from .exempt import ExemptConfig
from .llm import Price

TRUSTED_APPS = (
    "atlan-app-fleet",
    "atlan-conformance-sync",
    "connectivity-ai",
    "mothership-ai",
    "atlan-connectivity-release-train",
)


@dataclass
class Config:
    model: str = "gpt-6-luna"
    reasoning_effort: str | None = "max"
    api: str = "responses"
    price: Price = field(default_factory=lambda: Price(0.0, 0.0, 0.0))
    cap_usd_per_pr: float = 1.0
    first_round_share: float = 0.6
    max_rounds: int = 5
    later_round_min_severity: str = "high"
    max_bundles: int = 8
    concurrency: int = 4
    max_changed_files: int = 60
    reflect: bool = True
    verify: bool = True
    approach: bool = True
    preflight: bool = True
    exclude: list[str] = field(default_factory=list)
    # GitHub Apps whose `/review` comments are honoured (event.trusted_app).
    trusted_apps: list[str] = field(default_factory=lambda: list(TRUSTED_APPS))
    # Which pull requests are not reviewed at all (exempt.classify).
    exempt: ExemptConfig = field(default_factory=ExemptConfig)
    limits: AgentLimits = field(default_factory=AgentLimits)
    raw_hash: str = ""


def load_config(config_dir: Path) -> Config:
    path = config_dir / "config.toml"
    raw = path.read_bytes() if path.exists() else b""
    data = tomllib.loads(raw.decode()) if raw else {}
    cfg = Config()
    m = data.get("model", {})
    cfg.model = m.get("name", cfg.model)
    cfg.reasoning_effort = m.get("reasoning_effort", cfg.reasoning_effort) or None
    cfg.api = m.get("api", cfg.api)
    p = m.get("price", {})
    cfg.price = Price(
        input_per_mtok=float(p.get("input_per_mtok", 0.0)),
        cached_input_per_mtok=float(
            p.get("cached_input_per_mtok", p.get("input_per_mtok", 0.0))
        ),
        output_per_mtok=float(p.get("output_per_mtok", 0.0)),
        cache_write_per_mtok=(
            float(p["cache_write_per_mtok"]) if "cache_write_per_mtok" in p else None
        ),
    )
    b = data.get("budget", {})
    cfg.cap_usd_per_pr = float(b.get("cap_usd_per_pr", cfg.cap_usd_per_pr))
    cfg.first_round_share = float(b.get("first_round_share", cfg.first_round_share))
    r = data.get("rounds", {})
    cfg.max_rounds = int(r.get("max_rounds", cfg.max_rounds))
    cfg.later_round_min_severity = r.get(
        "later_round_min_severity", cfg.later_round_min_severity
    )
    s = data.get("scope", {})
    cfg.max_bundles = int(s.get("max_bundles", cfg.max_bundles))
    cfg.concurrency = int(s.get("concurrency", cfg.concurrency))
    cfg.max_changed_files = int(s.get("max_changed_files", cfg.max_changed_files))
    cfg.exclude = list(s.get("exclude", []))
    st = data.get("stages", {})
    cfg.reflect = bool(st.get("reflect", cfg.reflect))
    cfg.verify = bool(st.get("verify", cfg.verify))
    cfg.approach = bool(st.get("approach", cfg.approach))
    cfg.preflight = bool(st.get("preflight", cfg.preflight))
    access = data.get("access", {})
    cfg.trusted_apps = [str(x) for x in access.get("trusted_apps", cfg.trusted_apps)]
    ex = data.get("exempt", {})
    cfg.exempt = ExemptConfig(
        **{
            k: [str(x) for x in v]
            for k, v in ex.items()
            if k in ExemptConfig.__dataclass_fields__
        }
    )
    a = data.get("agent", {})
    cfg.limits = AgentLimits(
        **{k: int(v) for k, v in a.items() if k in AgentLimits.__dataclass_fields__}
    )
    cfg.raw_hash = hashlib.sha1(raw).hexdigest()[:10]
    return cfg


def validate(cfg: Config) -> list[str]:
    """Refuse to run on a config that cannot enforce its own cap."""
    errs: list[str] = []
    prices = (
        cfg.price.input_per_mtok,
        cfg.price.output_per_mtok,
    )
    # `nan <= 0` is false, so a NaN price would pass a plain comparison and
    # then pass every budget check too.
    if any(not math.isfinite(p) or p <= 0 for p in prices):
        errs.append(
            "model.price.input_per_mtok / output_per_mtok must be set: the $ cap is enforced from them."
        )
    cache_prices = (cfg.price.cached_input_per_mtok, cfg.price.cache_write_per_mtok)
    if any(p is not None and (not math.isfinite(p) or p < 0) for p in cache_prices):
        errs.append(
            "model.price.cached_input_per_mtok / cache_write_per_mtok must be finite and not negative."
        )
    if not (math.isfinite(cfg.cap_usd_per_pr) and 0 < cfg.cap_usd_per_pr <= 5):
        errs.append("budget.cap_usd_per_pr must be in (0, 5].")
    if not 0 < cfg.first_round_share <= 1:
        errs.append("budget.first_round_share must be in (0, 1].")
    return errs
