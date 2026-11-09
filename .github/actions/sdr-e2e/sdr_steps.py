"""Branching steps of the sdr-e2e composite action, one subcommand per step.

Each subcommand used to be an inline ``run:`` block in ``action.yaml``.
docs/standards/ci.md keeps conditional logic out of inlined shell, so the
decisions live here and the steps call them straight-line. Tests:
``.github/scripts/tests/test_sdr_e2e_steps.py``.

Lives beside the action, not under ``.github/scripts/``, for the same reason as
``select_dapr_components.py``: the action stashes its own directory to
``/tmp/sdr-e2e`` before setup-deps wipes the workspace, and the steps after that
run from the stash.

Subcommands:

* ``image-ref``      the prebuilt image reference, else the one just built.
* ``action-root``    where the action's assets live after setup-deps.
* ``test-config``    render the app's test-config template, else the SDK's.
* ``secrets-bundle`` run the caller's secrets script and check its output.
* ``resolve-hosts``  pin the tenant's auth + Temporal hostnames to IPv4.
* ``wait-ready``     poll the SDR container's health endpoint.
* ``restore-assets`` put the action back in place for post-hooks.
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

#: Where the "Stash action assets" step copies the action directory.
DEFAULT_STASH = "/tmp/sdr-e2e"
HEALTH_URL = "http://localhost:8000/server/health"


def _run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    """The one seam to external commands, so tests can stub them."""
    return subprocess.run(argv, check=False, **kwargs)


def _append(env_var: str, lines: list[str]) -> None:
    with open(os.environ[env_var], "a", encoding="utf-8") as f:
        for line in lines:
            f.write(f"{line}\n")


def _error(message: str) -> int:
    print(f"::error::{message}")
    return 1


def image_ref(prebuilt: str, built: str) -> int:
    ref = prebuilt or built
    if not ref:
        return _error(
            "No image reference resolved: prebuilt-image is empty and the build "
            "produced no image. If the build step was skipped, pass prebuilt-image; "
            "if it ran, check its logs."
        )
    print(f"Using image: {ref}")
    _append("GITHUB_OUTPUT", [f"ref={ref}"])
    return 0


def action_root(action_path: str, stash: str) -> int:
    # A LOCAL action's path sits in the workspace, which setup-deps' checkout
    # wiped; the stash made before that is the fallback.
    path = Path(action_path)
    root = (
        action_path
        if action_path and path.is_dir() and (path / "test-config.yaml.tmpl").is_file()
        else stash
    )
    print(f"Using SDR action assets from: {root}")
    _append("GITHUB_OUTPUT", [f"path={root}"])
    return 0


def _envsubst(template: str, dest: str) -> int:
    with open(template, "rb") as src, open(dest, "wb") as out:
        return _run(["envsubst"], stdin=src, stdout=out).returncode


def render_test_config(sdr_config_dir: str, sdk_template: str, dest: str) -> int:
    app_template = f"{sdr_config_dir}/test-config.yaml.tmpl"
    if Path(app_template).is_file():
        print("Using app-level test-config.yaml.tmpl override")
        return _envsubst(app_template, dest)
    print("Using SDK default test-config.yaml.tmpl")
    return _envsubst(sdk_template, dest)


def secrets_bundle(sdr_config_dir: str, script: str) -> int:
    Path(f"{sdr_config_dir}/secrets").mkdir(parents=True, exist_ok=True)
    if not script or not Path(script).is_file():
        return _error(
            f"secrets-script not found at {script}. Provide one that writes "
            f"{sdr_config_dir}/secrets/credentials.json from env vars."
        )
    interpreter = "python3" if script.endswith(".py") else "bash"
    rc = _run([interpreter, script]).returncode
    if rc != 0:
        return rc
    if not Path(f"{sdr_config_dir}/secrets/credentials.json").is_file():
        return _error(
            "secrets-script ran but did not produce "
            f"{sdr_config_dir}/secrets/credentials.json"
        )
    return 0


def _first_a_record(host: str) -> tuple[int, str]:
    """``dig +short A <host> | head -1``: the first answer line, verbatim."""
    proc = _run(["dig", "+short", "A", host], capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr or "")
        return proc.returncode, ""
    lines = proc.stdout.splitlines()
    return 0, lines[0] if lines else ""


def resolve_hosts(tenant: str) -> int:
    if not tenant:
        return _error(
            "SDR_TEST_TENANT is not set; the caller workflow must export it as a "
            "job-level env var."
        )
    auth_host = tenant
    temporal_host = f"{tenant.removesuffix('.atlan.com')}-temporal.atlan.com"
    rc, auth_ipv4 = _first_a_record(auth_host)
    if rc:
        return rc
    rc, temporal_ipv4 = _first_a_record(temporal_host)
    if rc:
        return rc
    if not auth_ipv4 or not temporal_ipv4:
        return _error(
            f"Failed to resolve TEMPORAL_HOST={temporal_host} or AUTH_HOST={auth_host}"
        )
    print(f"Resolved {auth_host} -> {auth_ipv4}")
    print(f"Resolved {temporal_host} -> {temporal_ipv4}")
    # Exported for the compose extra_hosts substitution.
    _append(
        "GITHUB_ENV",
        [
            f"AUTH_HOST={auth_host}",
            f"AUTH_IPV4={auth_ipv4}",
            f"TEMPORAL_HOST={temporal_host}",
            f"TEMPORAL_IPV4={temporal_ipv4}",
        ],
    )
    return 0


def wait_ready(timeout: int, compose_files: str) -> int:
    print(f"Waiting for SDR container on :8000/server/health (timeout={timeout}s)...")
    for i in range(1, timeout + 1):
        probe = _run(
            ["curl", "-sf", HEALTH_URL],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if probe.returncode == 0:
            print(f"SDR container ready after {i}s")
            return 0
        if i == timeout:
            print(f"::error::SDR container failed to start within {timeout}s")
            _run(["docker", "compose", *shlex.split(compose_files), "logs"])
            return 1
        time.sleep(1)
    return 0


def restore_assets(action_path: str, stash: str) -> int:
    # Post-hooks re-read <action_path>/action.yaml; a LOCAL action's copy was
    # wiped by setup-deps' checkout, so put the stash back.
    if not Path(action_path, "action.yaml").is_file() and Path(stash).is_dir():
        Path(action_path).mkdir(parents=True, exist_ok=True)
        shutil.copytree(stash, action_path, symlinks=True, dirs_exist_ok=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("image-ref")
    p.add_argument("--prebuilt", default="")
    p.add_argument("--built", default="")

    for name in ("action-root", "restore-assets"):
        p = sub.add_parser(name)
        p.add_argument("--action-path", required=True)
        p.add_argument("--stash", default=DEFAULT_STASH)

    p = sub.add_parser("test-config")
    p.add_argument("--sdr-config-dir", default="")
    p.add_argument("--sdk-template", required=True)
    p.add_argument("--output", default="test-config.yaml")

    p = sub.add_parser("secrets-bundle")
    p.add_argument("--sdr-config-dir", default="")
    p.add_argument("--script", default="")

    p = sub.add_parser("resolve-hosts")
    p.add_argument("--tenant", default="")

    p = sub.add_parser("wait-ready")
    p.add_argument("--timeout", type=int, required=True)
    p.add_argument("--compose-files", default="")

    args = parser.parse_args(argv)
    if args.command == "image-ref":
        return image_ref(args.prebuilt, args.built)
    if args.command == "action-root":
        return action_root(args.action_path, args.stash)
    if args.command == "restore-assets":
        return restore_assets(args.action_path, args.stash)
    if args.command == "test-config":
        return render_test_config(args.sdr_config_dir, args.sdk_template, args.output)
    if args.command == "secrets-bundle":
        return secrets_bundle(args.sdr_config_dir, args.script)
    if args.command == "resolve-hosts":
        return resolve_hosts(args.tenant)
    return wait_ready(args.timeout, args.compose_files)


if __name__ == "__main__":
    sys.exit(main())
