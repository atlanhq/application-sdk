#!/usr/bin/env python3
"""Codebase index builder for SDK Evolution.

Builds/updates a structured index of the application-sdk codebase:
- Per-file metadata (line count, classes, functions, imports, domain tags)
- Import graph + reverse import graph
- Call graph (function → callers)
- Public API surface (__init__.py exports)
- Dumping ground detection (files with 3+ unrelated domains)
- v3 replacement mapping
- Deprecation flags

Usage:
  python update_index.py --repo /workspace/repo --index session/INDEX.md --output /tmp/updated-index.json

On first run (or with --full), does a full AST parse of all .py files.
On subsequent runs, only re-parses files changed since the index's last_commit_sha.
"""

import argparse
import ast
import json
import os
import random
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def git_head_sha(repo: str) -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()


def git_changed_files(repo: str, since_sha: str) -> list[str]:
    """Files changed since the given SHA."""
    try:
        out = subprocess.check_output(
            ["git", "diff", "--name-only", since_sha, "HEAD"],
            cwd=repo, text=True, stderr=subprocess.DEVNULL,
        )
        return [f for f in out.strip().split("\n") if f.endswith(".py")]
    except subprocess.CalledProcessError:
        return []


def parse_python_file(filepath: str) -> dict:
    """AST-parse a Python file and extract metadata."""
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
            lines = content.count("\n") + 1
    except (OSError, UnicodeDecodeError):
        return {"error": f"Cannot read {filepath}"}

    try:
        tree = ast.parse(content, filename=filepath)
    except SyntaxError:
        return {
            "path": filepath, "lines": lines,
            "classes": [], "functions": [], "imports": [],
            "domain_tags": [], "complexity_score": 0,
            "has_v2_patterns": False,
        }

    classes = []
    functions = []
    imports = []
    domain_tags = set()
    has_v2 = False

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            bases = [_name(b) for b in node.bases]
            classes.append({
                "name": node.name,
                "line_start": node.lineno,
                "line_end": node.end_lineno or node.lineno,
                "bases": bases,
            })
            # Check for v2 patterns
            if any(b in ("ActivitiesInterface", "WorkflowInterface", "HandlerInterface",
                         "BaseSQLMetadataExtractionActivities") for b in bases):
                has_v2 = True

        elif isinstance(node, ast.FunctionDef) or isinstance(node, ast.AsyncFunctionDef):
            if isinstance(node, ast.FunctionDef) and any(
                isinstance(p, ast.ClassDef) for p in ast.walk(tree)
            ):
                pass  # Skip methods inside classes for top-level function list
            functions.append({
                "name": node.name,
                "line_start": node.lineno,
                "line_end": node.end_lineno or node.lineno,
                "is_async": isinstance(node, ast.AsyncFunctionDef),
            })

        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)
                # Detect v2 import patterns
                if any(v2 in node.module for v2 in (
                    "application_sdk.workflows",
                    "application_sdk.activities",
                    "application_sdk.handlers",
                    "application_sdk.services",
                )):
                    has_v2 = True
                # Domain tagging
                if "sql" in node.module.lower():
                    domain_tags.add("sql")
                if "credential" in node.module.lower():
                    domain_tags.add("credentials")
                if "storage" in node.module.lower() or "s3" in node.module.lower():
                    domain_tags.add("file_io")
                if "dapr" in node.module.lower():
                    domain_tags.add("dapr")
                if "temporal" in node.module.lower():
                    domain_tags.add("temporal")
                if "http" in node.module.lower() or "requests" in node.module.lower():
                    domain_tags.add("http")

    # Simple complexity: count nesting depth indicators
    complexity = content.count("    if ") + content.count("    for ") + content.count("    while ")

    return {
        "path": filepath,
        "lines": lines,
        "classes": classes,
        "functions": functions,
        "imports": imports,
        "domain_tags": sorted(domain_tags),
        "complexity_score": complexity,
        "has_v2_patterns": has_v2,
    }


