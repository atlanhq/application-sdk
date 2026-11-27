"""The common-app-server half of a hosted api app's e2e run (FND-5377).

A hosted api app (``deploy.routeToCommonAPIServer: true`` in atlan.yaml) serves
its routes from common-app-server. Its e2e runs them on a host carrying this
PR's code without touching the shared host: the run builds a host image
(common-app-server's ``scripts/e2e_host_build.py``), installs it as the
e2e-only host ``common-api-server-e2e`` under a lease every hosted app shares,
and installs the app with ``routeToCommonAPIServerOnTargetedRelease`` naming
that host, so LM routes only this app to it.

Subcommands, each printing ``key=value`` lines for ``$GITHUB_OUTPUT``:

``detect``
    ``hosted=true|false`` from atlan.yaml in the cwd.
``image``
    ``image=`` and ``version=`` for the host build. The tag names the app, its
    commit and the host commit, so a re-run of the same pair converges and a
    newer host commit installs again.
``hosted-repos``
    ``repos=`` the repo names of the hosted apps in the host's pins file, for a
    token that reads their runs: a waiter checks whether the run holding the
    shared lease is still alive.
``e2e-host``
    ``app_id=`` of the e2e host's marketplace app, from ``e2e-host.toml`` in
    the common-app-server checkout: the host keeps it in its own private repo,
    as every app keeps its id in its own ``atlan.yaml``.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path
from typing import cast

#: common-app-server's own repo and GM app id (its atlan.yaml ``app_id``).
HOST_REPO = "atlanhq/common-app-server"
HOST_IMAGE = "ghcr.io/atlanhq/common-app-server"
#: The lease every hosted app's run takes on a tenant, beside its own.
HOST_LEASE_APP = "common-app-server"

ROUTE_FLAG = "routeToCommonAPIServer"

_SHA = re.compile(r"^[0-9a-f]{40}$")
_PIN_KEY = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
_VERSION_ID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


class CommonHostError(ValueError):
    pass


def is_hosted(atlan_yaml: Path) -> bool:
    """True when atlan.yaml's deploy block routes the app to common-app-server."""
    if not atlan_yaml.is_file():
        return False
    import yaml  # noqa: PLC0415 — the runner's system Python has it

    try:
        data = yaml.safe_load(atlan_yaml.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise CommonHostError(f"{atlan_yaml} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        return False
    deploy = cast("dict[str, object]", data).get("deploy")
    if not isinstance(deploy, dict):
        return False
    return cast("dict[str, object]", deploy).get(ROUTE_FLAG) is True


def host_image(app_key: str, app_rev: str, host_rev: str) -> tuple[str, str]:
    """``(image, version)`` for the host build of *app_key* at *app_rev*."""
    if not _PIN_KEY.fullmatch(app_key):
        raise CommonHostError(f"app key {app_key!r} is not a DNS label")
    for label, rev in (("app", app_rev), ("host", host_rev)):
        if not _SHA.fullmatch(rev):
            raise CommonHostError(f"{label} rev {rev!r} is not a full commit SHA")
    version = f"e2e-{app_key}-{app_rev[:7]}-{host_rev[:7]}"
    return f"{HOST_IMAGE}:{version}", version


def hosted_repos(pins: Path) -> list[str]:
    """Repo names of the apps in common-app-server's ``hosted-api-apps.toml``."""
    try:
        data = tomllib.loads(pins.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise CommonHostError(f"cannot read {pins}: {exc}") from exc
    apps = data.get("apps")
    if not isinstance(apps, dict):
        raise CommonHostError(f"{pins} has no [apps] table")
    repos: set[str] = set()
    for name, pin in cast("dict[str, object]", apps).items():
        repo = (
            cast("dict[str, object]", pin).get("repo", "")
            if isinstance(pin, dict)
            else ""
        )
        if not isinstance(repo, str) or not repo.startswith("atlanhq/"):
            raise CommonHostError(f"{pins}: apps.{name}.repo is not atlanhq/<repo>")
        repos.add(repo.removeprefix("atlanhq/"))
    if not repos:
        raise CommonHostError(f"{pins} lists no app: refusing an unscoped token")
    return sorted(repos)


def e2e_host_app_id(path: Path, shared_atlan_yaml: Path | None = None) -> str:
    """The e2e host's marketplace app id from common-app-server's ``e2e-host.toml``.

    Refuses the shared host's own id (``app_id`` in *shared_atlan_yaml*): the
    run would then install a PR build onto the host every tenant uses.
    """
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise CommonHostError(
            f"cannot read the e2e host's app id from {path}: {exc}"
        ) from exc
    value = data.get("app_id")
    if not isinstance(value, str) or not _VERSION_ID.fullmatch(value):
        raise CommonHostError(f"{path}: app_id is not a marketplace app UUID")
    if shared_atlan_yaml is not None and value == _atlan_yaml_app_id(shared_atlan_yaml):
        raise CommonHostError(
            f"{path}: app_id is the shared common-app-server's own app id "
            f"({shared_atlan_yaml}); the e2e host must be its own marketplace app"
        )
    return value


def _atlan_yaml_app_id(atlan_yaml: Path) -> str:
    import yaml  # noqa: PLC0415 — the runner's system Python has it

    try:
        data = yaml.safe_load(atlan_yaml.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise CommonHostError(f"cannot read {atlan_yaml}: {exc}") from exc
    if not isinstance(data, dict):
        raise CommonHostError(f"{atlan_yaml} is not a YAML mapping")
    value = cast("dict[str, object]", data).get("app_id")
    if not isinstance(value, str) or not value:
        raise CommonHostError(f"{atlan_yaml} has no app_id")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p_detect = sub.add_parser("detect")
    p_detect.add_argument("--atlan-yaml", type=Path, default=Path("atlan.yaml"))
    p_image = sub.add_parser("image")
    p_image.add_argument("--app-key", required=True)
    p_image.add_argument("--app-rev", required=True)
    p_image.add_argument("--host-rev", required=True)
    p_repos = sub.add_parser("hosted-repos")
    p_repos.add_argument("--pins", type=Path, required=True)
    p_host = sub.add_parser("e2e-host")
    p_host.add_argument("--file", type=Path, required=True)
    p_host.add_argument("--shared-atlan-yaml", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        if args.command == "detect":
            outputs = {"hosted": "true" if is_hosted(args.atlan_yaml) else "false"}
        elif args.command == "image":
            image, version = host_image(args.app_key, args.app_rev, args.host_rev)
            outputs = {"image": image, "version": version}
        elif args.command == "hosted-repos":
            outputs = {"repos": ",".join(hosted_repos(args.pins))}
        else:
            outputs = {"app_id": e2e_host_app_id(args.file, args.shared_atlan_yaml)}
    except CommonHostError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    for key, value in outputs.items():
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
