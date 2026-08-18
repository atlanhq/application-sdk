# Conventional Commits

This project follows the [Conventional Commits](https://www.conventionalcommits.org/) specification for commit messages.

## Commit Message Format

Each commit message must follow this structure:

```
<type>[optional scope]: <description>

[optional body]

[optional footer(s)]
```

## Types

- **feat**: A new feature for the user
- **fix**: A bug fix for the user
- **docs**: Documentation only changes
- **style**: Changes that do not affect the meaning of the code (formatting, missing semi-colons, etc.)
- **refactor**: A code change that neither fixes a bug nor adds a feature
- **perf**: A code change that improves performance
- **test**: Adding missing tests or correcting existing tests
- **build**: Changes that affect the build system or external dependencies
- **ci**: Changes to CI configuration files and scripts
- **chore**: Other changes that don't modify src or test files
- **revert**: Reverts a previous commit

## Scope

The scope is optional and should be a noun describing a section of the codebase surrounded by parentheses. Examples:

- `feat(clients)`: Add Redis client connection pooling
- `fix(workflow)`: Resolve metadata extraction timeout issue
- `docs(readme)`: Update installation instructions

## Description

- Use the imperative, present tense: "add" not "added" nor "adds"
- Don't capitalize the first letter
- No period (.) at the end
- Keep it concise (50 characters or less is ideal)

## Body

The body is optional and should provide additional context about the change:

- Use the imperative, present tense
- Explain the motivation for the change
- Contrast with previous behavior
- Wrap at 72 characters

## Footer

The footer is optional and is used for:

- **Breaking Changes**: Must begin with `BREAKING CHANGE:` followed by a description
- **Issue References**: Reference GitHub issues, e.g., `Closes #123`, `Fixes #456`

## Examples

### Simple commit

```
feat: add user authentication
```

### Commit with scope

```
fix(sql): resolve connection pool exhaustion
```

### Commit with body

```
feat(observability): add distributed tracing support

Implement OpenTelemetry integration for end-to-end request tracing.
This enables better debugging and performance analysis across services.
```

### Breaking change

```
feat(clients)!: change authentication method

BREAKING CHANGE: The client authentication now requires OAuth2 tokens
instead of API keys. Update your configuration to use the new auth flow.

Closes #789
```

### Revert commit

```
revert: feat(workflow): add automatic retry logic

This reverts commit abc123def456.
```

## Guidelines

- **Be atomic**: Each commit should represent a single logical change
- **Be descriptive**: The description should clearly explain what changed and why
- **Be consistent**: Follow the same patterns across all commits
- **Reference issues**: Link commits to relevant GitHub issues when applicable
- **Test before committing**: Ensure all tests pass before creating a commit
- **Review the diff**: Always review your changes before committing
- **Scan before pushing**: For Dockerfile or dependency changes, run security scans before pushing (see [build-security.md](build-security.md))

## What CI enforces

CI gates the **PR title**, not the individual commits on your branch.

This repo is squash-merge-only, and the squash subject is the PR title (the
squash body is left blank), so your branch's commit subjects are discarded at
merge. The PR title is the only string that reaches `main` — and the only one
`update_changelog.py` and `release.py` read. Gate it, and the history, the
changelog, and the version bump all follow.

- **`Validate PR title`** (`.github/workflows/pr-title-convention.yaml`) is the
  required check. Its rules are stricter than plain conventional commits: the
  allowed type depends on which component the PR touches, because the title
  decides which changelog and which release a merge lands in. The rules are
  documented at the top of that workflow.
- Connector and app repos get the general, path-agnostic grammar guard instead,
  via the `.github/workflows/commits.yaml` reusable in this repo.

The format above still applies to your commits — it keeps `git log` on a branch
readable and makes a commit easy to lift into a title — but nothing fails a
build over it.

## Tools

Consider using tools to help enforce conventional commits:

- [commitizen](https://github.com/commitizen/cz-cli) - Interactive commit message wizard
- [commitlint](https://commitlint.js.org/) - Lint commit messages
- Git hooks to validate commit messages before accepting them
