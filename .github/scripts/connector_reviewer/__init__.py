"""connector-reviewer: a fixed-cost PR reviewer for the connector app repos.

A fork of lens (`.github/scripts/lens`, the reviewer of application-sdk itself);
`.github/connector-reviewer/FORK.md` lists every intentional difference. The code
lives here and reviews pull requests in other repositories (`atlanhq/atlan-*-app`).

Deterministic code does everything with a determinate answer — which files
are reviewed, how they are grouped, which rules apply, what context the model
sees, where a comment lands, whether a finding survives, when the loop stops.
The model is asked only what is wrong, and is given a small set of capped,
read-only lookups over a prebuilt index instead of a shell.

Standard library only: nothing here adds a dependency to the repo.
"""
