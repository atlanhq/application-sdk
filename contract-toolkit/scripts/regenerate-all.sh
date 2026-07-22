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
# No --select: ruff auto-discovers this repo's own pyproject.toml rule set,
# same as pre-commit would apply — hardcoding a narrower subset (previously
# just F401) drifts from whatever's actually configured (see
# .github/scripts/renovate_pkl_sync.py's _format_generated for the consumer-repo
# equivalent of this same fix).
# Match every generated *.py (_input.py, _e2e_base.py, _e2e_credential.py,
# _e2e_substitutions.py, …) — not just _input.py — so none lands unformatted.
PY_FILES=$(find examples -path '*/app/generated/*.py' | sort)
if [ -n "$PY_FILES" ]; then
  echo ""
  echo ":: Linting and formatting generated Python files..."
  if command -v uvx &> /dev/null; then
    echo "$PY_FILES" | xargs uvx ruff check --fix --quiet
    echo "$PY_FILES" | xargs uvx ruff format
  elif command -v ruff &> /dev/null; then
    echo "$PY_FILES" | xargs ruff check --fix --quiet
    echo "$PY_FILES" | xargs ruff format
  else
    echo "WARNING: ruff not found — skipping Python lint/format."
  fi
fi

echo ""
echo "All examples regenerated successfully."
