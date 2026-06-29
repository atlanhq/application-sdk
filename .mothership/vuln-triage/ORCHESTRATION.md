# Vuln Triage — Orchestration Playbook

Follow ALL stages (0–5). NEVER stop early. NEVER defer work to "later."

This rover is now **autonomous**: it does not merely recommend remediations, it
**enacts the SLA flow** so newly-detected CVEs need zero human touch until an SLA
nears expiry. Concretely, for each CVE you:

- **Critical / High** → open an **allowlist PR** that starts the remediation SLA
  clock (entry `expires` = `RUN_DATE` + the severity SLA), so the build gate stays
  green now and only tightens as expiry nears.
- **Medium / Low** → **never** allowlisted (the validator rejects them and they
  are not gated). Detail them on the ticket with their SLA only.
- Then act per case: a dependency fix → a **bump PR**; otherwise the remediation
  detail goes on the ticket.

**What you may do (and is expected):**
- Commit allowlist entries and open the **allowlist PR** (Critical/High).
- Open the **bump PR** for Case 1.
- Label both PRs `vuln-auto-merge` so the deterministic GHA gate
  (`.github/workflows/vuln-auto-merge.yml`) can approve + auto-merge them once
  required CI is green.

**Hard limits (the GHA gate enforces these — do not rely on them being lenient):**
- An **allowlist PR** must touch **ONLY** `.security/base-allowlist.json`.
- A **bump PR** must touch **ONLY** a subset of `pyproject.toml`, `uv.lock`,
  `requirements.txt`. If a Case-1 fix also needs source-code changes, the bump PR
  no longer matches the gate's path-allowlist — open it **without** the
  `vuln-auto-merge` label (as a draft) and tag a human. The gate is the safety
  boundary; these instructions are not.
- Never push to `main`. Never `apk`-patch the Dockerfile / base image. Never set
  the ticket to "Done" (reconciliation closes it once a new SDK release no longer
  ships the CVE).

Every CVE exits this run in exactly one state, fully written into the ticket:
- **ALLOWLISTED+PR** — Critical/High Case 1: allowlist PR + bump PR, both linked.
- **ALLOWLISTED** — Critical/High Case 2/3/4: allowlist PR + remediation detail.
- **TRACKED** — Medium/Low: ticket detail + SLA, no allowlist.
- **KILLED** — not present in the SDK / not our issue, with a documented reason.

Print `[Stage N/6 complete]` after each stage.

## SLA windows (from first detection)

| Severity | CVSS | Remediation SLA | Allowlisted? |
|----------|------|-----------------|--------------|
| Critical | 9.0–10.0 | **7 days**  | Yes |
| High     | 7.0–8.9  | **30 days** | Yes |
| Medium   | 4.0–6.9  | 90 days     | No (ticket only) |
| Low      | 0.1–3.9  | 180 days    | No (ticket only) |

The allowlist `_expiry_policy` (CRITICAL 7, HIGH 30) is enforced by
`validate_allowlist.py`; the entry `expires` date IS the SLA deadline.

## Time Budget

Hard stop: **60 minutes**. If approaching, skip to Stage 5 and post the summary
with whatever is triaged so far.

---

## Stage 0: Preflight

The dispatch prompt passes run context directly — read it, do NOT re-derive:

```
TICKET, SEVERITY, RUN_DATE, GHA_RUN_URL
```

1. **Set working directory + auth** (mothership cloned the repo on `main`):
   ```bash
   cd /workspace/application-sdk
   echo "$GITHUB_TOKEN" | gh auth login --with-token
   uv sync --all-extras 2>/dev/null &   # warm deps for a possible Case-1 bump
   ```
2. **Read the ticket** `${TICKET}` via the Linear proxy (see tools.md) — title,
   description, and the embedded vuln table / `<!-- vuln-ids: ... -->` marker.
   The ticket is scoped to one severity (`${SEVERITY}`).
3. **Read in-repo assets**: `.mothership/vuln-triage/CLAUDE.md`,
   `.mothership/vuln-triage/tools.md`, and the current
   `.security/base-allowlist.json` (to avoid duplicating an existing entry).

Print: `[Stage 0/6 complete] ticket=${TICKET} severity=${SEVERITY}`

---

## Stage 1: Enumerate every CVE

Do NOT trust only the ticket title. Download the scan's raw artifacts
(`security-scan-raw-results`, see tools.md) and build the authoritative list of
**every unique CVE** across the image scan and the filesystem scan:

```bash
jq -r '.Results[]?.Vulnerabilities[]?
  | "\(.Severity)|\(.VulnerabilityID)|\(.PkgName)|\(.InstalledVersion)|\(.FixedVersion // "N/A")"' \
  trivy-image-results.json trivy-fs-results.json | sort -u
```

Track, per CVE, which file flagged it (`trivy-fs-results.json` vs
`trivy-image-results.json`) — this is half the classification signal.

Print: `[Stage 1/6 complete] <N> unique CVEs to triage`

---

## Stage 2: Root-cause each CVE

