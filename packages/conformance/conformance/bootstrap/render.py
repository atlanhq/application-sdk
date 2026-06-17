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
    content = render("docstring-coverage.yaml", package_name="app")
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
    "docstring-coverage.yaml",
    "auto-fix.yml",
    "renovate-pkl-sync.yaml",
)


def _load_template(name: str) -> str:
    """Read a template file from the embedded templates directory."""
    pkg = _ir.files("conformance.bootstrap") / "templates"
    return (pkg / name).read_text(encoding="utf-8")  # type: ignore[union-attr]


def render(
    name: str,
    *,
    package_name: str = "app",
    unit_tests_workflow: str = "tests.yaml",
    app_name: str = "app",
    app_image_name: str = "",
    enable_e2e: str = "true",
    services_script: str = "",
) -> str:
    """Render template *name* with the given substitution variables.

    For static templates (no ``<< >>`` variables) this is a plain file read.
    Parameterised templates:

    - ``build-and-publish.yaml``: ``unit_tests_workflow`` (default ``"tests.yaml"``)
    - ``docstring-coverage.yaml``: ``package_name`` (default ``"app"``)
    - ``tests.yaml``: ``app_name`` (default ``"app"``), ``app_image_name``
      (default derived as ``"atlan-<app_name>-app"``), ``enable_e2e``
      (default ``"true"``), ``services_script`` (default ``""`` — renders the
      services-script line commented out; supply a path to render it active).

    All other keyword arguments are accepted but unused, so callers can pass
    the full variable set without knowing which template is parametric.
    """
    # Derive app_image_name from app_name if not supplied.
    if not app_image_name:
        app_image_name = f"atlan-{app_name}-app"

    raw = _load_template(name)
    tmpl = _ENV.from_string(raw)
    return tmpl.render(
        package_name=package_name,
        unit_tests_workflow=unit_tests_workflow,
        app_name=app_name,
        app_image_name=app_image_name,
        enable_e2e=enable_e2e,
        services_script=services_script,
    )
