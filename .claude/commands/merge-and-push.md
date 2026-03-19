# /merge-and-push — Merge, Commit, and Push Changes to Correct Branch

Use this command to collect all related changes, commit them to the correct branch, and push.

---

## Steps

### 1. Identify Current State

```bash
git status
git branch --show-current
git log --oneline -5
```

- Note the current branch and any uncommitted changes.
- Determine the **target branch** (default: current feature branch; if on `main`, confirm with user before proceeding).

---

### 2. Ensure a Feature Branch

- **Always create a new feature branch** before committing. Never commit directly to `main` or `master`.
- If already on a feature branch (e.g., `feat/*`, `fix/*`), proceed.
- If on `main` or `master`, create a new branch based on the change type:
  ```bash
  git checkout -b <type>/<short-description>
  ```
  Use `feat/`, `fix/`, `refactor/`, `chore/`, `docs/`, or `test/` as the prefix.
- If changes belong to a different existing branch, stash them (`git stash`), checkout that branch, and pop the stash.

---

### 3. Run Pre-Commit Checks

Before staging, run pre-commit on all changed files:

```bash
uv run pre-commit run --files <changed files>
```

- If pre-commit fails, **fix the issues first** before continuing.
- CI will fail if pre-commit checks are not passing — never skip this step.

---

### 4. Check Tests and Docs

Review the diff for changes that may require test or doc updates.

**Tests — check if any of the following are true:**
- New functions, methods, or classes were added
- Existing function signatures or return values changed
- New config fields or capabilities added
- Bug was fixed (regression test warranted)

If yes, **stop and ask the user:**
> "The changes include [describe what changed]. Do you want me to update or add tests before committing? (yes / no / already done)"

- If **yes**: make the test changes, then continue.
- If **no** or **already done**: continue without blocking.
- **Never modify existing tests without explicit approval** — only add new ones unless the user says otherwise.

**Docs — check if any of the following are true:**
- A new capability, config field, or SDK feature was added
- A public API or interface changed
- A `docs/` entry is clearly outdated given the changes
- Module changes require documentation updates per `.cursor/rules/documentation.mdc`

If yes, **stop and ask the user:**
> "The changes may need a docs update in [specific file]. Do you want me to update the docs before committing? (yes / no / already done)"

- If **yes**: make the doc changes, then continue.
- If **no** or **already done**: continue without blocking.

---

### 5. Stage Related Changes

- Review `git diff` and `git status` to understand what changed.
- Stage only files that are part of the current task:
  ```bash
  git add <specific files>
  ```
- **Never use `git add -A` or `git add .`** — always stage specific files to avoid committing unrelated changes or secrets.

---

### 6. Commit

- Write a concise commit message describing **why** the changes were made.
- Use Conventional Commits format per `.cursor/rules/commits.mdc`:
  ```bash
  git commit -m "<type>: <short description>"
  ```
- Types: `feat`, `fix`, `refactor`, `chore`, `docs`, `test`
- **Never add Co-Authored-By lines** — this is a repo rule in CLAUDE.md.

---

### 7. Push

- Push to the remote tracking branch:
  ```bash
  git push -u origin <current-branch>
  ```
- If the push is rejected (non-fast-forward), **do not force push**. Pull with rebase first:
  ```bash
  git pull --rebase origin <current-branch>
  ```
  Then push again.

---

### 8. Confirm

- Run `git log --oneline -3` to confirm the commit landed.
- Report the commit SHA and branch to the user.

---

## Hard Rules

- **Never force push** to `main` or `master`.
- **Never skip pre-commit hooks** (`--no-verify`).
- **Always run pre-commit** before committing — CI depends on it.
- **Always stage specific files** — never `git add -A` blindly.
- **Never add Co-Authored-By lines** to commits.
- **Always use a feature branch** — never commit directly to `main` or `master`.
- **Confirm with user** before merging across branches.