For each CVE, gather the facts (most come straight from the Trivy JSON — do not
re-derive by hand what the scan already gives you):

- **Package** = `.PkgName`, **Reference ID** = `.VulnerabilityID`
- **Installed (frozen) version** = `.InstalledVersion`
- **Fixed version** = `.FixedVersion` (`"N/A"` if none)
- Read the advisory (`.PrimaryURL` / `.References`) to understand what the
  exposure relies on.

Then determine the two facts that drive classification in Stage 3:

1. **Is this package one of OUR dependencies?** — i.e. is it in our resolved
   dependency graph, not merely baked into the base image?
   ```bash
   grep -i "name = \"<pkg>\"" uv.lock && echo "OURS (in uv.lock)"
   grep -i "<pkg>" pyproject.toml && echo "declared in pyproject.toml"
   ```
   - Flagged by **`trivy-fs-results.json`** and/or present in `uv.lock` → **OUR
     Python dependency** (bumpable).
   - Flagged **only** by `trivy-image-results.json` and **not** in `uv.lock`
     (an OS/apk package or something baked into the image) → **base-image** CVE.
2. **Is a fix available?** — `FixedVersion` present vs `N/A`.

If a CVE is not present in the SDK at all (stale, or only in a consumer app) →
**KILL** with a documented reason on the ticket (no allowlist entry).

Print: `[Stage 2/6 complete] <N> root-caused`

---

## Stage 3: Classify each CVE (decision tree — be precise)

Walk this tree per CVE. The classification MUST be unambiguous; it sets the
`case` field on the allowlist entry and the remediation detail on the ticket.

```
Is the package one of OUR dependencies (in uv.lock / pyproject.toml)?
│
├── YES → it is bumpable on our side
│   │
│   ├── FixedVersion available?
│   │   ├── YES → CASE 1: bump PR (Stage 4a) — we can fix this by bumping.
│   │   └── NO  → is upstream still maintained?
│   │           ├── YES → CASE 2: no-fix, upstream alive (Stage 4b)
│   │           └── NO  → CASE 3: no-fix, upstream dead (Stage 4c)
│
└── NO → the vulnerable package lives in the BASE IMAGE, not our deps
        → CASE 4: base-image rebuild (Stage 4d). NOT a dependency bump,
          NOT an apk patch.
```

> **The two forks are orthogonal — "fix available" is NOT "we bump uv.lock".**
> A CVE can have a fix *and* be a Case-4 base-image rebuild — e.g. **Dapr** ships
> in `app-runtime-base:3`, not `uv.lock`, so a fixed Dapr CVE is Case 4 (rebuild),
> never Case 1 (bump).

Print: `[Stage 3/6 complete] <c1> case1, <c2> case2, <c3> case3, <c4> case4, <k> killed`

---

## Stage 4: Act per case

### 4-pre. Allowlist PR (Critical / High — EVERY case, do this first)

For every Critical/High CVE — regardless of case, **including Case 1** — open
(or extend) the **allowlist PR**. This starts the SLA clock and keeps the gate
green. **Skip entirely for Medium/Low** (never allowlisted).

```bash
git checkout main && git clean -fd 2>/dev/null && git checkout -- . 2>/dev/null
git checkout -b chore/allowlist-<cve-id> origin/main
# edit ONLY .security/base-allowlist.json — add one entry per CVE:
```

Entry (minimal but descriptive — see `_entry_schema` in the file):

```json
"<CVE-ID>": {
  "package": "<pkg>",
  "severity": "CRITICAL",            // or HIGH
  "reason": "Case <n>: <one-sentence resolution>.",
  "expires": "<RUN_DATE + 7 (CRITICAL) or 30 (HIGH) days>",
  "added_by": "@atlan-ci (vuln-triage rover)",
  "case": <1|2|3|4>,
  "ticket": "<TICKET>"
}
```

`reason` examples (keep to one sentence — the case + how it resolves):
- Case 1: `"Case 1: fixed by dependency bump (PR linked on ticket); clears once merged & released."`
- Case 2: `"Case 2: no upstream fix; vulnerable path not exercised by the SDK."`
- Case 3: `"Case 3: upstream unmaintained; migrating to <alternative> (see ticket)."`
- Case 4: `"Case 4: base-image CVE; awaiting app-runtime-base:3 rebuild."`

Then push and open the PR (label it so the gate can auto-merge it):

```bash
git add .security/base-allowlist.json && git commit -m "chore(security): allowlist <CVE-IDs> with <severity> SLA"
git push origin chore/allowlist-<cve-id>
gh pr create --repo atlanhq/application-sdk --base main \
  --title "chore(security): allowlist <CVE-IDs> (<severity>, SLA <N>d)" \
  --label "vuln-auto-merge" \
  --body "<per-CVE: case, package, expires/SLA, ticket link, GHA_RUN_URL>"
```

Validate locally before pushing: `uv run python .github/scripts/validate_allowlist.py`
(it rejects Medium/Low, past dates, over-window dates, and missing case/ticket).

