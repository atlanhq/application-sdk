"""The example in the concept doc is executed here, so it cannot drift.

`docs/concepts/apps.md` is where an app author meets this package. An example
that no longer constructs — a renamed argument, a validation rule it violates —
is worse than no example, because it is read as the supported shape.
"""

from __future__ import annotations

import pathlib
import re

import pytest

DOC = pathlib.Path(__file__).resolve().parents[3] / "docs" / "concepts" / "apps.md"


def _size_tier_code_blocks() -> list[str]:
    """Every ```python block under the doc's "### Size tiers" heading."""
    text = DOC.read_text(encoding="utf-8")
    start = text.index("### Size tiers")
    end = text.find("\n## ", start)
    section = text[start : end if end != -1 else len(text)]
    return re.findall(r"```python\n(.*?)```", section, re.DOTALL)


@pytest.fixture(scope="module")
def blocks() -> list[str]:
    found = _size_tier_code_blocks()
    assert found, "the Size tiers section lost its examples"
    return found


def test_the_documented_ladder_constructs(blocks: list[str]) -> None:
    """It must satisfy the same validation every app's ladder does."""
    namespace: dict[str, object] = {}
    exec(blocks[0], namespace)  # noqa: S102 - executing our own documentation
    assert "LADDER" in namespace


def test_the_documented_ladder_routes_the_way_the_prose_says(blocks: list[str]) -> None:
    """Small input stays on the floor; a file-heavy one climbs."""
    namespace: dict[str, object] = {}
    exec(blocks[0], namespace)  # noqa: S102 - executing our own documentation
    ladder = namespace["LADDER"]

    assert ladder.tier_for("transform", {"files": 1_000}) == "s"
    assert ladder.tier_for("transform", {"files": 20_000}) == "m"
    assert ladder.tier_for("transform", {"files": 500_000}) == "l"
    # Nothing measured: the floor, never a guess upwards.
    assert ladder.tier_for("transform") == "s"


def test_every_name_the_dispatch_example_uses_is_exported(blocks: list[str]) -> None:
    """The second block is prose-shaped, so check its symbols rather than run it."""
    import application_sdk.tiering as pkg

    for name in ("queue_map_from_env", "resolve", "run_on_queue"):
        assert name in blocks[1], f"the dispatch example stopped showing {name}"
        assert getattr(pkg, name) is not None
