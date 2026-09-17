"""Cross-file guard: a GitHub Actions cache export must never fail a build.

BuildKit's `type=gha` cache exporter writes layer blobs to the Actions cache
service at the end of a build. That service intermittently refuses the write:

    #15 ERROR: error writing layer blob: failed to reserve cache
    ERROR: failed to build: failed to solve: error writing layer blob:
           failed to reserve cache

By default buildx treats that as a build failure, so a transient on a *cache
write* — after the image has already been built — reddens whatever check the
build feeds. On `build-and-scan.yaml` that check is the Security Gate, which is
required on every baselined app repo, so the flake parks an otherwise
auto-mergeable PR until a human re-runs the job.

`ignore-error=true` on the export is the whole fix: the cache is an
optimisation, and losing an entry costs a slower build, never a red one. It has
no effect on `cache-from` — a read miss is already non-fatal — so this guard
deliberately covers exports only.

Discovery is textual rather than YAML-structural because the exports are not all
in the same shape: most are a `cache-to:` key on `docker/build-push-action`, one
is a `--cache-to` flag inside a `run:` block, and one is the `default:` of a
composite action's `cache-to` input. A structural walk would miss the last two.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GHA_CACHE = "type=gha"
IGNORE_ERROR = "ignore-error=true"


def _yaml_files() -> list[Path]:
    files = sorted((ROOT / "workflows").glob("*.y*ml"))
    files += sorted((ROOT / "actions").glob("*/action.y*ml"))
    return files


def _rel(path: Path) -> str:
    return str(path.relative_to(ROOT))


def is_cache_export(line: str) -> bool:
    """Is this line configuring a gha cache EXPORT (as opposed to an import)?

    Two independent signals, either of which is enough:

    * the line names `cache-to` — the explicit form, whether as a YAML key or a
      `--cache-to` flag; or
    * the line carries `mode=`, which only an export accepts. This is what
      catches the shape with no `cache-to` on it at all: a composite action's
      `default: 'type=gha,mode=min'`, which is an export destination named
      several lines above the value.

    Neither fires on an import: `cache-from: type=gha,scope=x` has no `mode=`
    and does not say `cache-to`.

    Comment lines are prose about the exporter, not the exporter — and the
    surrounding files explain this setting at length, so matching them would
    make the guard fail on its own rationale.
    """
    if GHA_CACHE not in line:
        return False
    if line.lstrip().startswith("#"):
        return False
    return "cache-to" in line or "mode=" in line


def _exports() -> list[tuple[str, int, str]]:
    found = []
    for path in _yaml_files():
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if is_cache_export(line):
                found.append((_rel(path), number, line.strip()))
    return found


def test_the_guard_actually_finds_cache_exports():
    """A scan that silently matched nothing would pass the assertion below."""
    exports = _exports()
    assert (
        len(exports) >= 8
    ), f"cache-export discovery collapsed — found only {len(exports)}: {exports}"


def test_every_gha_cache_export_ignores_errors():
    bad = [
        f"{rel}:{number}  {line}"
        for rel, number, line in _exports()
        if IGNORE_ERROR not in line
    ]
    assert not bad, (
        f"Every `{GHA_CACHE}` cache export must carry `{IGNORE_ERROR}`, so a "
        "cache-service write failure degrades the build instead of failing "
        "it:\n  " + "\n  ".join(bad)
    )


@pytest.mark.parametrize(
    "line",
    [
        "          cache-to: type=gha,mode=max,scope=scan,ignore-error=true",
        '          --cache-to "type=gha,mode=max,scope=sdr-app,ignore-error=true" \\',
        "    default: 'type=gha,mode=min,ignore-error=true'",
    ],
)
def test_the_matcher_recognises_an_export(line: str):
    assert is_cache_export(line)


@pytest.mark.parametrize(
    "line",
    [
        "          cache-from: type=gha,scope=${{ matrix.platform }}",
        "          cache-from: type=gha",
        '          --cache-from "type=gha,scope=sdr-app" \\',
        "    default: 'type=gha'",
        "          cache-to: ${{ inputs.cache-to }}",  # indirection, not a gha value
        "        cache-to: type=registry,ref=example.com/cache",  # not the gha exporter
        "        # `cache-from/cache-to: type=gha` plugs buildx into the cache",
        "          # cache-to: type=gha,mode=max  (disabled for now)",
    ],
)
def test_the_matcher_ignores_a_non_gha_export_or_an_import(line: str):
    assert not is_cache_export(line)
