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
  if ! python3 -c "import ast, sys; ast.parse(open(sys.argv[1]).read())" "$f" 2>/dev/null; then
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
# 5. App.pkl: hasCredentialConfig=true without connector must throw a clear error.
# --------------------------------------------------------------------------
echo ":: Checking connector invariant (hasCredentialConfig=true requires connector)..."
BAD_CONTRACT="$(mktemp "$REPO_ROOT/test-no-connector-XXXXXX.pkl")"
OUT_DIR="$(mktemp -d "$REPO_ROOT/test-no-connector-out-XXXXXX")"
cat > "$BAD_CONTRACT" << 'PKLEOF'
amends "src/App.pkl"

name = "bad-app"
displayName = "Bad App"
icon = "https://example.com/icon.svg"
hasCredentialConfig = true

uiConfig = new UIConfig {
  tasks {
    ["Credential"] {
      inputs {
        ["credential-guid"] = new CredentialInput {
          credType = "atlan-connectors-bad-app"
        }
      }
    }
  }
}
PKLEOF
ERR_MSG="$(pkl eval -m "$OUT_DIR" "$BAD_CONTRACT" 2>&1 || true)"
rm -f "$BAD_CONTRACT"
rm -rf "$OUT_DIR"
if ! echo "$ERR_MSG" | grep -q "connector must be set when hasCredentialConfig = true"; then
  echo "FAIL: connector invariant did not fire with expected message"
  echo "  Got: $ERR_MSG"
  fail=1
fi

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
