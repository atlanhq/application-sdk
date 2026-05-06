---
name: capability-manifest
description: >
  Create or audit-and-refresh the canonical capability manifest for application-sdk —
  a single scannable document listing every public symbol (import path, signature, docstring
  summary) plus every typed Input/Output contract. Run when starting a new agent task
  that needs a fast picture of what the SDK exposes, when SDK code has changed, or on
  a 30-day cadence.
mandatory_triggers:
  - "/capability-manifest"
  - "audit the SDK manifest"
  - "refresh the capability manifest"
  - "is the SDK manifest stale?"
optional_triggers:
  - "what does the SDK expose"
  - "list public methods of the SDK"
owner: connector-platform-team
last_updated: "2026-05-04"
staleness_days: 30
inputs:
  - mode: "create | refresh | verify (auto-detected from existing state)"
outputs:
  - docs/agents/sdk-capabilities.md
gates: []
---

# capability-manifest

Create or audit-and-refresh `docs/agents/sdk-capabilities.md` — a deterministically-rendered
inventory of every public SDK symbol plus every typed Pydantic contract.

Reference: `references/format-spec.md` for exact output format; `references/extractor.py` for scripts.

---

## Phase 0 — Discover state

**Check for existing manifest:**

```bash
ls docs/agents/sdk-capabilities.md 2>/dev/null && echo "EXISTS" || echo "CREATE"
```

- Does not exist → **create flow** (Phases 1–4 below).
- Exists → **audit-and-refresh flow** (Steps A–F below).

**Pre-flight: dirty-tree check (both flows):**

```bash
git status --porcelain application_sdk/
```

If output is non-empty: **stop**. Report:
> Working tree has uncommitted changes under `application_sdk/`. Commit or stash them, then re-run.
> Reason: the embedded `source-sha` must faithfully identify the extracted code.

---

## Phases 1–3 — Extract, normalize, render

```bash
uv run poe regen-capabilities
```

This poe task (defined in `pyproject.toml`) runs the full extract → normalize → render pipeline,
enforces idempotence via `cmp`, and runs pre-commit non-Python hooks (trailing-whitespace,
fix-byte-order-marker, check-merge-conflict). Equivalent to
the raw four-command sequence below if you need to run steps individually:

<details>
<summary>Raw commands (for debugging)</summary>

```bash
mkdir -p /tmp/capability-manifest
EXTRACTOR=.claude/skills/capability-manifest/references/extractor.py
PURPOSES=.claude/skills/capability-manifest/references/subpackage-purposes.yaml
uv run --with griffe python "$EXTRACTOR" dump > /tmp/capability-manifest/raw.json
uv run --with griffe python "$EXTRACTOR" normalize /tmp/capability-manifest/raw.json > /tmp/capability-manifest/normalized.json
uv run --with griffe python "$EXTRACTOR" render /tmp/capability-manifest/normalized.json "$PURPOSES" > /tmp/capability-manifest/fresh1.md
uv run --with griffe python "$EXTRACTOR" render /tmp/capability-manifest/normalized.json "$PURPOSES" > /tmp/capability-manifest/fresh2.md
cmp /tmp/capability-manifest/fresh1.md /tmp/capability-manifest/fresh2.md \
  && echo "IDEMPOTENCE OK" \
  || { echo "IDEMPOTENCE FAILURE — fix extractor before proceeding"; exit 1; }
cp /tmp/capability-manifest/fresh1.md docs/agents/sdk-capabilities.md
```

</details>

**CI integration:** the drift-detector workflow (`.github/workflows/capability-manifest-check.yaml`)
runs `uv run poe regen-capabilities` on every PR push and fails if the committed manifest differs from
the regenerated output. To trigger an automatic regeneration commit, comment `/regen-manifest` on
the PR — the slash-command workflow (`.github/workflows/capability-manifest-regen.yaml`) will push
the updated file as `github-actions[bot]`.

---

## Phase 4 — Validate coverage