### 4a. Case 1 — bump PR (our dependency, fix available)

In addition to the allowlist entry above, open the **bump PR**:

```bash
git checkout main && git checkout -b fix/bump-<pkg>-<version>-<cve-id> origin/main
```

1. Follow the **uv recipe** in tools.md: pin uv to main's lock format; only edit
   `pyproject.toml` if its range doesn't cover the fixed version; then
   `uv sync --all-extras --all-groups --upgrade` and
   `uv export --no-hashes --frozen > requirements.txt`. The PR must touch
   **only** `pyproject.toml`, `uv.lock`, `requirements.txt`.
2. Run the **validation greps + pre-commit + unit tests** (tools.md).
3. **If the bump needs source-code changes, or any check fails:** the PR no
   longer fits the auto-merge path-allowlist. Open it **as a draft, WITHOUT the
   `vuln-auto-merge` label**, detail the blocker on the ticket, and tag a human.
   Do not force it.
4. Otherwise commit (Conventional Commits, NO Co-Authored-By), push, and open a
   **non-draft, labelled** PR so the gate auto-merges it once CI is green:
   ```bash
   gh pr create --repo atlanhq/application-sdk --base main \
     --title "fix(security): bump <pkg> to <version> to resolve <CVE-IDs>" \
     --label "vuln-auto-merge" \
     --body "<CVEs resolved, fixed versions, changelog impact, validation grep results, GHA_RUN_URL>"
   ```
5. Link both PRs (allowlist + bump) on the ticket. When the bump merges and the
   SDK cuts a release that no longer ships the CVE, the release-gated
   reconciliation auto-removes the allowlist entry + closes the ticket — no human
   action needed. (Reconciliation runs on `release: released`, not on the hourly
   scan, so the entry persists for downstream consumers until the fix is shipped.)

### 4b. Case 2 — our dep, no fix, upstream alive

The allowlist entry (4-pre) holds the SLA. Add an **exposure analysis** to the
ticket: does the SDK actually exercise the vulnerable path? State the conclusion
explicitly and put it in the entry `reason`.

### 4c. Case 3 — our dep, no fix, upstream dead

The allowlist entry holds the SLA. Detail on the ticket: why no bump is possible
(upstream archived/unmaintained — link the evidence), and a proposed alternative
(maintained replacement / vendoring) with trade-offs.

### 4d. Case 4 — base-image rebuild (not our dependency)

The allowlist entry holds the SLA. The fix is a Chainguard rebuild of
`app-runtime-base:3` — **never an `apk` patch, never a PR in this repo.** Detail
on the ticket whether Chainguard has a fixed version and that the remediation is
"rebuild + republish `app-runtime-base:3`." Tag Vaibhav/Chris — the rebuild is
their action.

Print: `[Stage 4/6 complete] <allow> allowlist PRs, <bump> bump PRs, <draft> human-route drafts`

---

## Stage 5: Summary

1. Update the ticket `${TICKET}` with a per-CVE summary table: CVE | severity |
   package | source (our-dep / base-image) | case | allowlist expiry (SLA) |
   outcome (bump PR link / allowlist-only / awaiting base-image rebuild / killed).
   Include `**Run:** [logs + cost](${GHA_RUN_URL})`.
2. Leave the ticket open. **Never set "Done"** — reconciliation
   (`reconcile_allowlist.py`, run on `release: released`) closes it once a new
   SDK release no longer ships any CVE on it.
3. Security audit: confirm no secrets/tokens leaked into any PR body or ticket.

Print:
```
[Stage 5/6 complete]
========================================
 Vuln Triage — ${TICKET} (${SEVERITY}, ${RUN_DATE})
========================================
CVEs:            <N> triaged
  allowlist PR:  <a>  (Critical/High — SLA started)
  bump PR:       <c1> (Case 1, auto-merge)
  human-route:   <d>  (bump needed source edits / failed checks)
  tracked-only:  <ml> (Medium/Low — no allowlist)
  killed:        <k>
========================================
```

---

## Principles (Non-Negotiable)

- **Allowlist every Critical/High on detection** — the entry `expires` is the SLA
  and keeps the gate green until then. Medium/Low are NEVER allowlisted.
- **Two PR shapes, kept separate**: allowlist PR touches only
  `.security/base-allowlist.json`; bump PR touches only the dep manifests. Label
  both `vuln-auto-merge`. A mixed PR will NOT auto-merge — the GHA path-allowlist
  refuses it (by design).
- **Classify precisely.** "Our dependency (bumpable)" vs "base image (rebuild)" is
  the first fork; "fix available?" is the second. The case sets the entry `case`.
- **Enumerate every CVE** — even ones the ticket title didn't mention.
- **Nothing is deferred** — every CVE is ALLOWLISTED(+PR), TRACKED, or KILLED.
- **Never push to `main`, never `apk`-patch the Dockerfile, never set "Done".**
- **Recommend full `uv sync --upgrade`** for the Case-1 bump (PR #1995 policy).
