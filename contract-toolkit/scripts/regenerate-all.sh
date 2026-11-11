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
  rm -rf "${example_dir}/app/generated" "${example_dir}/tests/e2e/generated"
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

# No ruff pass here: the toolkit emits ruff-stable Python itself
# (src/PyFormat.pkl), so the committed examples are exactly what an app's
# `pkl eval` writes. scripts/check-ruff-stable.py proves it in CI; formatting
# here would hide a renderer that regressed.

echo ""
echo "All examples regenerated successfully."
