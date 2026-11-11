"""Render the test fixtures that are toolkit output from their contracts.

A fixture listed in :data:`FIXTURES` is not maintained by hand: it is a
contract rendered by the in-repo ``contract-toolkit`` with the pinned pkl,
keeping only the files its tests read. ``tests/unit/test_rendered_fixtures.py``
renders each one again and compares, so a checked-in copy cannot drift from
what the toolkit emits (FND-4680).

Regenerate after changing a contract or the toolkit::

    uv run poe regen-test-fixtures

To add a fixture, write its contract next to it and add an entry below.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_TOOLKIT = _REPO / "contract-toolkit" / "src"


@dataclass(frozen=True)
class RenderedFixture:
    """A fixture directory that is the render of a contract."""

    contract: Path
    """The contract to render (``app.pkl``)."""

    fixture: Path
    """The checked-in directory holding the kept files."""

    kept: tuple[str, ...]
    """Paths under the render's ``app/generated/`` that the fixture keeps."""


_HANDLER_FIXTURES = _REPO / "tests" / "unit" / "handler" / "fixtures"

FIXTURES: tuple[RenderedFixture, ...] = (
    # Read by tests/unit/handler/test_validate_route_credentials.py.
    RenderedFixture(
        contract=_HANDLER_FIXTURES / "bigquery_contract" / "app.pkl",
        fixture=_HANDLER_FIXTURES / "generated_bigquery_credentials",
        kept=(
            "atlan-connectors-bigquery.json",
            "crawler/bigquery-crawler.json",
            "crawler/manifest.json",
        ),
    ),
)


def render(fixture: RenderedFixture, pkl: str | Path) -> dict[str, bytes]:
    """Render *fixture*'s contract with *pkl* and return the kept files' bytes."""
    with tempfile.TemporaryDirectory() as out:
        subprocess.run(
            [
                str(pkl),
                "eval",
                "--project-dir",
                str(_TOOLKIT),
                "-m",
                out,
                str(fixture.contract),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        generated = Path(out) / "app" / "generated"
        return {name: (generated / name).read_bytes() for name in fixture.kept}


def checked_in(fixture: RenderedFixture) -> dict[str, bytes]:
    """*fixture*'s current bytes, keyed like :func:`render`."""
    return {name: (fixture.fixture / name).read_bytes() for name in fixture.kept}


def main() -> int:
    from application_sdk.dev.pkl import ensure_pkl

    pkl = ensure_pkl()
    for fixture in FIXTURES:
        rendered = render(fixture, pkl)
        if fixture.fixture.exists():
            shutil.rmtree(fixture.fixture)
        for name, data in rendered.items():
            target = fixture.fixture / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        sys.stdout.write(
            f"Wrote {len(rendered)} files to {fixture.fixture.relative_to(_REPO)}\n"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
