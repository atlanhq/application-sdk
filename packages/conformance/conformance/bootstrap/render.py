"""Bootstrap template rendering — jinja2-based renderer for standard CI workflow shims.

Templates live alongside this module in ``bootstrap/templates/``.  The jinja2
environment uses custom delimiters (``<< >>``) so GitHub Actions ``${{ ... }}``
expressions pass through untouched.

Usage::

    from conformance.bootstrap.render import MANAGED_WORKFLOWS, render

    # Render a static template (no substitution needed)
    content = render("conformance.yaml")

    # Render a parameterised template
    content = render("build-and-publish.yaml", unit_tests_workflow="tests.yaml")
"""

from __future__ import annotations

import importlib.resources as _ir

import jinja2

# Custom delimiters avoid collision with GitHub Actions ${{ ... }} expressions.
# Everything that looks like {{ ... }} in YAML is passed through as literal text.
_ENV = jinja2.Environment(
    loader=None,  # templates loaded manually via importlib.resources
    variable_start_string="<< ",
    variable_end_string=" >>",
    block_start_string="<% ",
    block_end_string=" %>",
    comment_start_string="<# ",
    comment_end_string=" #>",
    autoescape=False,
    keep_trailing_newline=True,
    undefined=jinja2.StrictUndefined,
)

# All managed CI workflow filenames written by ``bootstrap`` (relative to
# ``.github/workflows/``).  The C002 drift check iterates this registry.
MANAGED_WORKFLOWS: tuple[str, ...] = (
    "conformance.yaml",
    "conformance-upload-sarif.yaml",
    "checks.yml",
    "commits.yaml",
    "release-gate.yaml",
    "update-dashboard.yml",
    "release.yaml",
    "tag-and-publish.yaml",
    "renovate-auto-approve.yml",
    "vulnerability-scan.yml",
    "build-and-publish.yaml",
    "stale.yml",
    "auto-fix.yml",
    "generated-freshness.yaml",
)

# Workflow shims bootstrap once managed and now actively removes (relative to
# ``.github/workflows/``).  A retired name must be deleted rather than merely
# dropped from ``MANAGED_WORKFLOWS``: bootstrap wrote these files into every
# consumer repo, so dropping the template alone leaves ~54 copies behind, each
# still firing on every PR.  ``bootstrap`` deletes them on its next run and
# C002 reports any that are still present, so the retirement propagates by the
# same path that installed them.
#
# ``docstring-coverage.yaml`` (FND-381): its shim called
# ``atlanhq/application-sdk/.github/workflows/docstring-coverage.yaml@main``,
# which never existed — docstring coverage lives here as a *composite action*
# (``.github/actions/docstring-coverage/``), not a reusable workflow, so the
# shim's shape was wrong outright.  Every call therefore failed at startup:
# conclusion ``failure``, zero jobs, no check run, no logs.  Retired rather
# than repointed — the check is not wanted on connectors.
RETIRED_WORKFLOWS: tuple[str, ...] = ("docstring-coverage.yaml",)

# Non-workflow files that must also be vendored into every consumer repo,
# keyed by (repo-root-relative dest path, template filename in templates/).
#
# ``conformance-reusable.yaml`` references the first two via a local
# ``uses: ./...`` step and a ``$GITHUB_ACTION_PATH``-relative script path.
# GitHub resolves both against the *caller's* checkout, not application-sdk's
# — so every consumer app needs its own copy or the C/D-series (and any other
# series whose changed-files filter matches) legs fail with "Can't find
# action.yml" the first time they actually run. Static templates (no per-repo
# params), always-overwrite like MANAGED_WORKFLOWS.
#
# ``probe_code_scanning.py`` is here for the same reason at one remove:
# ``conformance-upload-sarif.yaml`` invokes it from the consumer's own
# checkout to decide whether the repo can accept a SARIF upload at all
# (FND-1149). It lives in a script rather than in that workflow's ``run:``
# because docs/standards/ci.md forbids conditional logic in inlined shell —
# untestable branches, and this file is force-written fleet-wide.
#
# Because these are always-overwrite, they have to satisfy the *caller's*
# linters, not just this repo's: a consumer whose pre-commit rejects one of
# them cannot fix it, since the next bootstrap run reverts the fix. FND-445
# was exactly that — the vendored Python script failed pydocstyle ``D`` and
# got re-wrapped by ``ruff format`` at a 100-column line length, leaving a
# connector with a permanently red pre-commit and ``checks.yml``.
# ``tests/test_bootstrap_scaffold_lint.py`` holds that line for every
# force-written artifact: the template-rendered ones (these plus the
# MANAGED_WORKFLOWS shims and the remediate SKILL.md) under a config
# stricter than this repo's, and ``.github/ci-system-deps.txt``, whose
# bytes come from the ``--system-deps`` flag rather than a template,
# separately. Keep new templates inside it.
MANAGED_ACTION_FILES: tuple[tuple[str, str], ...] = (
    (
        ".github/actions/run-conformance-detect/action.yaml",
        "run-conformance-detect-action.yaml",
    ),
    (".github/scripts/build_conformance_args.py", "build_conformance_args.py"),
    (".github/scripts/probe_code_scanning.py", "probe_code_scanning.py"),
)


