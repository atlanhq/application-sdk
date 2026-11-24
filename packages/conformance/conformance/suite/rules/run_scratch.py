"""Run-scratch rule definition (P067, FND-5364).

Apps that need a local scratch path for a run often take it from the system
temp dir: ``Path(tempfile.gettempdir()) / <name> / self.run_id``.  In workflow
code the sandbox refuses ``tempfile`` (at import and at call time); anywhere,
``cleanup_files`` removes only the SDK's run scratch dir, so the system temp
dir copy stays on the worker's disk; and a ``/tmp/...`` path falls outside the
SDK's ``./local/tmp/...`` to object-store key mapping.

* ``P067`` TempDirOutsideRunScratch — ``tempfile.gettempdir()``, or
  ``tempfile.mkdtemp`` / ``mkstemp`` with no directory, in app code.

Scope
-----
``app``: ``App.run_scratch_dir`` is the consumer-facing replacement; the SDK
composes its own run scratch dir internally.

P-ids are a permanent public contract (see ``prescriptions.py``).
"""

from __future__ import annotations

from conformance.suite.schema.catalog import (
    RemediationKind,
    RemediationReference,
    RuleDefinition,
)
from conformance.suite.schema.disposition import (
    EnforcementTier,
    RuleScope,
)

_APPS_DOC = "https://github.com/atlanhq/application-sdk/blob/main/docs/concepts/apps.md"

RULES: tuple[RuleDefinition, ...] = (
    RuleDefinition(
        id="P067",
        canonical_reference=(
            "application_sdk/app/base.py `App.run_scratch_dir` — the run's scratch "
            "directory, computed from the run's context with string-only os.path "
            "calls, so it is the same in workflow code and in every task of the run, "
            "and it is the directory `cleanup_files` removes when the run ends. No "
            "reference app reads it yet: the canonical apps still make per-task "
            "directories with mkdtemp and no dir=, which this rule flags ("
            + _APPS_DOC
            + ")."
        ),
        scope=RuleScope.APP,
        name="TempDirOutsideRunScratch",
        tier=EnforcementTier.WARN,
        category="workflow-sandbox",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.61.0",
        rationale=(
            "A run's local files belong in the run's scratch directory, which the "
            "SDK owns. tempfile's system temp dir breaks in workflow code (the "
            "sandbox refuses tempfile), is never removed by cleanup_files (so it "
            "fills the worker's disk run after run), and has no object-store "
            "mapping."
        ),
        short_description=(
            "`tempfile.gettempdir()` / `mkdtemp` / `mkstemp` without `dir=` in app "
            "code (use `self.run_scratch_dir`)"
        ),
        full_description=(
            "App code calls ``tempfile.gettempdir()`` (or ``gettempdirb()``), or\n"
            "``tempfile.mkdtemp(...)`` / ``tempfile.mkstemp(...)`` without a\n"
            "directory, so the run's files land in the system temp dir:\n"
            "\n"
            "* in workflow code the sandbox refuses ``tempfile`` — at call time,\n"
            "  and for a module-level call at import time — so the workflow task\n"
            "  fails;\n"
            "* ``cleanup_files`` removes only the run's scratch directory, so\n"
            "  ``/tmp/<app>/<run>`` stays on the worker after every run;\n"
            "* the SDK maps ``./local/tmp/...`` paths to object-store keys, and a\n"
            "  ``/tmp/...`` path falls outside that mapping.\n"
            "\n"
            "Fix — use the App's run scratch directory::\n"
            "\n"
            "    # before\n"
            "    output = Path(tempfile.gettempdir()) / 'my-app' / self.run_id\n"
            "    # after\n"
            "    output = Path(self.run_scratch_dir)\n"
            "\n"
            "``self.run_scratch_dir`` is the same string in ``run()`` and in every\n"
            "``@task`` of the run, is safe in workflow code, and is removed by\n"
            "``cleanup_files`` when the run ends.  Create it in a task\n"
            "(``os.makedirs(..., exist_ok=True)``), never in ``run()``.  For\n"
            "``mkdtemp`` / ``mkstemp`` in a task, pass\n"
            "``dir=self.run_scratch_dir``.  Code outside an App method (a client,\n"
            "a helper) takes the directory as a parameter from its caller.  Code\n"
            "that runs outside any workflow run (an HTTP handler, a CLI) has no\n"
            "run scratch directory: suppress the finding with the reason, and\n"
            "remove what it creates itself.\n"
            "\n"
            "Moving a path from ``/tmp/...`` to ``./local/tmp/...`` changes where\n"
            "the files are; check what else reads the path (an output prefix\n"
            "passed on to other code, say) before switching.\n"
            "\n"
            "Silent unless the app's ``uv.lock`` resolves an\n"
            "``atlan-application-sdk`` that ships ``App.run_scratch_dir``.  A\n"
            "call that splats ``*args`` / ``**kwargs`` is not flagged, nor are\n"
            "``TemporaryDirectory`` / ``NamedTemporaryFile``, which remove\n"
            "themselves.  Test directories are not scanned.  See " + _APPS_DOC + ".\n"
            "\n"
            "Land as ``WARN``: a justified inline ``# conformance: ignore[P067]\n"
            "<reason>`` records a deliberate exception and stays visible in SARIF.\n"
        ),
        remediation_reference=RemediationReference(
            kind=RemediationKind.GUIDE,
            target="programs/areas/prescriptions.prose.md",
        ),
    ),
)