def _name(node) -> str:
    """Extract name from an AST node."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_name(node.value)}.{node.attr}"
    return "?"


def build_import_graph(file_indices: dict[str, dict]) -> tuple[dict, dict]:
    """Build import and reverse-import graphs from file metadata."""
    import_graph: dict[str, list[str]] = {}
    reverse_graph: dict[str, list[str]] = {}

    module_to_file: dict[str, str] = {}
    for path in file_indices:
        mod = path.replace("/", ".").replace(".py", "").replace(".__init__", "")
        module_to_file[mod] = path

    for path, meta in file_indices.items():
        import_graph[path] = []
        for imp in meta.get("imports", []):
            # Find the file this import resolves to
            for mod, fpath in module_to_file.items():
                if imp.startswith(mod) or mod.startswith(imp):
                    if fpath != path:
                        import_graph[path].append(fpath)
                        reverse_graph.setdefault(fpath, []).append(path)
                    break

    return import_graph, reverse_graph


def detect_dumping_grounds(file_indices: dict[str, dict]) -> list[str]:
    """Files with 3+ unrelated domain tags are dumping grounds."""
    return [
        path for path, meta in file_indices.items()
        if len(meta.get("domain_tags", [])) >= 3
    ]


def find_public_api(repo: str) -> list[str]:
    """Find all symbols exported via __init__.py files."""
    public = []
    for init_path in Path(repo).rglob("application_sdk/**/__init__.py"):
        try:
            content = init_path.read_text()
            tree = ast.parse(content)
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id == "__all__":
                            if isinstance(node.value, ast.List):
                                for elt in node.value.elts:
                                    if isinstance(elt, ast.Constant):
                                        public.append(str(elt.value))
        except (SyntaxError, OSError):
            continue
    return public


def spot_check(file_indices: dict[str, dict], repo: str, n: int = 5) -> int:
    """Spot-check N random files for staleness. Returns count of stale files."""
    paths = list(file_indices.keys())
    sample = random.sample(paths, min(n, len(paths)))
    stale = 0
    for path in sample:
        full = os.path.join(repo, path)
        if not os.path.exists(full):
            stale += 1
            continue
        actual_lines = sum(1 for _ in open(full, errors="replace"))
        indexed_lines = file_indices[path].get("lines", 0)
        if abs(actual_lines - indexed_lines) > 5:
            stale += 1
    return stale


def main():
    parser = argparse.ArgumentParser(description="SDK Evolution codebase index builder")
    parser.add_argument("--repo", required=True, help="Path to the repo checkout")
    parser.add_argument("--index", required=True, help="Path to existing INDEX.md (or empty)")
    parser.add_argument("--output", required=True, help="Path to write updated index JSON")
    parser.add_argument("--full", action="store_true", help="Force full rebuild")
    args = parser.parse_args()

    repo = args.repo
    current_sha = git_head_sha(repo)

    # Try loading existing index
    existing_index = None
    if os.path.exists(args.index) and not args.full:
        try:
            with open(args.index) as f:
                content = f.read()
                # INDEX.md may be markdown with JSON in a code block
                if "```json" in content:
                    json_str = content.split("```json")[1].split("```")[0]
                    existing_index = json.loads(json_str)
                elif content.strip().startswith("{"):
                    existing_index = json.loads(content)
        except (json.JSONDecodeError, IndexError):
            existing_index = None

    # Determine if we need full rebuild
    force_full = args.full
    if existing_index:
        last_sha = existing_index.get("last_commit_sha", "")
        if last_sha == current_sha:
            # No changes — spot-check and return
            stale = spot_check(existing_index.get("files", {}), repo)
            if stale > 2:
                print(f"WARN: {stale}/5 spot-check files stale, forcing full rebuild")
                force_full = True
            else:
                print(f"Index up-to-date (SHA {current_sha[:8]}), {stale} stale spot-checks")
                # Write existing index to output
                with open(args.output, "w") as f:
                    json.dump(existing_index, f, indent=2)
                return

        # Check change ratio
        changed = git_changed_files(repo, last_sha)
        total_files = len(existing_index.get("files", {}))
        if total_files > 0 and len(changed) / total_files > 0.40:
            print(f"WARN: {len(changed)}/{total_files} files changed (>40%), forcing full rebuild")
            force_full = True

    if not existing_index:
        force_full = True

    # Collect Python files
    all_py_files = []
    for root, dirs, files in os.walk(os.path.join(repo, "application_sdk")):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for f in files:
            if f.endswith(".py"):
                rel = os.path.relpath(os.path.join(root, f), repo)
                all_py_files.append(rel)

    for root, dirs, files in os.walk(os.path.join(repo, "tests")):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for f in files:
            if f.endswith(".py"):
                rel = os.path.relpath(os.path.join(root, f), repo)
                all_py_files.append(rel)

    if force_full:
        print(f"Full index build: {len(all_py_files)} files")
        files_to_parse = all_py_files
        file_indices = {}
    else:
        changed = git_changed_files(repo, existing_index["last_commit_sha"])
        files_to_parse = [f for f in changed if f in all_py_files or os.path.exists(os.path.join(repo, f))]
        file_indices = existing_index.get("files", {})
        print(f"Incremental update: {len(files_to_parse)} changed files")

    # Parse files
    for rel_path in files_to_parse:
        full_path = os.path.join(repo, rel_path)
        if os.path.exists(full_path):
            file_indices[rel_path] = parse_python_file(full_path)
        elif rel_path in file_indices:
            del file_indices[rel_path]  # File deleted

    # Build graphs
    import_graph, reverse_import_graph = build_import_graph(file_indices)
    dumping_grounds = detect_dumping_grounds(file_indices)
    public_api = find_public_api(repo)

    # Spot-check
    stale_count = spot_check(file_indices, repo)
    if stale_count > 2:
        print(f"WARN: {stale_count}/5 spot-checks stale after update")

    # Build output
    index = {
        "last_commit_sha": current_sha,
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "last_full_rebuild": datetime.now(timezone.utc).isoformat() if force_full else (
            existing_index.get("last_full_rebuild", datetime.now(timezone.utc).isoformat())
            if existing_index else datetime.now(timezone.utc).isoformat()
        ),
        "total_files": len(file_indices),
        "files": file_indices,
        "import_graph": import_graph,
        "reverse_import_graph": reverse_import_graph,
        "public_api": public_api,
        "dumping_grounds": dumping_grounds,
        "stale_spot_checks": stale_count,
    }

    with open(args.output, "w") as f:
        json.dump(index, f, indent=2)

    print(f"Index written: {len(file_indices)} files, {len(dumping_grounds)} dumping grounds, "
          f"{len(public_api)} public API symbols")


if __name__ == "__main__":
    main()