```bash
uv run --with griffe python - <<'EOF'
import ast, json
from pathlib import Path

SUBPACKAGES = ["app","clients","common","contracts","credentials","execution",
               "handler","infrastructure","observability","outputs","server",
               "storage","templates","testing","transformers"]

with open("/tmp/capability-manifest/normalized.json") as f:
    data = json.load(f)

total_all = 0
total_in_manifest = 0
for pkg in SUBPACKAGES:
    init = Path("application_sdk") / pkg / "__init__.py"
    if not init.exists(): continue
    tree = ast.parse(init.read_text())
    all_names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "__all__":
                    if isinstance(node.value, (ast.List, ast.Tuple)):
                        all_names = [e.value for e in node.value.elts
                                     if isinstance(e, ast.Constant)]
    if not all_names: continue
    in_manifest = {s["name"] for s in data["subpackages"].get(pkg, {}).get("symbols", [])}
    missing = [n for n in all_names if n not in in_manifest]
    total_all += len(all_names)
    total_in_manifest += len(all_names) - len(missing)
    if missing:
        print(f"  MISSING from {pkg}: {missing}")

n_contracts = sum(len(v) for v in data["contracts"].values())
print(f"Coverage: {total_in_manifest}/{total_all} __all__ entries in manifest")
print(f"Contracts: {n_contracts} models across {len(data['contracts'])} namespaces")
print(f"Subpackages: {len(data['subpackages'])}")
EOF
```

---

## Staleness check (verify-only mode)

When asked "is the SDK manifest stale?" without running a full refresh:

```bash
MANIFEST_SHA=$(awk '/^source-sha:/{print $2}' docs/agents/sdk-capabilities.md)
CURRENT_SHA=$(git log -1 --format=%H -- application_sdk/)
echo "Manifest SHA: $MANIFEST_SHA"
echo "Current SHA:  $CURRENT_SHA"
[ "$MANIFEST_SHA" = "$CURRENT_SHA" ] \
  && echo "STATUS: manifest is current" \
  || echo "STATUS: manifest is stale — run /capability-manifest to refresh"
```

The `staleness_days: 30` frontmatter is a secondary cadence reminder; prefer the SHA check.

---

## Audit-and-refresh flow (manifest already exists)

### Step A — Snapshot committed file, then re-render

`poe regen-capabilities` overwrites `docs/agents/sdk-capabilities.md` in place as its last step,
so the committed version must be snapshotted *before* the poe task runs — otherwise Step B's
diff compares the already-overwritten file against itself and always reports no drift.

```bash
cp docs/agents/sdk-capabilities.md /tmp/capability-manifest/committed.md
uv run poe regen-capabilities
```

### Step B — Compare

```bash
diff -u /tmp/capability-manifest/committed.md docs/agents/sdk-capabilities.md
```

- Empty diff → **manifest is current**. Report "no drift" and exit.
- Non-empty diff → move to Step C.

### Step C — Bucket the drift

Read the diff output and categorise:

| Bucket | Signal in diff |
|---|---|
| **Added** | `+#### \`SomeName\`` lines not in original |
| **Removed** | `-#### \`SomeName\`` lines not in fresh |
| **Signature drift** | `- **Signature:**` / `+ **Signature:**` pairs for same symbol |
| **Summary drift** | `- **Summary:**` / `+ **Summary:**` pairs for same symbol |

Report counts per bucket. If in-depth bucketing is too brittle, count diff hunks and note "N sections changed".

### Step D — Apply

`poe regen-capabilities` already wrote the updated file in Step A. No copy needed here.

The skill **never auto-commits**. Leave the diff for the user to review.

### Step E — Report

```
Capability manifest refresh
  Status:          drift detected; manifest updated
  Added:           N  (<names>)
  Removed:         N  (<names>)
  Signature drift: N
  Summary drift:   N
  Coverage:        all N __all__ entries present
  Idempotence:     re-render matches; safe to commit
  Next:            review `git diff docs/agents/sdk-capabilities.md` and commit
```

### Step F — Staleness update

Update `last_updated` in this SKILL.md's frontmatter to today's date after a successful refresh.

---

## Subpackage purposes

Edit `references/subpackage-purposes.yaml` to update purpose lines. Re-run the skill after editing.
The YAML key is the short name (e.g., `app`), not the full import path.

## Troubleshooting

- **griffe doesn't find application_sdk** — ensure you're running from the repo root.
- **Idempotence failure** — check for `datetime.now()`, `random`, or dict-ordering issues in `extractor.py`.
- **Missing symbols** — symbol not in `__all__`? Not exposed at subpackage level? Check the `__init__.py`.
- **Dirty-tree refusal** — stash or commit changes under `application_sdk/` before running.
- **"No drift" when CI says stale** — most likely the committed snapshot was not saved before running poe (Step A). The `poe regen-capabilities` task overwrites `docs/agents/sdk-capabilities.md` in place; if you diff the file against itself it always looks clean. Verify with `git diff HEAD docs/agents/sdk-capabilities.md` — if that shows drift, commit the file.
- **Fallback** — if griffe fails, use `ast`-only mode: parse source files with `ast.FunctionDef`/`ast.ClassDef`
  and note the fallback in `references/retro-log.md`.