def _load_template(name: str) -> str:
    """Read a template file from the embedded templates directory."""
    pkg = _ir.files("conformance.bootstrap") / "templates"
    return (pkg / name).read_text(encoding="utf-8")  # type: ignore[union-attr]


# Templates that are byte-for-byte vendored copies of files living elsewhere
# (action.yaml / shell scripts) and never take substitution variables. These
# skip the jinja env entirely rather than relying on "just don't define any
# << >> vars" — vendored scripts legitimately contain bash constructs like
# the here-string operator (`<<<`) that collide with our custom `<< `
# variable-start delimiter (Jinja matches it starting at the *second* `<`,
# then fails deep in the following line looking for the closing ` >>`).
_STATIC_TEMPLATES: frozenset[str] = frozenset(
    template_name for _, template_name in MANAGED_ACTION_FILES
)


def render(
    name: str,
    *,
    unit_tests_workflow: str = "tests.yaml",
    app_name: str = "app",
    app_image_name: str = "",
    enable_e2e: str = "true",
    services_script: str = "",
    system_deps: str = "",
    exit_zero: str = "false",
    automerge: str = "true",
    unit_coverage_fail_under: str = "",
    use_ghcr_base: str = "",
    force_external_runtime: str = "",
    secrets_block: str = "",
) -> str:
    """Render template *name* with the given substitution variables.

    For static templates (no ``<< >>`` variables) this is a plain file read.
    Parameterised templates:

    - ``build-and-publish.yaml``: ``unit_tests_workflow`` (default ``"tests.yaml"``)
      and ``use_ghcr_base`` (default ``""`` — no line, so the SDK's own default
      applies; ``"true"`` renders the opt-in as a bare
      ``use_ghcr_base: true``). Same same-line ``<% if %>`` hugging as
      ``checks.yml`` below, for the same reason: an un-taken block on its own
      lines would leave a blank line and read as C002 drift in every repo that
      hasn't opted in.
    - ``conformance.yaml``: ``exit_zero`` (default ``"false"``; set to ``"true"``
      for soft-enforcement rollouts where violations are tracked but do not block
      merges — flip to ``"false"`` when the app is ready for hard gating).
    - ``checks.yml``: ``system_deps`` (default ``""`` — no
      system-dependency step; supply a space-separated apt package list to
      render an ``apt-get install`` step before ``setup-deps``, for a repo whose
      dependencies build from sdist and need C headers on the runner). Its
      ``<% if %>``/``<% endif %>`` tags deliberately hug the content on the same
      line: on their own lines an un-taken block would still leave its trailing
      newline, and that one blank line would read as C002 drift in every
      already-bootstrapped repo. ``test_bootstrap`` locks the empty render
      byte-for-byte.
    - ``renovate.json``: ``automerge`` (default ``"true"``; set to ``"false"``
      to disable Renovate auto-merge during initial rollouts — the preset's
      ``automerge: true`` packageRules are overridden by a catch-all rule so
      PRs are raised and CI-gated but humans must click merge).
    - ``tests.yaml``: ``app_name`` (default ``"app"``), ``app_image_name``
      (default derived as ``"atlan-<app_name>-app"``), ``enable_e2e``
      (default ``"true"``), ``services_script`` (default ``""`` — renders the
      services-script line commented out; supply a path to render it active),
      ``unit_coverage_fail_under`` (default ``""`` — no line, so the SDK's own
      floor applies; supply a percent to render this app's higher floor, hugged
      onto the same line as its ``<% if %>`` tags like ``checks.yml``'s step so
      the no-override render is unchanged). The coverage line renders bare and
      as the first entry of the ``with:`` block — deliberately the exact shape
      the apps that already raised their floor hand-wrote, so those files match
      this canonical instead of reporting C002 drift for having opted up. A
      surrounding explanatory comment, or any other position, would guarantee
      the mismatch.  ``force_external_runtime`` (default ``""`` — no line;
      ``"true"`` renders a bare ``force-external-runtime: true`` immediately
      after the coverage line) and ``secrets_block`` (default ``""`` — renders
      the canonical ``secrets: inherit``; supply a verbatim block, as
      ``bootstrap.extract.extract_secrets_block`` reads one off disk, to render
      an app's explicit ``secrets:`` mapping in its place) are the two values
      FND-604 added, hugged onto their tags' lines for the same byte-identity
      reason.  ``secrets_block`` carries no trailing newline of its own — the
      template's line supplies it, so the no-override render is unchanged.
    - ``.gitignore``: static template, no substitution.

    All other keyword arguments are accepted but unused, so callers can pass
    the full variable set without knowing which template is parametric.
    """
    raw = _load_template(name)
    if name in _STATIC_TEMPLATES:
        return raw

    # Derive app_image_name from app_name if not supplied.
    if not app_image_name:
        app_image_name = f"atlan-{app_name}-app"

    tmpl = _ENV.from_string(raw)
    return tmpl.render(
        unit_tests_workflow=unit_tests_workflow,
        app_name=app_name,
        app_image_name=app_image_name,
        enable_e2e=enable_e2e,
        services_script=services_script,
        system_deps=system_deps,
        exit_zero=exit_zero,
        automerge=automerge,
        unit_coverage_fail_under=unit_coverage_fail_under,
        use_ghcr_base=use_ghcr_base,
        force_external_runtime=force_external_runtime,
        secrets_block=secrets_block,
    )
