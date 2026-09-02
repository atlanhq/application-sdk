"""Who reviews this PR, and how deep — owned by this lane.

The routing used until now lived as a markdown table in the *other* lane's
playbook, mirrored into a Python dict, with a drift check parsing the markdown
to keep the two in step. That arrangement made `pr-review/ORCHESTRATION.md` the
author of this lane's behaviour: restructuring or retiring it would silently
change who reviews what, and a redesign cannot claim to have replaced a document
it still takes instructions from.

`agents.yaml` is this lane's own copy of that decision. The old table is left
alone and continues to govern the sandbox lane.

The scope buckets survive the move because they encode real knowledge about this
repository that no principle replaces. `depth` is new: the old table routed on
scope alone, so a 40-line change and a 4,000-line change in the same directory
drew an identical review. See the data file for the inspection numbers.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PATH = (
    Path(__file__).resolve().parents[2] / ".mothership/pr-loop/data/agents.yaml"
)

#: Conditions an `also` entry may carry. Anything else is a typo that would
#: silently never fire, so the loader refuses it rather than dispatching one
#: specialist fewer than intended for the rest of the lane's life.
CONDITIONS = frozenset({"always", "when_touches_config", "when_touches_conformance"})

SINGLE_PASS = "single_pass"
PER_MODULE = "per_module"
MODES = frozenset({SINGLE_PASS, PER_MODULE})


class RoutingError(ValueError):
    """The routing data is malformed or incomplete."""


@dataclass(frozen=True)
class Route:
    scope: str
    agents: tuple[str, ...]
    also: dict[str, str]

    def resolve(
        self, *, touches_config: bool, touches_conformance: bool
    ) -> tuple[str, ...]:
        """The specialists to dispatch, in a stable order.

        Order is `agents` then `also`, deduplicated, because the dispatch order
        shows up in logs and a set would make it vary between runs for no
        reason.
        """
        out = list(self.agents)
        for name, condition in self.also.items():
            fires = (
                condition == "always"
                or (condition == "when_touches_config" and touches_config)
                or (condition == "when_touches_conformance" and touches_conformance)
            )
            if fires and name not in out:
                out.append(name)
        return tuple(out)


@dataclass(frozen=True)
class Depth:
    max_changed_lines: int | None
    mode: str


@dataclass(frozen=True)
class Routing:
    routes: dict[str, Route]
    depth: tuple[Depth, ...]

    def route(self, scope: str) -> Route:
        try:
            return self.routes[scope]
        except KeyError:
            raise RoutingError(
                f"no route for scope {scope!r}. A scope the classifier can emit "
                "but the routing does not cover dispatches nobody and returns a "
                "verdict over an unreviewed diff."
            ) from None

    def mode_for(self, changed_lines: int) -> str:
        """First rule whose ceiling the diff fits under wins."""
        for rule in self.depth:
            if (
                rule.max_changed_lines is None
                or changed_lines <= rule.max_changed_lines
            ):
                return rule.mode
        raise RoutingError("depth ladder has no catch-all rule")


def load_routing(path: Path | str | None = None) -> Routing:
    raw: dict[str, Any] = (
        yaml.safe_load(Path(path or DEFAULT_PATH).read_text(encoding="utf-8")) or {}
    )

    routes: dict[str, Route] = {}
    for scope, entry in (raw.get("routes") or {}).items():
        also = entry.get("also") or {}
        for name, condition in also.items():
            if condition not in CONDITIONS:
                raise RoutingError(
                    f"{scope}: `also.{name}` has condition {condition!r}, which is "
                    f"not one of {sorted(CONDITIONS)} — it would never fire."
                )
        if not (entry.get("why") or "").strip():
            raise RoutingError(
                f"{scope}: every route states why it dispatches what it does. A "
                "route nobody can justify is one nobody can safely change."
            )
        routes[scope] = Route(
            scope=scope, agents=tuple(entry.get("agents") or ()), also=dict(also)
        )

    depth: list[Depth] = []
    for rule in raw.get("depth") or ():
        mode = rule.get("mode")
        if mode not in MODES:
            raise RoutingError(f"depth mode {mode!r} is not one of {sorted(MODES)}")
        depth.append(Depth(max_changed_lines=rule.get("max_changed_lines"), mode=mode))
    if not depth or depth[-1].max_changed_lines is not None:
        raise RoutingError(
            "the depth ladder needs a final rule with max_changed_lines: null — "
            "without it a large enough diff matches nothing and gets no review."
        )

    return Routing(routes=routes, depth=tuple(depth))
