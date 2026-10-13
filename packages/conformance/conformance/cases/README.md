# Cases files

One file per migration rule, named by `RuleDefinition.cases_ref` (`cases/P001.md`). It lists every known way to fix the rule as *condition → action*. A known case that needs a person's decision is a case like any other; its action says who decides.

## Layout

1. **Header** (before `## Index`): what the rule protects, the done-bar, how to verify, and rules to also check. Keep it short; it is read for every finding.
2. **`## Index`**: one table row per case, with the columns `id | condition | how to confirm`. `id` is in backticks, lowercase with hyphens.
3. **One `## <id>` section per case**: the steps, a short example diff, and how to verify.

## How a remediator reads it

- The finding carries `atlan/case`: read the header and that one section.
- No `atlan/case`: read the header and the index, check every row, and confirm the row that matches; then read only that section.
- No row matches, or none can be confirmed: no case applies. List each condition and why it failed.

## Checks

`tests/test_cases_files.py` fails when a `cases_ref` file is missing, a section is missing from the index (or the reverse), an index row does not have the three columns, or a checker fixture expects a case id the rule's cases file does not list.

A case the checker can prove gets a case id from the checker and a test fixture that expects it. A site that repeatedly matches no case is a missing case.
