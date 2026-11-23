# Connector reviewer principles for this repository

This folder holds this repository's own principles for the connector reviewer,
the AI reviewer that runs when someone comments `/review` on a pull request.

The reviewer judges a change against two sets of principles:

- The shared fleet principles. They live in application-sdk at
  `.github/connector-reviewer/principles/`:
  https://github.com/atlanhq/application-sdk/tree/main/.github/connector-reviewer/principles
- This repository's own principles. They live in this folder.

## Adding a principle

Write one short Markdown file per principle. Name the first one
`rp01-<slug>.md` and give it the id `RP01`. The next is `rp02-<slug>.md` with
the id `RP02`, and so on.

Each file has this format:

```markdown
---
id: RP01
applies-to: [app, contract]
---
# One sentence naming what must stay true

Principle: <one or two sentences>
Why: <the failure this prevents>
Ask:
- <a question the reviewer should answer about the diff; at most three>
Don't flag: <what CI already enforces, or what is out of scope>
Severity: <what makes it high; otherwise medium>
```

`applies-to` is optional. It names the areas of the repository the principle
covers. Leave it out and the principle applies everywhere. The areas are:

- `app`: `app/`, except generated code
- `generated`: `app/generated/`
- `tests`: the tests
- `contract`: the contract
- `ci`: `.github/`

The full description of the format is kept with the fleet principles, at the
link above.

## What the reviewer reads

The reviewer reads only the files in this folder named `rpNN-*.md`. It ignores
every other file here, including this README.

This folder belongs to this repository. Bootstrap created this README once and
does not overwrite it.
