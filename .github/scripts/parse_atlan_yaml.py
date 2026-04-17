"""
Parse atlan.yaml for the Build & Publish reusable entrypoint.

Reads atlan.yaml from CWD (and uv.lock for sdk_version) and emits the values
the entrypoint needs as ``key=value`` lines on stdout. The entrypoint appends
those lines to ``$GITHUB_OUTPUT`` (deploy_config is multiline and uses the
GHA heredoc-delimiter syntax).

Lives outside the YAML so it can be unit-tested. Keep this file dependency-
light — only PyYAML is available in the entrypoint runner by default.

Outputs (single-line, append-safe):
    app_name           lowercased ``name`` from atlan.yaml
    app_id             ``app_id``
    build_tag          ``build_tag`` (only ``v1`` supported today)
    dockerfile         ``dockerfile`` (default ``./Dockerfile``)
    enable_sdr         ``true``/``false`` from ``self_deployed_runtime``
    sdk_version        atlan-application-sdk version pinned in uv.lock (or empty)
    entrypoints  JSON-encoded list (single line) or empty string
    deploy_config      YAML-dumped ``deploy:`` block (multiline; heredoc'd by caller)

Validation errors are emitted as ``::error::`` annotations and exit non-zero.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
from typing import Any

import yaml

# Strict kebab-case: must start with a letter (matches the runtime handler's
# _ENTRYPOINT_NAME_RE which also requires letter-start). Digits, hyphens, and
# lowercase letters are allowed in subsequent segments. Tile name flows
# downstream as a URL/route param + configmap filename stem.
_KEBAB_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")

# Closed type set. Mirrors core.version.model.EntrypointPackageType in GM.
_ALLOWED_TYPES = {"connector", "miner", "orchestrator", "utility", "custom"}

# Required keys for each entrypoints entry. description and icon_url
# are optional (default empty string on GM side).
# generated_dir is auto-derived from name (Option B: name == folder) so it is
# optional here; if provided it must equal name.
_REQUIRED_PACKAGE_KEYS = {"name", "display_name", "type"}


class AtlanYamlError(Exception):
    """Raised when atlan.yaml validation fails."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def _err(msg: str) -> None:
    """Raise :class:`AtlanYamlError` with *msg*."""
    raise AtlanYamlError(msg)


def _validate_entrypoints(wp_val: Any) -> str:
    """Validate ``entrypoints`` and return its JSON-encoded form (single line).

    Returns empty string if the field is absent or empty (single-package apps).
    Same rules as ``core.version.model.EntrypointPackage`` in GM — validating
    here means failures surface at CI time with annotated errors instead of
    a 422 from the publish endpoint.
    """
    if not wp_val:
        return ""
    if not isinstance(wp_val, list):
        _err("atlan.yaml entrypoints must be a list")

    seen_names: set[str] = set()
    for i, pkg in enumerate(wp_val):
        if not isinstance(pkg, dict):
            _err(f"entrypoints[{i}] must be a mapping")

        missing = _REQUIRED_PACKAGE_KEYS - set(pkg.keys())
        if missing:
            _err(f"entrypoints[{i}] missing required keys: {sorted(missing)}")

        name = pkg.get("name")
        if not isinstance(name, str) or not _KEBAB_RE.match(name):
            _err(
                f"entrypoints[{i}].name must be kebab-case "
                f"(lowercase a-z, 0-9, hyphens); got {name!r}"
            )
        if name in seen_names:
            _err(f"entrypoints[{i}].name {name!r} is duplicated")
        seen_names.add(name)

        pkg_type = pkg.get("type")
        if pkg_type not in _ALLOWED_TYPES:
            _err(
                f"entrypoints[{i}].type must be one of "
                f"{sorted(_ALLOWED_TYPES)}; got {pkg_type!r}"
            )

        # Option B: entrypoint name IS the generated directory.
        # generated_dir is optional in atlan.yaml — defaults to name when absent.
        # If explicitly provided it must equal name so the convention is enforced.
        generated_dir = pkg.get("generated_dir")
        if generated_dir is None:
            pkg["generated_dir"] = name
        elif generated_dir != name:
            _err(
                f"entrypoints[{i}].generated_dir must equal name ({name!r}); "
                f"got {generated_dir!r}. "
                "Set generated_dir equal to name or omit it (it will default to name)."
            )

        display_name = pkg.get("display_name")
        if not isinstance(display_name, str) or not display_name.strip():
            _err(f"entrypoints[{i}].display_name must be a non-empty string")

    # Compact JSON: single-line so it survives the entrypoint's
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
    """Parse atlan.yaml + uv.lock and return the entrypoint-output dict.

    Raises :class:`AtlanYamlError` on validation failures.  No I/O on
    stdout / GITHUB_OUTPUT — test entry point.
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

    entrypoints = _validate_entrypoints(d.get("entrypoints"))
    sdk_version = _read_sdk_version_from_uv_lock(uv_lock_path)

    return {
        "app_name": app_name,
        "app_id": str(app_id),
        "build_tag": build_tag,
        "dockerfile": dockerfile,
        "enable_sdr": enable_sdr,
        "sdk_version": sdk_version,
        "entrypoints": entrypoints,
        "deploy_config": deploy_config,
    }


def _write_outputs(outputs: dict[str, str]) -> None:
    """Write outputs to ``$GITHUB_OUTPUT``. ``deploy_config`` is multiline → heredoc."""
    gh_output = os.environ.get("GITHUB_OUTPUT")
    # Randomised delimiter prevents a crafted deploy: block from injecting
    # arbitrary $GITHUB_OUTPUT values by containing the literal delimiter.
    delim = f"DEPLOY_EOF_{secrets.token_hex(8)}"
    if not gh_output:
        # Local dry-run: just dump key=value lines to stdout.
        for k, v in outputs.items():
            if "\n" in v:
                print(f"{k}<<{delim}\n{v}\n{delim}")
            else:
                print(f"{k}={v}")
        return

    with open(gh_output, "a") as out:
        for k, v in outputs.items():
            if k == "deploy_config":
                out.write(f"deploy_config<<{delim}\n{v}\n{delim}\n")
            else:
                out.write(f"{k}={v}\n")


def main() -> None:
    try:
        outputs = parse()
    except AtlanYamlError as e:
        print(f"::error::{e.message}", file=sys.stderr)
        sys.exit(1)
    _write_outputs(outputs)
    print(f"App: {outputs['app_name']}")
    print(f"App ID: {outputs['app_id']}")
    print(f"Dockerfile: {outputs['dockerfile']}")
    print(f"SDR: {outputs['enable_sdr']}")
    print(f"SDK version: {outputs['sdk_version'] or '(not pinned)'}")
    print(
        "Entrypoint packages: "
        + (outputs["entrypoints"] or "(none — falling back to argo_package_names)")
    )


if __name__ == "__main__":
    main()
