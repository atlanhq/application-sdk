// Self-hosted (admin) Renovate config for the central fleet runner defined in
// .github/workflows/renovate.yaml.
//
// Everything here is an ADMIN-ONLY option — it can never be set from a
// repository's renovate.json, by design. That restriction is exactly why the
// fleet has to leave the Mend-hosted app to run postUpgradeTasks: the
// allowedPostUpgradeCommands allowlist below only exists for a runner we own.
//
// Consumed via RENOVATE_CONFIG_FILE. Per-repo upgrade policy still lives in each
// consumer's renovate.json (which extends renovate-config/default.json); this
// file carries only the trust/execution settings the runner itself must own.

module.exports = {
  platform: "github",

  // The matrix passes exactly one "owner/repo" per job as a positional CLI
  // argument (`renovate "$TARGET_REPO"`). Never autodiscover: it is unreliable
  // with GitHub App installation tokens, and an explicit per-job repo keeps each
  // run's GitHub API budget isolated.
  autodiscover: false,

  // Belt-and-suspenders scope guard. Discovery only passes repos whose
  // renovate.json already extends the shared preset, but if a non-adopter ever
  // slips into the matrix, never open an unsolicited onboarding PR: skip it.
  // requireConfig=required + onboarding=false => a repo with no renovate.json is
  // skipped cleanly (no onboarding PR, no default-config run).
  onboarding: false,
  requireConfig: "required",

  // Disable the github-actions manager for the fleet run. GitHub requires an App
  // token to hold `workflows: write` to modify any file under .github/workflows/,
  // and the atlan-app-fleet App deliberately does NOT hold it (fleet-wide
  // workflow-write is a supply-chain surface we don't want). Instead, app repos'
  // workflow/action pins are owned by the bootstrap pipeline and propagated from
  // the application-sdk templates — a push, not a per-repo Renovate PR. So the
  // fleet runner needs only contents + pull-requests write.
  //
  // Manager-level disable (not an enabledManagers allowlist, which would risk
  // silently dropping a manager we forgot to list; and not a packageRule
  // enabled:false, which the shared preset's github-actions rule would override
  // on merge). A disabled manager never extracts, so no workflow file is touched.
  "github-actions": { enabled: false },

  // Authorize exactly one post-upgrade command, nothing else. (This option was
  // renamed from `allowedPostUpgradeCommands` to `allowedCommands`.)
  //
  // The pkl-sync driver: a bare PATH executable with NO ${VARS}, because
  // Renovate does not shell-expand post-upgrade commands, so any ${VAR} would be
  // passed literally (the pilot caught exactly that). The workflow installs it as
  // /usr/local/bin/renovate-pkl-sync. Child processes the driver spawns (pkl,
  // uvx ruff, git) need no entry — only top-level commands are vetted.
  //
  // The entry is matched against the raw command strings in the shared preset's
  // postUpgradeTasks; keep the two in step. A command the allowlist does not
  // match is skipped with a log line and nothing else, so the failure is silent
  // — .github/scripts/check_renovate_allowed_commands.py guards the pairing.
  //
  // A `uv lock --exclude-newer` entry briefly lived in this list (FND-367), to
  // enforce a 7-day dependency cooldown on the lock-refresh lane. It is gone, and
  // nothing should replace it. Two reasons, in increasing order of importance.
  //
  // Mechanically, this list is a bad home for a security control: a command the
  // allowlist does not match is skipped with a log line and nothing else, so when
  // a config-resolution race meant the entry was not yet live, the lock refreshed
  // unbounded and pulled a package published three minutes earlier — with no red
  // check anywhere to show for it. A control whose failure mode is silent is not
  // a control.
  //
  // Substantively, the cooldown itself was dropped fleet-wide. This fleet fixes
  // vulnerabilities by keeping dependencies continuously current, so any
  // release-age bound on this lane is a delay on every security fix — paid in
  // exchange for a supply-chain window that no plausible duration meaningfully
  // closes. See the lockFileMaintenance description in default.json.
  allowedCommands: [
    "^renovate-pkl-sync --contract-dir contract --regenerate (true|false) --no-commit$",
  ],
};
