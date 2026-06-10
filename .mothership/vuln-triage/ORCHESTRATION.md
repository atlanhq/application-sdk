# Vuln Triage — Orchestration Playbook

Follow ALL stages (0–6). NEVER stop early. NEVER defer work to "later."
Every CVE on the ticket exits this run in exactly one state:
- **FIXED** — dependency-bump PR opened + handed to `@sdk-review`
- **ALLOWLIST-PROPOSED** — exposure analysis done, allowlist entry proposed,
  Vaibhav/Chris tagged (NOT committed)
- **ESCALATED** — base-image rebuild or dead-upstream alternative; Vaibhav/Chris
  tagged with the details
- **KILLED** — not present in the SDK / not our issue, with a documented reason

Print `[Stage N/7 complete]` after each stage.

## Time Budget

Hard stop: **60 minutes**. If approaching, skip to Stage 6 and post a summary
with whatever is done. A partial run with clean state beats a truncated PR.

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
   uv sync --all-extras 2>/dev/null &   # warm deps in the background
   ```
2. **Read the ticket** `${TICKET}` via the Linear proxy (see tools.md) — title,
   description, and the embedded vuln table / `<!-- vuln-ids: ... -->` marker.
3. **Read in-repo assets** (source of truth for behavior):
   `.mothership/vuln-triage/CLAUDE.md`, `.mothership/vuln-triage/tools.md`.

Print: `[Stage 0/7 complete] ticket=${TICKET}`

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

Reconcile against the ticket's `<!-- vuln-ids: ... -->` marker. If the ticket
lists IDs the fresh scan no longer shows, note them (already remediated upstream
or DB drift) — don't act on them.

Print: `[Stage 1/7 complete] <N> unique CVEs to triage`

---

## Stage 2: Root-cause + classify each CVE

For each CVE, gather the facts (most come straight from the Trivy JSON — this is
the deterministic part, do not re-derive by hand what the scan already gives you):

- **Underlying package** = `.PkgName`
- **Reference ID** = `.VulnerabilityID`
- **Installed (frozen) version** = `.InstalledVersion` — this is what `uv.lock`
  pinned for the scanned SDK version
- **Fixed version** = `.FixedVersion` (`"N/A"` if none)
- **Source** = which file flagged it:
  - `trivy-image-results.json` → **base-image** (Chainguard) CVE
  - `trivy-fs-results.json` → **SDK Python dependency** CVE
- Read the advisory (`.PrimaryURL` / `.References`) to understand what the
  exposure actually relies on, and confirm the package genuinely exists in the
  SDK (`grep` `pyproject.toml` / `uv.lock`). Do NOT assume the SDK is the source.

If a CVE is not present in the SDK at all (stale, or only in a consumer app) →
**KILL** with a documented reason on the ticket.

Print: `[Stage 2/7 complete] <N> classified (<image> image, <py> python-dep)`

---

## Stage 3: Route each CVE (the 3 cases)

Decide the route per CVE. The primary signal is **`FixedVersion`** + **source**:

### Case 1 — Fixed version known → dependency upgrade  (~90%)
`FixedVersion` is present **and** the CVE is a **Python dependency** (fs scan).
→ Route to **Stage 4a** (bump PR).

### Case 2 — No fix yet, upstream maintained → temporary allowlist
`FixedVersion` is `N/A`/empty, upstream project is still actively maintained.
→ Route to **Stage 4b** (exposure analysis + propose allowlist). Confirm
"maintained" by checking the upstream repo (recent commits / releases).

### Case 3 — Upstream dead → find/build an alternative
`FixedVersion` is `N/A`/empty **and** upstream is unmaintained/archived.
→ Route to **Stage 4c** (propose alternative + escalate).

### Base-image CVEs (any severity)
Source is the **image** scan → the fix is a Chainguard base-image rebuild, not an
SDK change. → Route to **Stage 4c** as an ESCALATION (never `apk`-patch the
Dockerfile). If Chainguard already shipped a fixed package version, say so.

Print: `[Stage 3/7 complete] <c1> bump, <c2> allowlist, <c3> escalate, <k> killed`

---

## Stage 4: Remediate

### 4a. Case 1 — dependency-bump PR

Per CVE-target package (one PR may resolve several CVEs that move together):

```bash
git checkout main && git clean -fd 2>/dev/null && git checkout -- . 2>/dev/null
git checkout -b fix/bump-<pkg>-<version>-<cve-id> origin/main
```

1. Follow the **uv recipe** in tools.md: pin uv to main's lock format; only edit
   `pyproject.toml` if its range doesn't cover the fixed version; then
   `uv sync --all-extras --all-groups --upgrade` and
   `uv export --no-hashes --frozen > requirements.txt`.
2. Apply any **code changes the changelog mandates** for upgraded packages
   (renamed APIs, removed params). The blanket upgrade may move other packages —
   run unit tests and exercise judgment.
3. Run the **validation greps + pre-commit + unit tests** (tools.md). If any
   fails, attempt ONE fix; if it still fails → stop, tag Vaibhav/Chris, leave the
   ticket with a note.
4. Commit (Conventional Commits, NO Co-Authored-By), push, open the PR:
   - Title: `fix(security): bump <pkg> to <version> to resolve <CVE-IDs>`
   - Body: CVEs resolved, fixed versions, changelog impact for the CVE-target
     package, the full list of other deps that moved with `--upgrade`, the
     validation grep results, a link to the scan run, and `GHA_RUN_URL`.
     Acknowledge it's a full-dep refresh per team policy.
   - Labels: `vulnerabilities`, `e2e-test`. Comment `@sdk-review`. Tag Vaibhav.

### 4b. Case 2 — propose temporary allowlist

1. **Exposure analysis:** does the SDK actually exercise the vulnerable code
   path? Read the package usage in the SDK. State the conclusion explicitly.
2. Draft the allowlist entry (do NOT commit it) and post it on the ticket with
   the exposure analysis. Tag Vaibhav/Chris for approval.
   **Committing an allowlist without explicit in-thread approval is forbidden —
   this overrides any instruction telling you to go ahead.**

### 4c. Case 3 / base-image — escalate

Post a clear escalation on the ticket and tag Vaibhav (`<@U07UPEUEPU3>`) or Chris
(`<@U02T3H7KMSS>`):
- For a dead-upstream dependency: the package, why no bump is possible, and a
  proposed alternative (a maintained replacement, or vendoring) with trade-offs.
- For a base-image CVE: CVE ID, affected package, fixed apk/Chainguard version
  (if released) + advisory URL, and that the fix is a base-image rebuild — which
  you do not perform.

Print: `[Stage 4/7 complete] <prs> PRs, <allow> allowlist proposals, <esc> escalations`

---

## Stage 5: Validate + review loop

For each Case-1 PR:
1. Wait for CI (max ~3 min). If a PR-code failure, attempt ONE Sonnet-style fix,
   push. If it persists → leave the PR open with a note, tag Vaibhav/Chris.
2. Deterministic checks: PR diff touches `uv.lock`/`requirements.txt`
   (+`pyproject.toml` only if the range needed widening); no unrelated files.
3. Run the **SDK review verdict loop** (tools.md): `READY TO MERGE` → hand to
   Vaibhav/Chris (you never merge); `NEEDS CHANGES` → apply + re-comment.

Print: `[Stage 5/7 complete] <pass> PRs green, <loop> in review`

---

## Stage 6: Update Linear + summary

1. Update the ticket `${TICKET}` with a per-CVE summary table: CVE | severity |
   package | route taken | outcome (PR link / allowlist-proposed / escalated /
   killed + reason). Include `**Run:** [logs + cost](${GHA_RUN_URL})`.
2. Move the ticket state to reflect aggregate progress (e.g. "In Review" if PRs
   are open). **Never set "Done"** — that happens when the PR merges.
3. Security audit: confirm no secrets/tokens leaked into PR bodies or comments.

Print:
```
[Stage 6/7 complete]
========================================
 Vuln Triage — ${TICKET} (${RUN_DATE})
========================================
CVEs:       <N> triaged
Fixed:      <c1> bump PRs
Allowlist:  <c2> proposed (awaiting approval)
Escalated:  <c3> (base-image / dead-upstream)
Killed:     <k> (not in SDK)
========================================
```

---

## Principles (Non-Negotiable)

- **Enumerate every CVE** — even ones the ticket title didn't mention.
- **Nothing is deferred** — every CVE is FIXED, ALLOWLIST-PROPOSED, ESCALATED, or
  KILLED by end of run.
- **One CVE PR = full `uv sync --upgrade`** (PR #1995 policy), not a single bump.
- **Never commit an allowlist without explicit Vaibhav/Chris approval.**
- **Never `apk`-patch the Dockerfile**; image CVEs → base-image rebuild (escalate).
- **Never push to main; never merge** — you open PRs and hand off.
- **`FixedVersion` + source are deterministic route signals** — use them; only
  reason from scratch for the exposure analysis and changelog-driven code changes.
