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
# 9. artifactSchemas (ADR-0020): the three declarations that must fail to
#    generate rather than render a declaration that checks nothing.
#
#    - A path/prefix/glob key. Declarations are keyed by the contract field the
#      runtime is materialising; path-shape inference is what let the earlier
#      upload-time hook match nothing and validate zero records.
#    - An empty `fields` listing. Reports as declared, asserts nothing — the
#      "looks adopted, validates nothing" state the capability exists to remove.
#    - Duplicate field names within a schema, which would silently collapse to
#      one assertion.
#
#    Asserted here rather than in pkl test because facts cannot express
#    "eval fails".
# --------------------------------------------------------------------------
echo ":: Checking artifactSchemas invariants..."

# $1 = artifactSchemas block body
artifact_schemas_body() {
  cat <<PKLEOF
amends "src/App.pkl"

name = "artifact-schemas-invariant-app"
displayName = "Artifact Schemas Invariant App"
icon = "https://example.com/icon.svg"
hasCredentialConfig = false
pipeline { publish = null }

uiConfig = new UIConfig {
  tasks {
    ["Configuration"] {
      inputs { ["target"] = new TextInput { title = "Target"; placeholderText = "x" } }
    }
  }
}

artifactSchemas {
$1
}
PKLEOF
}

check_artifact_schemas() {
  local label="$1" expected="$2" body="$3"
  local contract out_dir err
  contract="$(mktemp "$REPO_ROOT/test-artifact-schemas-XXXXXX.pkl")"
  out_dir="$(mktemp -d "$REPO_ROOT/test-artifact-schemas-out-XXXXXX")"
  printf '%s\n' "$body" > "$contract"
  err="$(pkl eval -m "$out_dir" "$contract" 2>&1 || true)"
  rm -f "$contract"
  rm -rf "$out_dir"
  if ! echo "$err" | grep -q "$expected"; then
    echo "FAIL: artifactSchemas invariant did not fire for $label"
    echo "  Got: $err"
    fail=1
  fi
}

