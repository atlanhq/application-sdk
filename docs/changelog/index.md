# Changelog

The SDK's release notes live in
[CHANGELOG.md](https://github.com/atlanhq/application-sdk/blob/main/CHANGELOG.md)
at the repository root. The release workflow generates it: never edit it by hand.

`CHANGELOG.md` holds only the current major version's releases. On the release
that bumps the major, the workflow moves every section from an older major into
`docs/changelog/v<major>.md` in
[this directory](https://github.com/atlanhq/application-sdk/tree/main/docs/changelog),
unchanged and newest first, one file per major. Those archives aren't built into
this site. Read them on GitHub.

## How a section is generated and checked

1. `release.yaml` runs `.github/scripts/update_changelog.py` on every merge to `main`.
   It prepends a `## v<version> (<date>)` section built from the conventional
   commit subjects since the last tag. It drops `ci:` commits.
2. With `--archive-dir docs/changelog`, the same script moves older-major sections
   out, as described above. It's idempotent: once `CHANGELOG.md` holds only the
   current major, it changes nothing until the next major bump.
3. `.github/scripts/lint_changelog.py <version>` checks the new section before the
   bump branch is pushed. The heading must appear exactly once, the section must
   have at least one bullet, and every bullet must carry a `(#N)` PR reference.
4. On release, `.github/scripts/extract_release_notes.py <version>` publishes that
   exact section as the GitHub release body.

See [Release Flow](../standards/release-flow.md) for the full release train.
