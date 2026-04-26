"""
Validate that an app's generated configmap filename matches atlan.yaml:name.

Native app configmap requests are keyed by the GMP/k8s app name. For app repos,
that runtime name is expected to be the top-level ``name`` in atlan.yaml. The
SDK serves generated configmaps by JSON filename stem, so at least one generated
JSON file under app/generated must have the same stem.

Validation errors are emitted as ``::error::`` annotations and exit non-zero.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


class ConfigMapContractError(Exception):
    """Raised when generated configmap files do not match atlan.yaml."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def _strip_inline_comment(value: str) -> str:
    in_single_quote = False
    in_double_quote = False
    escaped = False

    for i, char in enumerate(value):
        if escaped:
            escaped = False
            continue
        if char == "\\" and in_double_quote:
            escaped = True
            continue
        if char == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
            continue
        if char == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
            continue
        if char == "#" and not in_single_quote and not in_double_quote:
            return value[:i].rstrip()
    return value.strip()


def _unquote_yaml_scalar(value: str) -> str:
    value = _strip_inline_comment(value).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _read_top_level_name_without_yaml(path: Path) -> str:
    for raw_line in path.read_text().splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        if raw_line[0].isspace():
            continue

        key, separator, value = raw_line.partition(":")
        if separator and key.strip() == "name":
            app_name = _unquote_yaml_scalar(value)
            if app_name:
                return app_name
            raise ConfigMapContractError('atlan.yaml has an empty "name" field')

    raise ConfigMapContractError('atlan.yaml is missing required "name" field')


def read_atlan_app_name(path: Path) -> str:
    if not path.is_file():
        raise ConfigMapContractError(f"{path} not found")

    try:
        import yaml  # type: ignore[import-untyped]
    except ModuleNotFoundError:
        return _read_top_level_name_without_yaml(path)

    try:
        loaded = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigMapContractError(f"{path} is invalid YAML: {exc}") from exc

    if not isinstance(loaded, dict):
        raise ConfigMapContractError(f"{path} must contain a top-level mapping")

    app_name = loaded.get("name")
    if not isinstance(app_name, str) or not app_name.strip():
        raise ConfigMapContractError('atlan.yaml is missing required "name" field')
    return app_name.strip()


def generated_configmap_stems(generated_dir: Path) -> list[str]:
    if not generated_dir.is_dir():
        raise ConfigMapContractError(f"{generated_dir} not found")

    stems = {
        json_file.stem
        for json_file in generated_dir.rglob("*.json")
        if json_file.stem != "manifest"
    }
    return sorted(stems)


def validate_configmap_contract(
    atlan_yaml_path: Path,
    generated_dir: Path,
) -> tuple[str, list[str]]:
    app_name = read_atlan_app_name(atlan_yaml_path)
    expected_stem = app_name.lower()
    stems = generated_configmap_stems(generated_dir)

    if expected_stem not in stems:
        found = ", ".join(stems) if stems else "(none)"
        raise ConfigMapContractError(
            "Generated configmap does not match atlan.yaml name.\n"
            f"Expected generated configmap stem: {expected_stem}\n"
            f"Found generated configmap stems: {found}\n"
            "For native apps, Heracles requests the configmap using the GMP/k8s "
            "app name from atlan.yaml, so this mismatch can make the UI render "
            "without a config form."
        )

    return expected_stem, stems


def _escape_github_annotation(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate generated configmap filename stems against atlan.yaml:name."
    )
    parser.add_argument("--atlan-yaml", default="atlan.yaml", help="Path to atlan.yaml")
    parser.add_argument(
        "--generated-dir",
        default="app/generated",
        help="Directory containing generated contract JSON files",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        expected_stem, stems = validate_configmap_contract(
            Path(args.atlan_yaml), Path(args.generated_dir)
        )
    except ConfigMapContractError as exc:
        print(
            "::error title=ConfigMap contract mismatch::"
            + _escape_github_annotation(exc.message),
            file=sys.stderr,
        )
        print(exc.message, file=sys.stderr)
        return 1

    print(f"ConfigMap contract valid: expected={expected_stem} available={stems}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
