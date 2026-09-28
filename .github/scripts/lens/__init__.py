"""lens: a fixed-cost PR reviewer that runs in this repo's CI.

Deterministic code does everything with a determinate answer — which files
are reviewed, how they are grouped, which rules apply, what context the model
sees, where a comment lands, whether a finding survives, when the loop stops.
The model is asked only what is wrong, and is given a small set of capped,
read-only lookups over a prebuilt index instead of a shell.

Standard library only: nothing here adds a dependency to the repo.
"""
