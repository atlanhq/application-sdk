# Vuln Triage — Orchestration Playbook

Follow ALL stages (0–5). NEVER stop early. NEVER defer work to "later."

**What you are allowed to do:**
- For **Case 1 only** (a dependency *we* control, with a fix version the scan
  confirms) → open a **DRAFT PR** with the bump. Never mark it ready, never merge.
- For **every other case** → detail the triage + recommended remediation on the
  Linear ticket and tag Vaibhav or Chris to act.
- You never merge, never push to `main`, never commit an allowlist entry, never
  edit the Dockerfile / base image.

Every CVE exits this run in exactly one state, fully written into the ticket:
- **DRAFT-PR** — Case 1: a draft PR with the dependency bump, linked on the ticket
- **TRIAGED** — Case 2/3/4: classified + root-caused + a concrete recommendation
  for a human to action
- **KILLED** — not present in the SDK / not our issue, with a documented reason

Print `[Stage N/6 complete]` after each stage.

## Time Budget

Hard stop: **60 minutes**. If approaching, skip to Stage 5 and post the summary
with whatever is triaged so far.

---

## Stage 0: Preflight

The dispatch prompt passes run context directly — read it, do NOT re-derive:

```
TICKET, RUN_DATE, GHA_RUN_URL
```

1. **Set working directory + auth** (mothership cloned the repo on `main`):
   ```bash
   cd /workspace/application-sdk
   echo "$GITHUB_TOKEN" | gh auth login --with-token
   uv sync --all-extras 2>/dev/null &   # warm deps for a possible Case-1 bump
   ```
2. **Read the ticket** `${TICKET}` via the Linear proxy (see tools.md) — title,
   description, and the embedded vuln table / `<!-- vuln-ids: ... -->` marker.
3. **Read in-repo assets**: `.mothership/vuln-triage/CLAUDE.md`,
   `.mothership/vuln-triage/tools.md`.

Print: `[Stage 0/6 complete] ticket=${TICKET}`

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
   # Present in our locked dependency tree → it is ours to bump.
   grep -i "name = \"<pkg>\"" uv.lock && echo "OURS (in uv.lock)"
   grep -i "<pkg>" pyproject.toml && echo "declared in pyproject.toml"
   ```
   - Flagged by **`trivy-fs-results.json`** and/or present in `uv.lock` → **OUR
     Python dependency** (bumpable).
   - Flagged **only** by `trivy-image-results.json` and **not** in `uv.lock`
     (an OS/apk package or something baked into the image) → **base-image** CVE.
2. **Is a fix available?** — `FixedVersion` present vs `N/A`.

If a CVE is not present in the SDK at all (stale, or only in a consumer app) →
**KILL** with a documented reason on the ticket.

Print: `[Stage 2/6 complete] <N> root-caused`

---

## Stage 3: Classify each CVE (decision tree — be precise)

Walk this tree per CVE. The classification MUST be unambiguous; state the chosen
case and *why* on the ticket.

```
Is the package one of OUR dependencies (in uv.lock / pyproject.toml)?
│
├── YES → it is bumpable on our side
│   │
│   ├── FixedVersion available?
│   │   ├── YES → CASE 1: DRAFT PR (Stage 4a) — we can fix this by bumping.
│   │   └── NO  → is upstream still maintained?
│   │           ├── YES → CASE 2: recommend temporary allowlist (Stage 4b)
│   │           └── NO  → CASE 3: recommend an alternative (Stage 4c)
│
└── NO → the vulnerable package lives in the BASE IMAGE, not our deps
        → CASE 4: base-image rebuild (Stage 4d) — NOT a dependency bump,
          NOT an apk patch. Evaluate whether the remediation is simply
          waiting on app-runtime-base being rebuilt.
