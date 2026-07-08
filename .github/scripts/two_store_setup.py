#!/usr/bin/env python3
"""Two-store SDR mode setup — the driver behind the sdr-e2e "Build compose -f
chain" step, so the allowlist decision + side effects live in tested Python
rather than inline ``case``/``if`` branching in the action YAML
(docs/standards/ci.md: "no branching logic in YAML").

For an allowlisted connector this runs the FULL SDR flow in CI: extract →
deployment store, then the connector's ``self.upload()`` → UPSTREAM
(atlan-objectstore) store that Publish reads. Both stores are local +
bind-mounted to the host so the asset guards can assert count/location.
Everything is SDK-side — no connector-repo change; adding a connector to
``TWO_STORE_APPS`` below is the only edit needed to onboard it.

Usage (from the composite run step, cwd = runner workspace):
    python3 .github/scripts/two_store_setup.py <app-name> <assets-dir>

  * <app-name>  = ATLAN_APPLICATION_NAME
  * <assets-dir> = the action's two-store/ dir (holds atlan-objectstore.yaml +
                   docker-compose.two-store.yml)
  * cwd              = workspace (holds ci-deploy/components/)
  * GITHUB_WORKSPACE = workspace root for the bind-mount dirs (falls back to cwd)
  * GITHUB_ENV       = file the guard env vars are appended to (skipped if unset)
  * STDOUT           = the extra compose tokens ("-f <overlay>", or nothing when
                       the app isn't a two-store app) — the caller reads them
                       into its compose ``-f`` chain. All diagnostics go to
                       STDERR so they never pollute that chain.
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import sys

#: Connectors that run the two-store SDR flow in CI. Add a connector here to
#: onboard it — no connector-repo change needed.
TWO_STORE_APPS = frozenset(
    {"oracle", "mssql", "mysql", "redshift", "saperp", "tableau", "looker"}
)

#: A Dapr component declaring ``metadata.name: atlan-objectstore`` (matched by
#: name, not filename — the configurator may emit it under any filename).
_ATLAN_OBJECTSTORE_NAME = re.compile(
    r"""^[ \t]*name:[ \t]*["']?atlan-objectstore["']?[ \t]*$""", re.MULTILINE
)


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


def _remove_configurator_atlan_objectstore(components_dir: str) -> None:
    """Remove any configurator-generated component named ``atlan-objectstore``.

    Ours is copied in next; if the configurator's stayed, Dapr would load two
    components with the same name (alongside, not overriding) and routing would
    be undefined. Matched by declared name so a differing filename still hits.
    """
    for path in glob.glob(os.path.join(components_dir, "**"), recursive=True):
        if not os.path.isfile(path):
            continue
        try:
            text = open(path, encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        if _ATLAN_OBJECTSTORE_NAME.search(text):
            os.remove(path)
            _log(f"Removed configurator atlan-objectstore component: {path}")


def setup(
    app_name: str,
    *,
    assets_dir: str,
    components_dir: str,
    workspace: str,
    github_env: str | None,
) -> list[str]:
    """Perform two-store setup for ``app_name``.

    Returns the extra compose tokens to append (``["-f", "<overlay>"]``), or
    ``[]`` when the app isn't a two-store app. Idempotent side effects: replaces
    the upstream component, creates the two host-writable mount dirs, and appends
    the guard env vars to ``github_env``.
    """
    if app_name not in TWO_STORE_APPS:
        return []

    _log(f"Two-store SDR mode enabled for '{app_name}'")

    # 1. Upstream localstorage component (overrides the configurator's unusable
    #    S3-to-tenant atlan-objectstore). Remove any same-named one first, then
    #    copy ours in so it wins.
    _remove_configurator_atlan_objectstore(components_dir)
    os.makedirs(components_dir, exist_ok=True)
    shutil.copy(os.path.join(assets_dir, "atlan-objectstore.yaml"), components_dir)

    # 2. Host-writable mount dirs (containers run as uid 1000; a root-owned bind
    #    mount would be permission-denied to daprd/the app).
    for sub in ("data", "data-upstream"):
        d = os.path.join(workspace, sub)
        os.makedirs(d, exist_ok=True)
        os.chmod(d, 0o777)

    # 3. Enable + point the guards (read host-side by the pytest run; GITHUB_ENV
    #    persists to the "Run connector SDR tests" step). PYTEST_ADDOPTS=-s: the
    #    SDK logs via loguru (stderr), not stdlib, so pytest's log_cli can't
    #    surface the guard confirmations; only -s lets them stream into the
    #    tee'd log so the anti-silent-success check has evidence it ran.
    env_lines = [
        "SDR_REQUIRE_ASSETS_LANDED=true",
        "SDR_REQUIRE_UPSTREAM_ASSETS_LANDED=true",
        f"SDR_EXTRACTED_OUTPUT_BASE_PATH=data/artifacts/apps/{app_name}/workflows",
        f"SDR_UPSTREAM_OUTPUT_BASE_PATH=data-upstream/artifacts/apps/{app_name}/workflows",
        "PYTEST_ADDOPTS=-s",
    ]
    if github_env:
        with open(github_env, "a", encoding="utf-8") as fh:
            fh.write("\n".join(env_lines) + "\n")
    else:
        _log("GITHUB_ENV not set — skipping guard env export (local run?)")

    # 4. Overlay that bind-mounts both stores + pins the SDR upload envs.
    return ["-f", os.path.join(assets_dir, "docker-compose.two-store.yml")]


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    app_name = args[0] if args else ""
    # The action passes its two-store/ dir; fall back to a sibling of this script
    # only for ad-hoc local runs.
    assets_dir = (
        args[1] if len(args) > 1 else os.path.dirname(os.path.abspath(__file__))
    )
    tokens = setup(
        app_name,
        assets_dir=assets_dir,
        components_dir="ci-deploy/components",
        workspace=os.environ.get("GITHUB_WORKSPACE", "."),
        github_env=os.environ.get("GITHUB_ENV"),
    )
    if tokens:
        # STDOUT is the compose-token channel — the caller reads it into its
        # ``-f`` chain. Paths carry no whitespace (simple action-relative paths).
        print(" ".join(tokens))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
