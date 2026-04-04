# Create PR

Generate a pull request with proper title, description, and conventional commit format.

## Source of Truth

Read these before starting:
- `docs/agents/commits-prs.md` — commit format and PR expectations
- `.cursor/rules/commits.mdc` — Conventional Commits specification

## Instructions

1. **Analyze branch changes**:
   - `git diff main --stat` — files changed
   - `git log main..HEAD --oneline` — commit history
   - `git diff main...HEAD` — full diff from branch point
2. **Determine PR type**: `feat`, `fix`, `docs`, `refactor`, `test`, `build`, `ci`, `chore`
3. **Generate title** (<72 chars, imperative mood):
   - Format: `<type>[optional scope]: <description>`
   - Example: `feat(clients): add Snowflake SQL client`
4. **Generate body** with sections:
   - **Summary**: 1-3 bullet points describing the change
   - **Changes**: Detailed list of what was modified
   - **Testing**: How the changes were tested
   - **Documentation**: Any docs updated
5. **Create PR**: `gh pr create --title "<title>" --body "<body>"`
6. **IMPORTANT**: Never add Co-Authored-By lines (per CLAUDE.md rule)

$ARGUMENTS
