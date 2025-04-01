# Validate PR Title Action

This GitHub Action validates pull request titles against defined patterns. It ensures that PR titles follow both Jira issue key requirements and Conventional Commits specification.

## Inputs

| Input | Required | Description |
|-------|----------|-------------|
| `github-token` | Yes | The GitHub token to use for the action |

## What it does

1. Jira issue key validation:
    - Checks for presence of a Jira issue key (e.g., ABC-123)
    - Allows the key to be anywhere in the title
2. Conventional Commits validation:
    - Ensures PR title follows semantic commit conventions
    - Validates format: `type: description`
    - Enforces standard commit types (feat, fix, etc.)

## Usage Example

```yaml
name: Validate PR Title
on:
  pull_request:
    types: [opened, edited, synchronize]

jobs:
  validate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Validate PR Title
        uses: ./.github/actions/validate-pr-title
        with:
          github-token: ${{ secrets.GITHUB_TOKEN }}
```

## Valid PR Title Examples

```
feat: APP-3214 Add user authentication
fix: APP-1234 Resolve memory leak issue
docs: APP-5678 Update API documentation
chore: APP-9012 Update dependencies
refactor: APP-3456 Restructure authentication logic
```

## Requirements

- PR title must include a valid Jira issue key
- PR title must follow the Conventional Commits format:
  - Must start with a type (feat, fix, docs, etc.)
  - Must include a description
  - Must include a Jira issue key
  - Should be clear and descriptive

## Error Handling

The action will fail the check if:

1. No Jira issue key is found in the PR title
2. The PR title doesn't follow Conventional Commits format
3. Invalid commit type is used