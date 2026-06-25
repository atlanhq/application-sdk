#!/usr/bin/env bash
# Discover and regenerate all PKL example outputs.
# New examples are auto-discovered — no list to maintain.
#
# Each example is evaluated with `pkl eval -m <example-dir> <example>/app.pkl`
# so that output file keys (e.g. `app/generated/foo.json`, `atlan.yaml`)
# land at their natural paths relative to the example directory — mirroring
# how consuming apps run `pkl eval -m . contract/app.pkl` from their repo root.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

failed=0

for pkl in examples/*/app.pkl; do
  [ -f "$pkl" ] || continue

  example_dir="$(dirname "$pkl")"

  # Remove known generated paths before eval so stale artifacts don't linger.
  rm -rf "${example_dir}/app/generated"
  rm -f  "${example_dir}/atlan.yaml" "${example_dir}/app.yaml"

  echo ":: $pkl → $example_dir"
  if ! pkl eval -m "$example_dir" "$pkl"; then
    echo "FAIL: $pkl"
    failed=1
  fi
done

if [ "$failed" -ne 0 ]; then
  echo ""
  echo "Some examples failed to generate. See errors above."
  exit 1
fi

# Lint + format generated Python files to match what the pre-commit hooks produce.
# ruff check --fix removes unused imports (F401); ruff format handles whitespace.
PY_FILES=$(find examples -name '_input.py' | sort)
if [ -n "$PY_FILES" ]; then
  echo ""
  echo ":: Linting and formatting generated Python files..."
  if command -v uvx &> /dev/null; then
    echo "$PY_FILES" | xargs uvx ruff check --fix --select F401 --quiet
    echo "$PY_FILES" | xargs uvx ruff format
  elif command -v ruff &> /dev/null; then
    echo "$PY_FILES" | xargs ruff check --fix --select F401 --quiet
    echo "$PY_FILES" | xargs ruff format
  else
    echo "WARNING: ruff not found — skipping Python lint/format."
  fi
fi

echo ""
echo "All examples regenerated successfully."
