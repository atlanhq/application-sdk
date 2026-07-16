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

  // The matrix passes exactly one "owner/repo" per job via RENOVATE_REPOSITORIES.
  // Never autodiscover: it is unreliable with GitHub App installation tokens, and
  // an explicit per-job repo keeps each run's GitHub API budget isolated.
  autodiscover: false,

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

  // Authorize ONLY the pkl-sync driver as a post-upgrade command. (This option
  // was renamed from `allowedPostUpgradeCommands` to `allowedCommands`.) The
  // allowlist is matched against the raw command string in the shared preset's
  // postUpgradeTasks (before ${SDK_SCRIPTS} expansion). Child processes the
  // driver itself spawns (pkl, uvx ruff, git) need no entry here — the allowlist
  // vets only the top-level commands Renovate is asked to run.
  allowedCommands: [
    '^python3 ".*/renovate_pkl_sync\\.py" --contract-dir contract --regenerate (true|false) --no-commit$',
  ],

  // Resolves the ${SDK_SCRIPTS} reference in the preset's postUpgradeTasks
  // command to the checked-out application-sdk/.github/scripts directory. The
  // workflow exports SDK_SCRIPTS before invoking renovate; the `|| ""` keeps
  // this a string (Renovate rejects a non-string customEnvVariables value) if
  // the config is ever loaded without it set (e.g. renovate-config-validator).
  customEnvVariables: {
    SDK_SCRIPTS: process.env.SDK_SCRIPTS || "",
  },
};
