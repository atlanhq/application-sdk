"""Parity test framework for regression detection in connector output.

Compares transformed metadata output from two extraction runs (baseline vs candidate)
and reports ADDED, REMOVED, and MODIFIED assets per category.

Usage as CLI:
    python -m application_sdk.testing.parity \\
        --baseline ./baseline/ --candidate ./candidate/

Usage as library:
    from application_sdk.testing.parity import run_comparison, generate_markdown

    results = run_comparison(baseline_dir, candidate_dir)
    report = generate_markdown(results)
"""

from application_sdk.testing.parity.comparator import (
    compare_category,
    discover_categories,
    run_comparison,
)
from application_sdk.testing.parity.models import (
    AssetDiff,
    CategoryResult,
    FieldDiff,
)
from application_sdk.testing.parity.report import (
    generate_json_report,
    generate_markdown,
)

__all__ = [
    "run_comparison",
    "compare_category",
    "discover_categories",
    "generate_markdown",
    "generate_json_report",
    "AssetDiff",
    "CategoryResult",
    "FieldDiff",
]
