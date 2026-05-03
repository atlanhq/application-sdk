#!/usr/bin/env bash
# Validate invariants on generated output files.
# Runs without any external dependencies (pure bash + python3 ast).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

fail=0

# --------------------------------------------------------------------------
# 1. _input.py: dict[str, Any] must inherit allow_unbounded_fields=True via
#    ExtractionInput or set it explicitly.
# --------------------------------------------------------------------------
echo ":: Checking allow_unbounded_fields invariant..."
for f in $(find examples -name '_input.py'); do
  if grep -q 'dict\[str, Any\]' "$f" \
     && ! grep -q 'allow_unbounded_fields=True' "$f" \
     && ! grep -qE 'class AppInputContract\(ExtractionInput\)' "$f"; then
    echo "FAIL: $f has dict[str, Any] but neither allow_unbounded_fields=True nor ExtractionInput base"
    fail=1
  fi
done

# --------------------------------------------------------------------------
# 2. _input.py: must be valid Python
# --------------------------------------------------------------------------
echo ":: Checking _input.py syntax..."
for f in $(find examples -name '_input.py'); do
  if ! python3 -c "import ast; ast.parse(open('$f').read())" 2>/dev/null; then
    echo "FAIL: $f is not valid Python"
    fail=1
  fi
done

# --------------------------------------------------------------------------
# 3. __init__.py must exist alongside every _input.py
# --------------------------------------------------------------------------
echo ":: Checking __init__.py presence..."
for f in $(find examples -name '_input.py'); do
  init="$(dirname "$f")/__init__.py"
  if [ ! -f "$init" ]; then
    echo "FAIL: missing $init (required for Python imports)"
    fail=1
  fi
done

# --------------------------------------------------------------------------
# 4. All generated JSON files must be valid
# --------------------------------------------------------------------------
echo ":: Checking JSON validity..."
for f in $(find examples -name '*.json' -path '*/generated/*'); do
  if ! python3 -m json.tool "$f" > /dev/null 2>&1; then
    echo "FAIL: $f is not valid JSON"
    fail=1
  fi
done

# --------------------------------------------------------------------------
# Done
# --------------------------------------------------------------------------
if [ "$fail" -ne 0 ]; then
  echo ""
  echo "Invariant checks failed. See errors above."
  exit 1
fi

echo ""
echo "All invariant checks passed."
