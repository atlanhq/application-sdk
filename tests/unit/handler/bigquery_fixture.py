"""Render ``fixtures/generated_bigquery_credentials/`` from its contract (FND-4680).

The fixture is toolkit output: ``fixtures/bigquery_contract/app.pkl`` rendered
by the in-repo ``contract-toolkit`` with the pinned pkl. Only the files the
validate route reads are kept. ``test_bigquery_fixture_freshness.py`` renders
it again and compares, so the checked-in copy cannot drift from what the
toolkit emits.

Regenerate after changing the contract or the toolkit::

    uv run poe regen-test-fixtures
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
_HERE = Path(__file__).resolve().parent
CONTRACT = _HERE / "fixtures" / "bigquery_contract" / "app.pkl"
FIXTURE = _HERE / "fixtures" / "generated_bigquery_credentials"
_TOOLKIT = _REPO / "contract-toolkit" / "src"

# Paths under app/generated/ that the fixture keeps. The rest of the render
# (Python models, e2e scaffolds, atlan.yaml) is not read by the route tests.
KEPT = (
    "atlan-connectors-bigquery.json",
    "crawler/bigquery-crawler.json",
    "crawler/manifest.json",
)


def render(pkl: str | Path) -> dict[str, bytes]:
    """Render the contract with *pkl* and return the kept files' bytes."""
    with tempfile.TemporaryDirectory() as out:
        subprocess.run(
            [
                str(pkl),
                "eval",
                "--project-dir",
                str(_TOOLKIT),
                "-m",
                out,
                str(CONTRACT),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        generated = Path(out) / "app" / "generated"
        return {name: (generated / name).read_bytes() for name in KEPT}


def checked_in() -> dict[str, bytes]:
    """The fixture's current bytes, keyed like :func:`render`."""
    return {name: (FIXTURE / name).read_bytes() for name in KEPT}


def main() -> int:
    from application_sdk.dev.pkl import ensure_pkl

    rendered = render(ensure_pkl())
    if FIXTURE.exists():
        shutil.rmtree(FIXTURE)
    for name, data in rendered.items():
        target = FIXTURE / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    sys.stdout.write(f"Wrote {len(rendered)} files to {FIXTURE.relative_to(_REPO)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
