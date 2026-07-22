"""Resolve the SDR config dir + app.yaml for the sdr-e2e composite action.

Adopter apps historically kept their SDR files in ``.github/e2e/`` at the repo
root alongside ``app.yaml``; the newer convention namespaces everything under
``.github/sdr-e2e/`` (with ``app.yaml`` inside it) so connector repos don't get
cluttered with SDR-only files at the root. Both layouts are accepted so adopters
migrate at their own pace. Resolution order:

  1. explicit ``--config-dir`` input (must exist), else
  2. ``.github/sdr-e2e`` if present, else
  3. ``.github/e2e`` if present, else error.

Then ``app.yaml`` inside the resolved dir wins, falling back to a repo-root
``app.yaml``, else error.

Prints two ``KEY=VALUE`` lines (``SDR_CONFIG_DIR`` / ``APP_YAML_PATH``) to stdout,
suitable for ``>> "$GITHUB_ENV"``; diagnostics and ``::error::`` go to stderr.

Mirrors the "Resolve SDR config paths" step in
``.github/actions/sdr-e2e/action.yaml`` — moved here (rather than left as inline
``if/elif/else`` shell) per ``docs/standards/ci.md``, which reserves conditional
logic in ``run:`` blocks for tested Python.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def resolve_paths(config_dir_input: str, *, root: Path = Path(".")) -> tuple[str, str]:
    """Return ``(sdr_config_dir, app_yaml_path)`` as repo-relative strings.

    *config_dir_input* is the raw ``config-dir`` action input (empty = unset).
    *root* is the directory existence checks run against (the connector checkout
    root in CI; a fixture dir in tests); the returned strings stay relative so
    downstream action steps resolve them from the same working directory.

    Raises ``ValueError`` when an explicit ``config-dir`` doesn't exist, when no
    config dir is found under either convention, or when no ``app.yaml`` is
    found in the resolved dir or at the repo root.
    """
    if config_dir_input:
        if not (root / config_dir_input).is_dir():
            raise ValueError(f"config-dir '{config_dir_input}' not found.")
        config_dir = config_dir_input
    elif (root / ".github/sdr-e2e").is_dir():
        config_dir = ".github/sdr-e2e"
    elif (root / ".github/e2e").is_dir():
        config_dir = ".github/e2e"
    else:
        raise ValueError("No SDR config dir found (.github/sdr-e2e or .github/e2e).")

    if (root / config_dir / "app.yaml").is_file():
        app_yaml = f"{config_dir}/app.yaml"
    elif (root / "app.yaml").is_file():
        app_yaml = "app.yaml"
    else:
        raise ValueError(
            f"No app.yaml found at {config_dir}/app.yaml or repo root. "
            "See the SDR onboarding skill for the minimal three-line shape."
        )

    return config_dir, app_yaml


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config-dir",
        default="",
        help="Explicit config-dir input; empty = auto-detect the convention.",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("."),
        help="Directory existence checks run against (default: cwd).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)

    try:
        config_dir, app_yaml = resolve_paths(args.config_dir, root=args.root)
    except ValueError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1

    print(f"Using SDR config dir: {config_dir}", file=sys.stderr)
    print(f"Using app.yaml at:    {app_yaml}", file=sys.stderr)
    print(f"SDR_CONFIG_DIR={config_dir}")
    print(f"APP_YAML_PATH={app_yaml}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
