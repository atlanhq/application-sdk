"""Pin the lazy-pandas contract on testing.integration (BLDX-1235 / BLDX-1254).

`application_sdk.testing.integration` re-exports from `validation.py`,
which provides `validate_with_pandera`. pandas + pandera are heavy
optional deps — importing them at module load time forces every
consumer of `application_sdk.testing.integration` (apps using
``Scenario`` / ``equals`` / ``is_not_empty`` and never touching
``schema_base_path``) to install the ``pandas`` extra. atlan-openapi-app
hit this when adopting the SDR e2e harness; pytest collection failed
without the extra installed.

Both `validation.py` and every sibling module under
`application_sdk.testing.integration/` are written so the
pandas / pandera imports are deferred to first-call inside
``validate_with_pandera`` (and its helpers). The same lazy contract
holds for `application_sdk.testing.sdr` which inherits from
`BaseIntegrationTest`.

The tests below pin that invariant. A subprocess runs each import in
a clean interpreter, so test-ordering or other suites pre-importing
pandas can't mask a regression. If a future change adds a top-level
`import pandas` to any of these modules, the assertion fires.
"""

from __future__ import annotations

from tests.unit._doubles import loaded_after_running


def test_importing_integration_does_not_load_pandas() -> None:
    """`from application_sdk.testing.integration import *` must not load pandas."""
    loaded = loaded_after_running(
        "from application_sdk.testing.integration import Scenario, equals, is_not_empty"
    )
    pandas_modules = {m for m in loaded if m.startswith("pandas")}
    pandera_modules = {m for m in loaded if m.startswith("pandera")}
    assert not pandas_modules, (
        "pandas was loaded at import time. The `pandas` extra is supposed to "
        "be truly optional — apps using `Scenario` / `equals` etc. without "
        "`validate_with_pandera` should not need it. "
        f"Loaded pandas modules: {sorted(pandas_modules)}"
    )
    assert not pandera_modules, (
        "pandera was loaded at import time. Same contract as pandas — only "
        "`validate_with_pandera` should pull it in. "
        f"Loaded pandera modules: {sorted(pandera_modules)}"
    )


def test_importing_sdr_does_not_load_pandas() -> None:
    """SDR's `BaseSDRIntegrationTest` inherits from the integration runner —
    the same lazy-pandas contract must hold for the SDR adoption path
    that triggered BLDX-1235.
    """
    loaded = loaded_after_running(
        "from application_sdk.testing.sdr import BaseSDRIntegrationTest"
    )
    pandas_modules = {m for m in loaded if m.startswith("pandas")}
    pandera_modules = {m for m in loaded if m.startswith("pandera")}
    assert not pandas_modules, f"SDR import triggered pandas: {sorted(pandas_modules)}"
    assert not pandera_modules, (
        f"SDR import triggered pandera: {sorted(pandera_modules)}"
    )


def test_importing_validation_module_does_not_load_pandas() -> None:
    """The `validation` module *itself* is the natural place a careless
    `import pandas` would land — pin it explicitly. ``validate_with_pandera``
    must remain importable without the pandas extra installed; only
    *calling* it should trigger the load.
    """
    loaded = loaded_after_running(
        "from application_sdk.testing.integration.validation import "
        "validate_with_pandera, format_validation_report"
    )
    pandas_modules = {m for m in loaded if m.startswith("pandas")}
    pandera_modules = {m for m in loaded if m.startswith("pandera")}
    assert not pandas_modules, (
        "validation module imported pandas at module load time. "
        "Move the import inside the function body. "
        f"Loaded pandas modules: {sorted(pandas_modules)}"
    )
    assert not pandera_modules, (
        "validation module imported pandera at module load time. "
        f"Loaded pandera modules: {sorted(pandera_modules)}"
    )
