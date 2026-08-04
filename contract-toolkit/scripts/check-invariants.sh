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
# 8. CNCT-93: `app_name` is a reserved uiConfig property name. A tenant-facing
#    form field cannot supply it — the extract node bakes the contract `name` so
#    failure logs stay attributable (HYP-1678) — so the arg loops skip it and
#    codegen skips it. Silently dropping the field would leave a live widget on
#    the setup form wired to nothing, and (before the codegen skip) shadow the
#    base Input.app_name with the author's type, failing only at dispatch.
#    Generation must therefore refuse. Asserted here rather than in pkl test
#    because facts cannot express "eval fails".
#
#    Three cases: App.pkl, the kebab-case spelling (toPyName normalises
#    `app-name` → `app_name`), and a contract with `pipeline { extract = null }`
#    — the check is forced from `output`, not from the extract node, so opting
#    out of extract must not smuggle a collision through. Plus NativeApp.pkl,
#    which must not drift from App.pkl on this rule.
# --------------------------------------------------------------------------
echo ":: Checking reserved uiConfig property name invariant (app_name)..."
check_reserved_app_name() {
  local label="$1" body="$2"
  local contract out_dir err
  contract="$(mktemp "$REPO_ROOT/test-reserved-appname-XXXXXX.pkl")"
  out_dir="$(mktemp -d "$REPO_ROOT/test-reserved-appname-out-XXXXXX")"
  printf '%s\n' "$body" > "$contract"
  err="$(pkl eval -m "$out_dir" "$contract" 2>&1 || true)"
  rm -f "$contract"
  rm -rf "$out_dir"
  if ! echo "$err" | grep -q "uses the reserved name"; then
    echo "FAIL: reserved app_name invariant did not fire for $label"
    echo "  Got: $err"
    fail=1
  fi
}

# $1 = uiConfig property key, $2 = pipeline block
app_reserved_body() {
  cat <<PKLEOF
amends "src/App.pkl"

name = "reserved-appname-app"
displayName = "Reserved App Name"
icon = "https://example.com/icon.svg"
hasCredentialConfig = false
pipeline $2

uiConfig = new UIConfig {
  tasks {
    ["Configuration"] {
      inputs { ["$1"] = new TextInput { title = "App Name"; placeholderText = "x" } }
    }
  }
}
PKLEOF
}

check_reserved_app_name "App.pkl (app_name)" \
  "$(app_reserved_body app_name '{ publish = null }')"

# Kebab-case spelling normalises to the same Python name and must also be caught.
check_reserved_app_name "App.pkl (app-name)" \
  "$(app_reserved_body app-name '{ publish = null }')"

# `extract = null` must not let a collision through: the invariant is forced from
# `output`, so it fires even when the extract node is never evaluated.
check_reserved_app_name "App.pkl (extract = null)" \
  "$(app_reserved_body app_name '{ publish = null; extract = null }')"

# Control: the identical contract with a NON-reserved property name must generate
# cleanly. Without this, a check that always errors (for any reason) would pass
# the three assertions above and prove nothing about the reserved name.
echo ":: Checking a non-reserved property name still generates (control)..."
CTRL_CONTRACT="$(mktemp "$REPO_ROOT/test-reserved-ctrl-XXXXXX.pkl")"
CTRL_OUT="$(mktemp -d "$REPO_ROOT/test-reserved-ctrl-out-XXXXXX")"
app_reserved_body source-app-name '{ publish = null }' > "$CTRL_CONTRACT"
CTRL_ERR="$(pkl eval -m "$CTRL_OUT" "$CTRL_CONTRACT" 2>&1 || true)"
rm -f "$CTRL_CONTRACT"
rm -rf "$CTRL_OUT"
if echo "$CTRL_ERR" | grep -q "Pkl Error"; then
  echo "FAIL: control contract with a non-reserved property name failed to generate"
  echo "  Got: $CTRL_ERR"
  fail=1
fi

check_reserved_app_name "NativeApp.pkl" 'amends "src/NativeApp.pkl"

import "src/Connectors.pkl"
import "src/Config.pkl"

name = "reserved-appname-native"
connector = Connectors.POSTGRES
icon = "https://example.com/icon.svg"
workflowType = "PostgresWorkflow"

uiConfig = new Config.UIConfig {
  tasks {
    ["Configuration"] {
      inputs { ["app-name"] = new Config.TextInput { title = "App Name"; placeholderText = "x" } }
    }
  }
}'

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
