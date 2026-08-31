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
last_updated: "2026-08-28"
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
uv run --with griffe==2.1.0 python "$EXTRACTOR" dump > /tmp/capability-manifest/raw.json
uv run --with griffe==2.1.0 python "$EXTRACTOR" normalize /tmp/capability-manifest/raw.json > /tmp/capability-manifest/normalized.json
uv run --with griffe==2.1.0 python "$EXTRACTOR" render /tmp/capability-manifest/normalized.json "$PURPOSES" > /tmp/capability-manifest/fresh1.md
uv run --with griffe==2.1.0 python "$EXTRACTOR" render /tmp/capability-manifest/normalized.json "$PURPOSES" > /tmp/capability-manifest/fresh2.md
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

Checks every public module that declares `__all__` — package `__init__` files *and*
submodules. Checking only the `__init__` files is what let the manifest omit
`application_sdk.execution.heartbeat` (and with it `run_in_thread`) while reporting
full coverage (FND-439).

```bash
uv run --with griffe==2.1.0 python - <<'EOF'
import ast, json, sys
from pathlib import Path

sys.path.insert(0, ".claude/skills/capability-manifest/references")
# The same walk and the same skip rule the dump uses, so the two cannot disagree.
from extractor import _rendered_as_contract, discover_public_modules

with open("/tmp/capability-manifest/normalized.json") as f:
    data = json.load(f)

total = covered = as_contract = 0
gaps = []
for subpkg, dotted, all_names in discover_public_modules():
    emitted = {s["name"] for s in data["subpackages"].get(subpkg, {}).get("symbols", [])}
    total += len(all_names)
    for name in all_names:
        if name in emitted:
            covered += 1
        elif _rendered_as_contract(dotted, name, data["contracts"]):
            # Deliberately absent from Section 2: Section 3 renders it with its fields.
            as_contract += 1
        else:
            gaps.append(f"{dotted}.{name}")

n_contracts = sum(len(v) for v in data["contracts"].values())
print(f"Coverage: {covered}/{total} __all__ entries in Section 2")
print(f"          + {as_contract} rendered in the Contracts section instead")
print(f"Contracts: {n_contracts} models across {len(data['contracts'])} namespaces")
print(f"Subpackages: {len(data['subpackages'])}")
print(f"UNEXPLAINED GAPS: {gaps or 'none'}")
EOF
```

`covered + rendered-in-Contracts` must equal `total`, leaving `UNEXPLAINED GAPS: none`.
Every name is accounted for by name rather than by a prose caveat about which shortfall
is expected, so a real omission cannot hide behind the contract re-exports.

There is no "module skipped" outcome to check for: `dump` exits non-zero if a module
declares `__all__` and griffe cannot see it (an ancestor directory missing an
`__init__.py` makes it an implicit namespace package, which griffe does not descend
into). Runtime introspection is deliberately *not* used as a fallback there — an export
behind an optional extra would then appear or vanish with the installed extras, breaking
the drift check for anyone who ran `uv sync --all-extras`.

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
- **Drift you did not cause, in `Field(...)` defaults** — check the griffe version. griffe renders
  those defaults, so it is pinned (`griffe==2.1.0`) everywhere it is invoked: the `regen-capabilities`
  poe task, the raw commands above, and Phase 4. Unpinned, the committed manifest becomes a function
  of whichever griffe the machine last cached — griffe 2.2.0 changed the parenthesisation of a call
  inside a `Field` default, which silently rewrote ten committed lines with no source change and then
  reported as drift on every PR built on an older cache. The `cmp` idempotence gate cannot catch this:
  it proves one machine agrees with itself, not that two machines agree with each other. Bump the pin
  deliberately, in all three files at once, and regenerate in the same commit — and observe the org §5
  release-age cooldown when choosing the version, exactly as for any other dependency.
- **`source-date` flips between `Z` and `+00:00`** — expected and harmless. It comes from
  `git log --format=%cI`, whose UTC rendering changed across git versions. The drift check excludes
  `source-date` (along with `source-sha` and `sdk-version`), so this never fails CI.
- **Missing symbols** — symbol not in `__all__`? Not exposed at subpackage level? Check the `__init__.py`.
- **Dirty-tree refusal** — stash or commit changes under `application_sdk/` before running.
- **"No drift" when CI says stale** — most likely the committed snapshot was not saved before running poe (Step A). The `poe regen-capabilities` task overwrites `docs/agents/sdk-capabilities.md` in place; if you diff the file against itself it always looks clean. Verify with `git diff HEAD docs/agents/sdk-capabilities.md` — if that shows drift, commit the file.
- **Fallback** — if griffe fails, use `ast`-only mode: parse source files with `ast.FunctionDef`/`ast.ClassDef`
  and note the fallback in `references/retro-log.md`.
