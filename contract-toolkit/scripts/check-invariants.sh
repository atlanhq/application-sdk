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
# 6. AgentSelector.includeInManifest must not accept false (Boolean(this)
#    constraint). Setting it to false must raise a type constraint violation
#    at eval time — not be silently ignored — because a missing agent_json
#    slot in the manifest breaks SDR credential routing (atlan-mssql-app#177).
# --------------------------------------------------------------------------
echo ":: Checking AgentSelector.includeInManifest=false raises a constraint violation..."
BAD_CONTRACT="$(mktemp "$REPO_ROOT/test-agent-incl-XXXXXX.pkl")"
cat > "$BAD_CONTRACT" << 'PKLEOF'
import "src/Widgets.pkl"
local widget: Widgets.AgentSelector = new Widgets.AgentSelector {
  includeInManifest = false
}
output {
  text = widget.includeInManifest.toString()
}
PKLEOF
ERR_MSG="$(pkl eval "$BAD_CONTRACT" 2>&1 || true)"
rm -f "$BAD_CONTRACT"
if ! echo "$ERR_MSG" | grep -q "Type constraint"; then
  echo "FAIL: AgentSelector.includeInManifest=false should raise a type constraint violation"
  echo "  Got: $ERR_MSG"
  fail=1
fi

# --------------------------------------------------------------------------
# 7. schedules: duplicate ScheduleSpec.name within an entrypoint must throw.
#    `name` is the reconcile identity key downstream (Local Marketplace keys AE
#    triggers on it); duplicates would collapse to one trigger (last wins), so the
#    Listing has an isDistinct constraint that must fire at eval time.
# --------------------------------------------------------------------------
echo ":: Checking schedules duplicate-name invariant..."
BAD_CONTRACT="$(mktemp "$REPO_ROOT/test-dup-sched-XXXXXX.pkl")"
OUT_DIR="$(mktemp -d "$REPO_ROOT/test-dup-sched-out-XXXXXX")"
cat > "$BAD_CONTRACT" << 'PKLEOF'
amends "src/App.pkl"

name = "dup-sched-app"
displayName = "Dup Sched App"
icon = "https://example.com/icon.svg"
hasCredentialConfig = false
pipeline { publish = null }

// A uiConfig makes the toolkit emit manifest.json, whose triggers.schedules render
// reads `schedules` — pkl is lazy, so the constraint only fires once schedules is
// evaluated (which every real app does when it generates a manifest).
uiConfig = new UIConfig {
  tasks {
    ["Configuration"] {
      inputs { ["target"] = new TextInput { title = "Target"; placeholderText = "x" } }
    }
  }
}

schedules {
  new ScheduleSpec { name = "dup"; cronExpression = "0 0 * * *" }
  new ScheduleSpec { name = "dup"; cronExpression = "0 6 * * *" }
}
PKLEOF
ERR_MSG="$(pkl eval -m "$OUT_DIR" "$BAD_CONTRACT" 2>&1 || true)"
rm -f "$BAD_CONTRACT"
rm -rf "$OUT_DIR"
if ! echo "$ERR_MSG" | grep -q "isDistinct"; then
  echo "FAIL: duplicate schedule-name invariant did not fire (expected an isDistinct constraint violation)"
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