```

> **The two forks are orthogonal — "fix available" is NOT "we bump uv.lock".**
> The `FixedVersion?` check lives only inside the *our-dependency* branch. The
> base-image branch routes to **Case 4 regardless of fix availability**. A CVE
> can have a fix *and* be a Case-4 base-image rebuild — e.g. **Dapr** ships in
> `app-runtime-base:3`, not `uv.lock`, so a fixed Dapr CVE is Case 4 (rebuild),
> never Case 1 (bump).
>
> The scan runs **without** `--ignore-unfixed` (daily-security-scan.yml), so
> **Cases 2/3 (our dep, no fix) are reachable today** — no-fix CVEs are surfaced
> and ticketed, not filtered at scan time. **Case 4 is also fully reachable** and
> remains the dominant non-Case-1 path. Do not mistake Case 4 for a dead branch
> and prune it.

Print: `[Stage 3/6 complete] <c1> draft-PR, <c2> allowlist, <c3> alternative, <c4> base-image, <k> killed`

---

## Stage 4: Act per case

### 4a. Case 1 — DRAFT PR (our dependency, fix available)

This is the only case where you change code. Open a **draft** PR — you do not
mark it ready or merge; Vaibhav or Chris finalize it.

```bash
git checkout main && git clean -fd 2>/dev/null && git checkout -- . 2>/dev/null
git checkout -b fix/bump-<pkg>-<version>-<cve-id> origin/main
```

1. Follow the **uv recipe** in tools.md: pin uv to main's lock format; only edit
   `pyproject.toml` if its range doesn't cover the fixed version; then
   `uv sync --all-extras --all-groups --upgrade` (full-dep refresh, PR #1995) and
   `uv export --no-hashes --frozen > requirements.txt`.
2. Apply any **code changes the changelog mandates** for upgraded packages; run
   unit tests.
3. Run the **validation greps + pre-commit + unit tests** (tools.md). If any
   fails, attempt ONE fix; if it still fails → do NOT open the PR; detail the
   blocker on the ticket and tag Vaibhav/Chris.
4. Commit (Conventional Commits, NO Co-Authored-By), push, open the PR **as a
   draft**:
   ```bash
   gh pr create --draft --repo atlanhq/application-sdk --base main \
     --title "fix(security): bump <pkg> to <version> to resolve <CVE-IDs>" \
     --label "vulnerabilities" \
     --body "<CVEs resolved, fixed versions, changelog impact, other deps moved, validation grep results, scan link, GHA_RUN_URL>"
   ```
5. Link the draft PR on the ticket and tag Vaibhav/Chris to review + finalize.
   Do **not** comment `@sdk-review`, do **not** mark ready, do **not** merge.

### 4b. Case 2 — recommend temporary allowlist (our dep, no fix, upstream alive)

1. **Exposure analysis:** does the SDK actually exercise the vulnerable path?
   State the conclusion explicitly.
2. Detail on the ticket: the proposed allowlist entry + the exposure analysis.
   **Do not commit it.** Tag Vaibhav/Chris for approval.

### 4c. Case 3 — recommend an alternative (our dep, no fix, upstream dead)

Detail on the ticket: the package, why no bump is possible (upstream
archived/unmaintained — link the evidence), and a proposed alternative
(maintained replacement / vendoring) with trade-offs. Tag Vaibhav/Chris.

### 4d. Case 4 — base-image rebuild (not our dependency)

The vulnerable package is in the base image, not `pyproject.toml`. The fix is a
Chainguard rebuild of `app-runtime-base:3` — **never an `apk` patch, never a PR
in this repo.**

Evaluate and detail on the ticket:
- Has Chainguard already released a fixed package version? Include the version +
  advisory URL.
- Is the remediation simply **waiting on the base image being rebuilt**? If so,
  say that plainly — the SDK fix is "rebuild + republish `app-runtime-base:3`",
  there is nothing to change in this repo.
- Tag Vaibhav (`<@U07UPEUEPU3>`) or Chris (`<@U02T3H7KMSS>`) — the rebuild is
  their action; you do not perform it.

Print: `[Stage 4/6 complete] <prs> draft PRs, <allow> allowlist, <alt> alternatives, <img> base-image rebuilds`

---

## Stage 5: Summary

1. Update the ticket `${TICKET}` with a per-CVE summary table: CVE | severity |
   package | source (our-dep / base-image) | case | outcome (draft PR link /
   recommended allowlist / alternative / awaiting base-image rebuild / killed).
   Include `**Run:** [logs + cost](${GHA_RUN_URL})`.
2. Leave the ticket open. **Never set "Done"** — that happens when a human merges
   the draft PR (Case 1) or completes the rebuild/allowlist/alternative.
3. Security audit: confirm no secrets/tokens leaked into the PR body or ticket.

Print:
```
[Stage 5/6 complete]
========================================
 Vuln Triage — ${TICKET} (${RUN_DATE})
========================================
CVEs:           <N> triaged
  draft PR:     <c1>  (our dep, fix available)
  allowlist:    <c2>  (our dep, no fix, upstream alive)
  alternative:  <c3>  (our dep, no fix, upstream dead)
  base-image:   <c4>  (not our dep → rebuild app-runtime-base)
  killed:       <k>
All awaiting Vaibhav/Chris.
========================================
```

---

## Principles (Non-Negotiable)

- **Classify precisely.** "Our dependency (bumpable)" vs "base image (rebuild)" is
  the first fork; "fix available?" is the second. State the case + why on the ticket.
- **Draft PR only for Case 1**, and only when the scan confirms a fix version for a
  dependency we control. Never mark ready, never merge.
- **Everything else is detailed on the ticket** for Vaibhav/Chris — you never
  commit allowlist entries, never edit the Dockerfile/base image.
- **Enumerate every CVE** — even ones the ticket title didn't mention.
- **Nothing is deferred** — every CVE is DRAFT-PR, TRIAGED, or KILLED by end of run.
- **Recommend full `uv sync --upgrade`** for the Case-1 bump (PR #1995 policy).
- **Never `apk`-patch the Dockerfile**; base-image CVEs → rebuild app-runtime-base.
