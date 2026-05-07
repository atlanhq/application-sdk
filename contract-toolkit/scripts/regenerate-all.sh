#!/usr/bin/env bash
# Discover and regenerate all PKL example outputs.
# New examples are auto-discovered — no list to maintain.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

failed=0

for pkl in examples/*/*.pkl; do
  [ -f "$pkl" ] || continue

  base="$(basename "$pkl" .pkl)"

  # Skip non-contract files (shared modules, package metadata)
  case "$base" in
    credentials|PklProject) continue ;;
  esac

  dir="$(dirname "$pkl")/generated"

  # Optional split contracts: foo.pkl → generated/foo/
  if [ "$base" != "app" ]; then
    dir="$(dirname "$pkl")/generated/$base"
  fi

  echo ":: $pkl → $dir"
  if ! pkl eval -m "$dir" "$pkl"; then
    echo "FAIL: $pkl"
    failed=1
  fi
done

if [ "$failed" -ne 0 ]; then
  echo ""
  echo "Some examples failed to generate. See errors above."
  exit 1
fi

# Format generated Python files so they pass ruff format --check in CI.
PY_FILES=$(find examples -name '_input.py')
if [ -n "$PY_FILES" ]; then
  echo ""
  echo ":: Formatting generated Python files..."
  if command -v uvx &> /dev/null; then
    echo "$PY_FILES" | xargs uvx ruff format
  elif command -v ruff &> /dev/null; then
    echo "$PY_FILES" | xargs ruff format
  else
    echo "WARNING: ruff not found — skipping Python formatting."
  fi
fi

echo ""
echo "All examples regenerated successfully."