check_artifact_schemas "path-shaped key" 'A-Za-z_' "$(artifact_schemas_body '  ["artifacts/raw/*.parquet"] = new ArtifactSchema {
    format = "parquet"
    fields { new ArtifactField { name = "QUERY_ID"; type = "string"; description = "d" } }
  }')"

check_artifact_schemas "empty fields" 'isEmpty' "$(artifact_schemas_body '  ["raw_queries"] = new ArtifactSchema {
    format = "parquet"
    fields {}
  }')"

check_artifact_schemas "duplicate field names" 'isDistinct' "$(artifact_schemas_body '  ["raw_queries"] = new ArtifactSchema {
    format = "parquet"
    fields {
      new ArtifactField { name = "QUERY_ID"; type = "string"; description = "d" }
      new ArtifactField { name = "QUERY_ID"; type = "int"; description = "d" }
    }
  }')"

# Control: the same contract with a valid declaration must generate cleanly, and
# must actually emit the artifact. Without this, a check that always errors (for
# any reason) would pass the three assertions above and prove nothing.
echo ":: Checking a valid artifactSchemas declaration still generates (control)..."
AS_CTRL_CONTRACT="$(mktemp "$REPO_ROOT/test-artifact-schemas-ctrl-XXXXXX.pkl")"
AS_CTRL_OUT="$(mktemp -d "$REPO_ROOT/test-artifact-schemas-ctrl-out-XXXXXX")"
artifact_schemas_body '  ["raw_queries"] = new ArtifactSchema {
    format = "parquet"
    fields { new ArtifactField { name = "START_TIME"; type = "timestamp"; description = "d" } }
  }' > "$AS_CTRL_CONTRACT"
AS_CTRL_ERR="$(pkl eval -m "$AS_CTRL_OUT" "$AS_CTRL_CONTRACT" 2>&1 || true)"
if echo "$AS_CTRL_ERR" | grep -q "Pkl Error"; then
  echo "FAIL: control contract with a valid artifactSchemas declaration failed to generate"
  echo "  Got: $AS_CTRL_ERR"
  fail=1
elif [ ! -f "$AS_CTRL_OUT/app/generated/artifact_schemas.json" ]; then
  echo "FAIL: control contract generated no app/generated/artifact_schemas.json"
  fail=1
fi
rm -f "$AS_CTRL_CONTRACT"
rm -rf "$AS_CTRL_OUT"

check_artifact_schemas "missing field description" '`description` is required' "$(artifact_schemas_body '  ["raw_queries"] = new ArtifactSchema {
    format = "parquet"
    fields { new ArtifactField { name = "START_TIME"; type = "timestamp" } }
  }')"

check_artifact_schemas "empty field description" 'isEmpty' "$(artifact_schemas_body '  ["raw_queries"] = new ArtifactSchema {
    format = "parquet"
    fields { new ArtifactField { name = "START_TIME"; type = "timestamp"; description = "" } }
  }')"

# A nested path whose container is undeclared drops exactly the check that catches a
# producer which flattened the container away — the leaf assertion can still pass
# against a flattened record. And a container declared with a scalar type is a
# contradiction the validator would have to resolve by guessing.
check_artifact_schemas "nested path with undeclared container" 'without their container' "$(artifact_schemas_body '  ["raw_queries"] = new ArtifactSchema {
    format = "ndjson"
    fields { new ArtifactField { name = "attributes.name"; type = "string"; description = "d" } }
  }')"

check_artifact_schemas "element path with undeclared element" 'without their container' "$(artifact_schemas_body '  ["raw_queries"] = new ArtifactSchema {
    format = "ndjson"
    fields {
      new ArtifactField { name = "columns"; type = "array"; description = "d" }
      new ArtifactField { name = "columns[].name"; type = "string"; description = "d" }
    }
  }')"

check_artifact_schemas "named-member step into a scalar container" 'descends into' "$(artifact_schemas_body '  ["raw_queries"] = new ArtifactSchema {
    format = "ndjson"
    fields {
      new ArtifactField { name = "attributes"; type = "string"; description = "d" }
      new ArtifactField { name = "attributes.name"; type = "string"; description = "d" }
    }
  }')"

check_artifact_schemas "[] step into a non-array container" 'descends into' "$(artifact_schemas_body '  ["raw_queries"] = new ArtifactSchema {
    format = "ndjson"
    fields {
      new ArtifactField { name = "columns"; type = "struct"; description = "d" }
      new ArtifactField { name = "columns[]"; type = "struct"; description = "d" }
    }
  }')"

# Index selection is not a declaration — a schema describes every element, not one of
# them — so `columns[0]` must fail the path grammar rather than be read as `columns[]`.
check_artifact_schemas "index selection in a path" 'Type constraint' "$(artifact_schemas_body '  ["raw_queries"] = new ArtifactSchema {
    format = "ndjson"
    fields { new ArtifactField { name = "columns[0]"; type = "struct"; description = "d" } }
  }')"

# Control: a fully-declared array of structs must generate cleanly. Without this, the
# four refusals above would pass even if every element path were rejected outright.
echo ":: Checking a fully-declared array of structs still generates (control)..."
AS_ARR_CONTRACT="$(mktemp "$REPO_ROOT/test-artifact-schemas-arr-XXXXXX.pkl")"
AS_ARR_OUT="$(mktemp -d "$REPO_ROOT/test-artifact-schemas-arr-out-XXXXXX")"
artifact_schemas_body '  ["transformed_entities"] = new ArtifactSchema {
    format = "ndjson"
    fields {
      new ArtifactField { name = "columns"; type = "array"; description = "d" }
      new ArtifactField { name = "columns[]"; type = "struct"; description = "d" }
      new ArtifactField { name = "columns[].name"; type = "string"; description = "d" }
    }
  }' > "$AS_ARR_CONTRACT"
AS_ARR_ERR="$(pkl eval -m "$AS_ARR_OUT" "$AS_ARR_CONTRACT" 2>&1 || true)"
if echo "$AS_ARR_ERR" | grep -q "Pkl Error"; then
  echo "FAIL: control contract with a fully-declared array of structs failed to generate"
  echo "  Got: $AS_ARR_ERR"
  fail=1
elif ! grep -q '"columns\[\].name"' "$AS_ARR_OUT/app/generated/artifact_schemas.json"; then
  echo "FAIL: control contract did not render the element path columns[].name"
  fail=1
fi
rm -f "$AS_ARR_CONTRACT"
rm -rf "$AS_ARR_OUT"

# A bundle root has no contract model, so a key there could not name a real
# FileReference field, and the file it would emit could be picked up as a fallback
# for an entrypoint that declares nothing. Generation must refuse.
echo ":: Checking artifactSchemas on a bundle root is refused..."
AS_BUNDLE_CONTRACT="$(mktemp "$REPO_ROOT/test-artifact-schemas-bundle-XXXXXX.pkl")"
AS_BUNDLE_OUT="$(mktemp -d "$REPO_ROOT/test-artifact-schemas-bundle-out-XXXXXX")"
cat > "$AS_BUNDLE_CONTRACT" << 'PKLEOF'
amends "src/App.pkl"

import "examples/artifact-schemas/app.pkl" as Sub

name = "artifact-schemas-bundle"
displayName = "Artifact Schemas Bundle"
icon = "https://example.com/icon.svg"
hasCredentialConfig = false

entrypoints {
  new Entrypoint { name = "sub"; displayName = "Sub"; contract = Sub }
}

artifactSchemas {
  ["raw_queries"] = new ArtifactSchema {
    format = "parquet"
    fields { new ArtifactField { name = "START_TIME"; type = "timestamp"; description = "d" } }
  }
}
PKLEOF
AS_BUNDLE_ERR="$(pkl eval -m "$AS_BUNDLE_OUT" "$AS_BUNDLE_CONTRACT" 2>&1 || true)"
rm -f "$AS_BUNDLE_CONTRACT"
rm -rf "$AS_BUNDLE_OUT"
if ! echo "$AS_BUNDLE_ERR" | grep -q "multi-entrypoint bundle root"; then
  echo "FAIL: artifactSchemas on a bundle root was not refused"
  echo "  Got: $AS_BUNDLE_ERR"
  fail=1
fi

# Control: a contract declaring NO artifactSchemas must not emit the artifact.
# This is the fleet-wide byte-identical requirement — every existing consumer
# repo regenerates against this toolkit and must see no new file.
echo ":: Checking an app with no artifactSchemas emits no artifact (control)..."
AS_NONE_CONTRACT="$(mktemp "$REPO_ROOT/test-artifact-schemas-none-XXXXXX.pkl")"
AS_NONE_OUT="$(mktemp -d "$REPO_ROOT/test-artifact-schemas-none-out-XXXXXX")"
cat > "$AS_NONE_CONTRACT" << 'PKLEOF'
amends "src/App.pkl"

name = "artifact-schemas-absent-app"
displayName = "Artifact Schemas Absent App"
icon = "https://example.com/icon.svg"
hasCredentialConfig = false
pipeline { publish = null }

uiConfig = new UIConfig {
  tasks {
    ["Configuration"] {
      inputs { ["target"] = new TextInput { title = "Target"; placeholderText = "x" } }
    }
  }
}
PKLEOF
pkl eval -m "$AS_NONE_OUT" "$AS_NONE_CONTRACT" > /dev/null 2>&1 || true
if [ -f "$AS_NONE_OUT/app/generated/artifact_schemas.json" ]; then
  echo "FAIL: an app declaring no artifactSchemas still emitted artifact_schemas.json"
  fail=1
fi
rm -f "$AS_NONE_CONTRACT"
rm -rf "$AS_NONE_OUT"

# --------------------------------------------------------------------------
# 10. CONNECT-1081: legacyWorkflowTypes on a multi-entrypoint bundle root must throw.
#    The root re-exports each entrypoint contract's already-generated files and emits
#    no manifest.json of its own, so aliases declared there would reach no manifest at
#    all. Left lazy, the declaration would vanish silently and resurface much later as
#    conformance drift pointing at the wrong file. The check is forced from `output`,
#    which the root always evaluates. Asserted here rather than in a pkl test because
#    facts cannot express "eval fails".
# --------------------------------------------------------------------------
echo ":: Checking legacyWorkflowTypes bundle-root placement invariant..."
BAD_CONTRACT="$(mktemp "$REPO_ROOT/test-root-alias-XXXXXX.pkl")"
OUT_DIR="$(mktemp -d "$REPO_ROOT/test-root-alias-out-XXXXXX")"
cat > "$BAD_CONTRACT" << 'PKLEOF'
amends "src/App.pkl"

name = "root-alias-bundle"
displayName = "Root Alias Bundle"
icon = "https://example.com/icon.svg"
hasCredentialConfig = false

entrypoints {
  new Entrypoint {
    name = "crawler"
    displayName = "Crawler"
  }
}

legacyWorkflowTypes {
  new LegacyWorkflowTypeSpec { alias = "RootAliasWorkflow"; entrypoint = "crawler" }
}
PKLEOF
ERR_MSG="$(pkl eval -m "$OUT_DIR" "$BAD_CONTRACT" 2>&1 || true)"
rm -f "$BAD_CONTRACT"
rm -rf "$OUT_DIR"
if ! echo "$ERR_MSG" | grep -q "cannot be declared on a multi-entrypoint bundle root"; then
  echo "FAIL: legacyWorkflowTypes bundle-root invariant did not fire with expected message"
  echo "  Got: $ERR_MSG"
  fail=1
fi

# Control: the same aliases on a single-entrypoint contract must generate cleanly.
# Without this, a contract that errors for an unrelated reason would pass the assertion
# above and prove nothing about the placement rule.
echo ":: Checking legacyWorkflowTypes on a single-entrypoint contract still generates (control)..."
CTRL_CONTRACT="$(mktemp "$REPO_ROOT/test-alias-ctrl-XXXXXX.pkl")"
CTRL_OUT="$(mktemp -d "$REPO_ROOT/test-alias-ctrl-out-XXXXXX")"
cat > "$CTRL_CONTRACT" << 'PKLEOF'
amends "src/App.pkl"

name = "alias-ctrl-app"
displayName = "Alias Control App"
icon = "https://example.com/icon.svg"
hasCredentialConfig = false
pipeline { publish = null }

uiConfig = new UIConfig {
  tasks {
    ["Configuration"] {
      inputs { ["target"] = new TextInput { title = "Target"; placeholderText = "x" } }
    }
  }
}

legacyWorkflowTypes {
  new LegacyWorkflowTypeSpec { alias = "AliasCtrlWorkflow"; entrypoint = "alias-ctrl-app" }
}
PKLEOF
CTRL_ERR="$(pkl eval -m "$CTRL_OUT" "$CTRL_CONTRACT" 2>&1 || true)"
rm -f "$CTRL_CONTRACT"
rm -rf "$CTRL_OUT"
if echo "$CTRL_ERR" | grep -q "Pkl Error"; then
  echo "FAIL: control contract declaring legacyWorkflowTypes failed to generate"
  echo "  Got: $CTRL_ERR"
  fail=1
fi

# --------------------------------------------------------------------------
# 10b. CONNECT-1081: the other no-manifest path. manifest.json is emitted only
#     `when (uiConfig != null)`, so a single-entrypoint contract without a UI also
#     generates nothing for the aliases to land in. The guard must refuse that too,
#     or the declaration vanishes exactly as it would on a bundle root.
# --------------------------------------------------------------------------
echo ":: Checking legacyWorkflowTypes requires a manifest-emitting contract..."
BAD_CONTRACT="$(mktemp "$REPO_ROOT/test-alias-noui-XXXXXX.pkl")"
OUT_DIR="$(mktemp -d "$REPO_ROOT/test-alias-noui-out-XXXXXX")"
cat > "$BAD_CONTRACT" << 'PKLEOF'
amends "src/App.pkl"

name = "alias-noui-app"
displayName = "Alias No UI App"
icon = "https://example.com/icon.svg"
hasCredentialConfig = false
pipeline { publish = null }

legacyWorkflowTypes {
  new LegacyWorkflowTypeSpec { alias = "AliasNoUiWorkflow"; entrypoint = "alias-noui-app" }
}
PKLEOF
ERR_MSG="$(pkl eval -m "$OUT_DIR" "$BAD_CONTRACT" 2>&1 || true)"
rm -f "$BAD_CONTRACT"
rm -rf "$OUT_DIR"
if ! echo "$ERR_MSG" | grep -q "requires a contract that generates a manifest.json"; then
  echo "FAIL: legacyWorkflowTypes no-uiConfig invariant did not fire with expected message"
  echo "  Got: $ERR_MSG"
  fail=1
fi

# --------------------------------------------------------------------------
# 10c. CONNECT-1081: the placement guard must be `local`, not `hidden` — a `hidden`
#     property lands in the amending module's namespace, so a contract could assign
#     it away and generate the very shape the guard exists to refuse.
# --------------------------------------------------------------------------
echo ":: Checking the legacyWorkflowTypes placement guard cannot be amended away..."
BAD_CONTRACT="$(mktemp "$REPO_ROOT/test-alias-override-XXXXXX.pkl")"
OUT_DIR="$(mktemp -d "$REPO_ROOT/test-alias-override-out-XXXXXX")"
cat > "$BAD_CONTRACT" << 'PKLEOF'
amends "src/App.pkl"

name = "alias-override-bundle"
displayName = "Alias Override Bundle"
icon = "https://example.com/icon.svg"
hasCredentialConfig = false

entrypoints {
  new Entrypoint { name = "crawler"; displayName = "Crawler" }
}

legacyWorkflowTypes {
  new LegacyWorkflowTypeSpec { alias = "OverrideAliasWorkflow"; entrypoint = "crawler" }
}

_legacyWorkflowTypesPlacementCheck = null
PKLEOF
ERR_MSG="$(pkl eval -m "$OUT_DIR" "$BAD_CONTRACT" 2>&1 || true)"
rm -f "$BAD_CONTRACT"
rm -rf "$OUT_DIR"
if ! echo "$ERR_MSG" | grep -qE "cannot be declared on a multi-entrypoint bundle root|Cannot find property"; then
  echo "FAIL: the placement guard was amended away and generation succeeded"
  echo "  Got: $ERR_MSG"
  fail=1
fi

# --------------------------------------------------------------------------
# 10d. CONNECT-1081: a control character in an alias must throw. The SDK rejects it at
#     registration (`not char.isprintable()`), so accepting it here would generate a
#     clean contract that fails at worker boot — and a control character mangles logs
#     and the Temporal UI in the meantime.
# --------------------------------------------------------------------------
echo ":: Checking legacyWorkflowTypes control-character invariant..."
BAD_CONTRACT="$(mktemp "$REPO_ROOT/test-alias-ctrlchar-XXXXXX.pkl")"
OUT_DIR="$(mktemp -d "$REPO_ROOT/test-alias-ctrlchar-out-XXXXXX")"
cat > "$BAD_CONTRACT" << 'PKLEOF'
amends "src/App.pkl"

name = "alias-ctrlchar-app"
displayName = "Alias Ctrl Char App"
icon = "https://example.com/icon.svg"
hasCredentialConfig = false
pipeline { publish = null }

uiConfig = new UIConfig {
  tasks {
    ["Configuration"] {
      inputs { ["target"] = new TextInput { title = "Target"; placeholderText = "x" } }
    }
  }
}

legacyWorkflowTypes {
  new LegacyWorkflowTypeSpec { alias = "Legacy\u{7}Workflow"; entrypoint = "alias-ctrlchar-app" }
}
PKLEOF
ERR_MSG="$(pkl eval -m "$OUT_DIR" "$BAD_CONTRACT" 2>&1 || true)"
rm -f "$BAD_CONTRACT"
rm -rf "$OUT_DIR"
if ! echo "$ERR_MSG" | grep -q "Type constraint"; then
  echo "FAIL: control-character alias should raise a type constraint violation"
  echo "  Got: $ERR_MSG"
  fail=1
fi

# --------------------------------------------------------------------------
# 10e. CONNECT-1081: a non-numeric removal version must throw. The SDK parses this
#     with int() per dot-separated part (application_sdk/app/base.py) and refuses
#     registration on anything else, so accepting "4.x.0" here would generate a
#     clean contract whose worker dies at boot.
# --------------------------------------------------------------------------
echo ":: Checking legacyWorkflowTypesRemovalVersion numeric-version invariant..."
BAD_CONTRACT="$(mktemp "$REPO_ROOT/test-alias-badver-XXXXXX.pkl")"
OUT_DIR="$(mktemp -d "$REPO_ROOT/test-alias-badver-out-XXXXXX")"
cat > "$BAD_CONTRACT" << 'PKLEOF'
amends "src/App.pkl"

name = "alias-badver-app"
displayName = "Alias Bad Version App"
icon = "https://example.com/icon.svg"
hasCredentialConfig = false
pipeline { publish = null }

uiConfig = new UIConfig {
  tasks {
    ["Configuration"] {
      inputs { ["target"] = new TextInput { title = "Target"; placeholderText = "x" } }
    }
  }
}

legacyWorkflowTypes {
  new LegacyWorkflowTypeSpec { alias = "AliasBadVerWorkflow"; entrypoint = "alias-badver-app" }
}
legacyWorkflowTypesRemovalVersion = "4.x.0"
PKLEOF
ERR_MSG="$(pkl eval -m "$OUT_DIR" "$BAD_CONTRACT" 2>&1 || true)"
rm -f "$BAD_CONTRACT"
rm -rf "$OUT_DIR"
if ! echo "$ERR_MSG" | grep -q "Type constraint"; then
  echo "FAIL: a non-numeric legacyWorkflowTypesRemovalVersion should raise a type constraint violation"
  echo "  Got: $ERR_MSG"
  fail=1
fi

# --------------------------------------------------------------------------
# 11. CONNECT-1081: a duplicate alias must throw. `alias` is the identity key both in
#     the manifest and in the SDK's `{alias: entrypoint}` mapping, where a repeat would
#     silently collapse to one entry (last wins) and route callers to the wrong entry
#     point. The Listing carries an isDistinct constraint that must fire at eval time.
# --------------------------------------------------------------------------
echo ":: Checking legacyWorkflowTypes duplicate-alias invariant..."
BAD_CONTRACT="$(mktemp "$REPO_ROOT/test-dup-alias-XXXXXX.pkl")"
OUT_DIR="$(mktemp -d "$REPO_ROOT/test-dup-alias-out-XXXXXX")"
cat > "$BAD_CONTRACT" << 'PKLEOF'
amends "src/App.pkl"

name = "dup-alias-app"
displayName = "Dup Alias App"
icon = "https://example.com/icon.svg"
hasCredentialConfig = false
pipeline { publish = null }

uiConfig = new UIConfig {
  tasks {
    ["Configuration"] {
      inputs { ["target"] = new TextInput { title = "Target"; placeholderText = "x" } }
    }
  }
}

legacyWorkflowTypes {
  new LegacyWorkflowTypeSpec { alias = "dup"; entrypoint = "dup-alias-app" }
  new LegacyWorkflowTypeSpec { alias = "dup"; entrypoint = "dup-alias-app" }
}
PKLEOF
ERR_MSG="$(pkl eval -m "$OUT_DIR" "$BAD_CONTRACT" 2>&1 || true)"
rm -f "$BAD_CONTRACT"
rm -rf "$OUT_DIR"
if ! echo "$ERR_MSG" | grep -q "isDistinct"; then
  echo "FAIL: duplicate-alias invariant did not fire (expected an isDistinct constraint violation)"
  echo "  Got: $ERR_MSG"
  fail=1
fi

# --------------------------------------------------------------------------
# 12. Streaming batch knobs are inert unless streaming.enabled is on, the wait
#     is inert at batch size 1 (the shard never waits for a batch of one). Both
#     are silent no-ops in AE, which is the worst failure mode for a latency knob:
#     the contract reads as tuned and behaves as default, discoverable only from a
#     latency graph. The hidden _streamingShapeCheck must fire at eval time.
#     Asserted here rather than in pkl test because facts cannot express "eval fails".
# --------------------------------------------------------------------------
echo ":: Checking streaming batch-config invariants..."

check_streaming_throw() {
  # $1 = human label, $2 = triggerConfig body, $3 = expected substring
  local label="$1" cfg_body="$2" expect="$3"
  local BAD_CONTRACT OUT_DIR ERR_MSG
  BAD_CONTRACT="$(mktemp "$REPO_ROOT/test-stream-XXXXXX.pkl")"
  OUT_DIR="$(mktemp -d "$REPO_ROOT/test-stream-out-XXXXXX")"
  cat > "$BAD_CONTRACT" << PKLEOF
amends "src/App.pkl"

name = "stream-cfg-app"
displayName = "Stream Cfg App"
streamingWorkflowTypeOverride = "stream-cfg-app:cdc-stream"
icon = "https://example.com/icon.svg"
hasCredentialConfig = false
pipeline { publish = null }

// A uiConfig makes the toolkit emit manifest.json, whose triggers.events render
// references the hidden validator — pkl is lazy, so the throw only fires once the
// trigger_config is actually rendered (which every real app does).
uiConfig = new UIConfig {
  tasks {
    ["Configuration"] {
      inputs { ["target"] = new TextInput { title = "Target"; placeholderText = "x" } }
    }
  }
}

events {
  new EventTriggerSpec {
    name = "cdc-user-entity"
    source = new EventSource { name = "atlan-kafka"; topic = "app.cdc.user_entity" }
    triggerConfig = new EventTriggerConfig {
${cfg_body}
    }
  }
}
PKLEOF
  ERR_MSG="$(pkl eval -m "$OUT_DIR" "$BAD_CONTRACT" 2>&1 || true)"
  rm -f "$BAD_CONTRACT"
  rm -rf "$OUT_DIR"
  if ! echo "$ERR_MSG" | grep -q "$expect"; then
    echo "FAIL: $label"
    echo "  Got: $ERR_MSG"
    fail=1
  fi
}

check_streaming_throw \
  "batchSize without streaming.enabled should throw" \
  "      streaming { batchSize = 200 }" \
  "require enabled = true"

check_streaming_throw \
  "batchWaitSeconds without streaming.enabled should throw" \
  "      streaming { batchWaitSeconds = 2.5 }" \
  "require enabled = true"

check_streaming_throw \
  "eventsPerSignal without streaming.enabled should throw" \
  "      streaming { eventsPerSignal = 100 }" \
  "require enabled = true"

check_streaming_throw \
  "batchWaitSeconds at batch size 1 should throw" \
  "      streaming {
        enabled = true
        batchWaitSeconds = 2.5
      }" \
  "meaningless at batchSize = 1"

# Streaming triggers whose DAG has no streaming workflow type: AE would signal the
# shard, the shard would dispatch the BATCH workflow with no events in its arguments,
# and nothing would error. Observed end-to-end on a tenant before this check existed.
check_streaming_throw_nowftype() {
  local label="$1" expect="$2"
  local BAD_CONTRACT OUT_DIR ERR_MSG
  BAD_CONTRACT="$(mktemp "$REPO_ROOT/test-stream-XXXXXX.pkl")"
  OUT_DIR="$(mktemp -d "$REPO_ROOT/test-stream-out-XXXXXX")"
  sed -e 's|^streamingWorkflowTypeOverride.*$||' /dev/null > /dev/null 2>&1 || true
  cat > "$BAD_CONTRACT" << 'PKLEOF'
amends "src/App.pkl"

name = "stream-nowftype-app"
displayName = "Stream NoWfType App"
icon = "https://example.com/icon.svg"
hasCredentialConfig = false
pipeline { publish = null }

uiConfig {
  tasks {
    ["Configuration"] {
      inputs {
        ["target"] = new TextInput { title = "Target" }
      }
    }
  }
}

events {
  new EventTriggerSpec {
    name = "cdc"
    source = new EventSource { name = "atlan-kafka"; topic = "example.cdc" }
    triggerConfig = new EventTriggerConfig {
      streaming { enabled = true }
    }
  }
}
PKLEOF
  ERR_MSG="$(cd "$REPO_ROOT" && pkl eval -m "$OUT_DIR" "$BAD_CONTRACT" 2>&1 || true)"
  if echo "$ERR_MSG" | grep -q "$expect"; then
    echo "  ok: $label"
  else
    echo "FAIL: $label"
    echo "$ERR_MSG" | head -5
    fail=1
  fi
  rm -f "$BAD_CONTRACT"; rm -rf "$OUT_DIR"
}

check_streaming_throw_nowftype \
  "streaming without streamingWorkflowTypeOverride should throw" \
  "no streamingWorkflowTypeOverride"

# ackPaths is an at-least-once durability assertion; the streaming path writes no
# acks and has no watchdog backstop, so declaring both must be refused rather than
# silently voided. The batch-knob cases above cost latency; this one costs events.
check_streaming_throw \
  "ackPaths declared with streaming.enabled should throw" \
  "      ackPaths { \"\$.extract.outputs.ack_path\" }
      streaming { enabled = true }" \
  "ackPaths is inert when streaming.enabled"

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
