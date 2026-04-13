"""
Parse atlan.yaml for the Build & Publish reusable workflow.

Reads atlan.yaml from CWD (and uv.lock for sdk_version) and emits the values
the workflow needs as ``key=value`` lines on stdout. The workflow appends
those lines to ``$GITHUB_OUTPUT`` (deploy_config is multiline and uses the
GHA heredoc-delimiter syntax).

Lives outside the YAML so it can be unit-tested. Keep this file dependency-
light — only PyYAML is available in the workflow runner by default.

Outputs (single-line, append-safe):
    app_name           lowercased ``name`` from atlan.yaml
    app_id             ``app_id``
    build_tag          ``build_tag`` (only ``v1`` supported today)
    dockerfile         ``dockerfile`` (default ``./Dockerfile``)
    enable_sdr         ``true``/``false`` from ``self_deployed_runtime``
    sdk_version        atlan-application-sdk version pinned in uv.lock (or empty)
    workflow_packages  JSON-encoded list (single line) or empty string
    deploy_config      YAML-dumped ``deploy:`` block (multiline; heredoc'd by caller)

Validation errors are emitted as ``::error::`` annotations and exit non-zero.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any

import yaml

# Strict kebab-case: lowercase a-z, 0-9, hyphen separators only. Mirrors the
# Pydantic validator on the GM side. Tile name flows downstream as a URL/route
# param + configmap filename stem — must stay safe for both.
_KEBAB_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

# Closed type set. Mirrors core.version.model.WorkflowPackageType in GM.
_ALLOWED_TYPES = {"connector", "miner", "orchestrator", "utility", "custom"}

# Required keys for each workflow_packages entry. description and icon_url
# are optional (default empty string on GM side).
_REQUIRED_PACKAGE_KEYS = {"name", "display_name", "type", "generated_dir"}


def _err(msg: str) -> None:
    """Emit a GitHub Actions error annotation and exit non-zero."""
    print(f"::error::{msg}", file=sys.stderr)
    sys.exit(1)


def _validate_workflow_packages(wp_val: Any) -> str:
    """Validate ``workflow_packages`` and return its JSON-encoded form (single line).

    Returns empty string if the field is absent or empty (single-package apps).
    Same rules as ``core.version.model.WorkflowPackage`` in GM — validating
    here means failures surface at CI time with annotated errors instead of
    a 422 from the publish endpoint.
    """
    if not wp_val:
        return ""
    if not isinstance(wp_val, list):
        _err("atlan.yaml workflow_packages must be a list")

    seen_names: set[str] = set()
    for i, pkg in enumerate(wp_val):
        if not isinstance(pkg, dict):
            _err(f"workflow_packages[{i}] must be a mapping")

        missing = _REQUIRED_PACKAGE_KEYS - set(pkg.keys())
        if missing:
            _err(f"workflow_packages[{i}] missing required keys: {sorted(missing)}")

        name = pkg.get("name")
        if not isinstance(name, str) or not _KEBAB_RE.match(name):
            _err(
                f"workflow_packages[{i}].name must be kebab-case "
                f"(lowercase a-z, 0-9, hyphens); got {name!r}"
            )
        if name in seen_names:
            _err(f"workflow_packages[{i}].name {name!r} is duplicated")
        seen_names.add(name)

        pkg_type = pkg.get("type")
        if pkg_type not in _ALLOWED_TYPES:
            _err(
                f"workflow_packages[{i}].type must be one of "
                f"{sorted(_ALLOWED_TYPES)}; got {pkg_type!r}"
            )

        generated_dir = pkg.get("generated_dir")
        if not isinstance(generated_dir, str) or not generated_dir.strip():
            _err(f"workflow_packages[{i}].generated_dir must be a non-empty string")

        display_name = pkg.get("display_name")
        if not isinstance(display_name, str) or not display_name.strip():
            _err(f"workflow_packages[{i}].display_name must be a non-empty string")

    # Compact JSON: single-line so it survives the workflow's
    # ``$GITHUB_OUTPUT`` ``key=value`` append (multi-line outputs need
    # delimiter syntax).
    return json.dumps(wp_val, separators=(",", ":"))


def _read_sdk_version_from_uv_lock(path: str = "uv.lock") -> str:
    """Best-effort read of the locked atlan-application-sdk version.

    Reads the precise version regardless of how the dependency is declared in
    pyproject.toml (==, >=, git ref, etc.). Returns empty string if uv.lock
    isn't present — non-fatal.
    """
    re_ver = re.compile(r'^version\s*=\s*"([^"]+)"')
    try:
        with open(path) as f:
            in_sdk_block = False
            for raw in f:
                line = raw.rstrip()
                if line == 'name = "atlan-application-sdk"':
                    in_sdk_block = True
                    continue
                if in_sdk_block:
                    m = re_ver.match(line)
                    if m:
                        return m.group(1)
                    if line.startswith("name = ") or line.startswith("[["):
                        break
    except FileNotFoundError:
        pass
    return ""


def parse(
    atlan_yaml_path: str = "atlan.yaml", uv_lock_path: str = "uv.lock"
) -> dict[str, str]:
    """Parse atlan.yaml + uv.lock and return the workflow-output dict.

    Pure function — no I/O on stdout / GITHUB_OUTPUT. Test entry point.
    """
    if not os.path.isfile(atlan_yaml_path):
        _err("atlan.yaml not found in repo root")

    try:
        with open(atlan_yaml_path) as f:
            d = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        _err(f"atlan.yaml is invalid YAML: {e}")

    app_name = (d.get("name") or "").lower()
    app_id = d.get("app_id", "")
    build_tag = d.get("build_tag", "v1")
    dockerfile = d.get("dockerfile", "./Dockerfile")
    enable_sdr = "true" if d.get("self_deployed_runtime", False) else "false"

    if not app_name:
        _err('atlan.yaml is missing required "name" field')

    if build_tag != "v1":
        _err(
            f"Unsupported build_tag {build_tag!r}. Only 'v1' is supported by this template version."
        )

    deploy_val = d.get("deploy")
    deploy_config = (
        yaml.dump(deploy_val, default_flow_style=False)
        if deploy_val is not None
        else ""
    )

    workflow_packages = _validate_workflow_packages(d.get("workflow_packages"))
    sdk_version = _read_sdk_version_from_uv_lock(uv_lock_path)

    return {
        "app_name": app_name,
        "app_id": str(app_id),
        "build_tag": build_tag,
        "dockerfile": dockerfile,
        "enable_sdr": enable_sdr,
        "sdk_version": sdk_version,
        "workflow_packages": workflow_packages,
        "deploy_config": deploy_config,
    }


def _write_outputs(outputs: dict[str, str]) -> None:
    """Write outputs to ``$GITHUB_OUTPUT``. ``deploy_config`` is multiline → heredoc."""
    gh_output = os.environ.get("GITHUB_OUTPUT")
    if not gh_output:
        # Local dry-run: just dump key=value lines to stdout.
        for k, v in outputs.items():
            if "\n" in v:
                print(f"{k}<<DEPLOY_EOF\n{v}\nDEPLOY_EOF")
            else:
                print(f"{k}={v}")
        return

    with open(gh_output, "a") as out:
        for k, v in outputs.items():
            if k == "deploy_config":
                out.write(f"deploy_config<<DEPLOY_EOF\n{v}\nDEPLOY_EOF\n")
            else:
                out.write(f"{k}={v}\n")


def main() -> None:
    outputs = parse()
    _write_outputs(outputs)
    print(f"App: {outputs['app_name']}")
    print(f"App ID: {outputs['app_id']}")
    print(f"Dockerfile: {outputs['dockerfile']}")
    print(f"SDR: {outputs['enable_sdr']}")
    print(f"SDK version: {outputs['sdk_version'] or '(not pinned)'}")
    print(
        "Workflow packages: "
        + (
            outputs["workflow_packages"]
            or "(none — falling back to argo_package_names)"
        )
    )


if __name__ == "__main__":
    main()
