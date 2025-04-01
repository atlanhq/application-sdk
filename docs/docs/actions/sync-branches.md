# Sync Branches Action

This GitHub Action automatically synchronizes and merges branches in the repository to maintain consistency across different development streams. It's particularly useful for keeping development branches up-to-date with the main branch and maintaining a consistent state across the repository's branches.

## Inputs

| Input           | Required | Description                     |
| --------------- | -------- | ------------------------------- |
| `source-branch` | Yes      | Source branch to sync from      |
| `target-branch` | Yes      | Target branch to sync to        |
| `github-token`  | Yes      | GitHub token for authentication |

## What it does

1. Uses GitHub's API to perform an automatic merge
2. Merges the source branch into the target branch
3. Creates a merge commit with a standardized message
4. Handles the merge operation securely using the provided GitHub token

## Usage Example

```yaml
name: Sync Branches
on:
  # Trigger when main branch is updated
  push:
    branches: [main]
  # Or run on a schedule
  schedule:
    - cron: '0 0 * * *'  # Daily at midnight

jobs:
  sync:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Sync Development Branch
        uses: ./.github/actions/sync-branches
        with:
          source-branch: 'main'
          target-branch: 'development'
          github-token: ${{ secrets.GITHUB_TOKEN }}
```

## Output

The action:

1. Creates a merge commit if the merge is successful
2. Fails if there are merge conflicts
3. Uses the commit message format: "ci: auto-merge {source-branch} into {target-branch}"
4. Provides feedback through GitHub Actions logs about the merge status