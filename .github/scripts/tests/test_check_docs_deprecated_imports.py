"""The docs check fails a Markdown example on a deprecated SDK symbol (FND-4268)."""

from __future__ import annotations

from pathlib import Path

import check_docs_deprecated_imports as check
import pytest
from conformance.suite.checks.deprecation._manifest import (
    DeprecatedSymbol,
    Manifest,
    load_manifest,
)


def _record(symbol: str, module: str, kind: str = "class") -> DeprecatedSymbol:
    return DeprecatedSymbol(
        symbol=symbol,
        kind=kind,
        module=module,
        marker_via="warn",
        message=f"{symbol} is deprecated.",
        migration_target=True,
        removal_version="4.0.0",
    )


MANIFEST = check.module_aware(
    Manifest(
        symbols=(
            _record(
                "SqlMetadataExtractor",
                "application_sdk.templates.sql_metadata_extractor",
            ),
            _record("HandlerError", "application_sdk.handler.base"),
            _record(
                "PreflightStatus.PARTIAL",
                "application_sdk.handler.contracts",
                "enum_member",
            ),
            # Methods match by bare attribute name, so the check leaves them out.
            _record(
                "activity", "application_sdk.observability.logger_adaptor", "method"
            ),
        )
    )
)

# The shape docs/concepts/apps.md had before FND-4268: the deprecated template
# imported through the package that re-exports it.
PRE_FIX_APPS_PAGE = """\
# Apps

### SqlMetadataExtractor

```python
from application_sdk.templates import SqlMetadataExtractor
from application_sdk.app import task

class MyExtractor(SqlMetadataExtractor):
    ...
```
"""


def _lines(text: str) -> list[int]:
    return [v.line for v in check.find_violations("page.md", text, MANIFEST)]


def test_pre_fix_apps_page_is_rejected() -> None:
    violations = check.find_violations("page.md", PRE_FIX_APPS_PAGE, MANIFEST)
    # The import (line 6) and the subclass (line 9), with file line numbers.
    assert [(v.line, v.message.split(" '")[0]) for v in violations] == [
        (6, "Imports deprecated SDK symbol"),
        (9, "Subclasses deprecated SDK symbol"),
    ]


def test_marked_block_passes() -> None:
    marked = PRE_FIX_APPS_PAGE.replace(
        "```python\n", '<!-- legacy-example: v2 "before" snippet -->\n```python\n'
    )
    assert _lines(marked) == []


def test_marker_must_be_directly_above_the_fence() -> None:
    detached = PRE_FIX_APPS_PAGE.replace(
        "```python\n", "<!-- legacy-example: kept for old apps -->\n\n```python\n"
    )
    assert _lines(detached) == [8, 11]


def test_marker_without_a_reason_is_rejected() -> None:
    bare = PRE_FIX_APPS_PAGE.replace(
        "```python\n", "<!-- legacy-example -->\n```python\n"
    )
    violations = check.find_violations("page.md", bare, MANIFEST)
    assert [v.line for v in violations] == [5, 7, 10]
    assert "needs a reason" in violations[0].message


def test_import_through_the_defining_module_is_flagged() -> None:
    page = "```python\nfrom application_sdk.handler.base import HandlerError\n```\n"
    assert _lines(page) == [2]


def test_current_symbol_from_the_same_package_passes() -> None:
    page = "```python\nfrom application_sdk.templates import SqlApp\n\nclass A(SqlApp): ...\n```\n"
    assert _lines(page) == []


def test_enum_member_is_flagged_through_its_enum() -> None:
    page = """\
```python
from application_sdk.handler.contracts import PreflightOutput, PreflightStatus

out = PreflightOutput(status=PreflightStatus.PARTIAL)
ready = PreflightOutput(status=PreflightStatus.READY)
```
"""
    # The import is current; only the deprecated member's use is flagged.
    assert _lines(page) == [4]


def test_unparseable_fragment_still_checks_the_lines_that_parse() -> None:
    page = """\
```python
from application_sdk.handler import HandlerError

class MyHandler(Handler):
    async def test_auth(self, input):
        raise HandlerError(
            message="x",
        ...
```
"""
    assert _lines(page) == [2]


def test_method_records_are_not_matched() -> None:
    page = "```python\nfrom application_sdk.observability import get_logger\n\nlogger.activity()\n```\n"
    assert _lines(page) == []


@pytest.mark.parametrize("tag", ["bash", "text", "toml", "json"])
def test_non_python_block_is_ignored(tag: str) -> None:
    page = PRE_FIX_APPS_PAGE.replace("```python", f"```{tag}")
    assert _lines(page) == []


def test_indented_fence_inside_an_admonition_is_checked() -> None:
    page = """\
!!! note

    ```python
    from application_sdk.handler import HandlerError
    ```
"""
    assert _lines(page) == [4]


def test_outer_fence_hides_a_nested_example() -> None:
    page = """\
````markdown
```python
from application_sdk.handler import HandlerError
```
````
"""
    assert _lines(page) == []


@pytest.mark.parametrize(
    ("snippet", "symbol"),
    [
        (
            "from application_sdk.templates import SqlMetadataExtractor",
            "SqlMetadataExtractor",
        ),
        (
            "from application_sdk.templates import SqlQueryExtractor",
            "SqlQueryExtractor",
        ),
        (
            "from application_sdk.templates import IncrementalSqlMetadataExtractor",
            "IncrementalSqlMetadataExtractor",
        ),
        ("from application_sdk.handler import HandlerError", "HandlerError"),
        ("from application_sdk.app import NonRetryableError", "NonRetryableError"),
        (
            "from application_sdk.credentials.ref import legacy_credential_ref",
            "legacy_credential_ref",
        ),
        (
            "from application_sdk.handler.contracts import PreflightStatus\n"
            "x = PreflightStatus.PARTIAL",
            "PreflightStatus.PARTIAL",
        ),
    ],
)
def test_committed_manifest_covers_the_symbols_the_docs_taught(
    snippet: str, symbol: str
) -> None:
    """The issue's symbols, through the import paths the docs used."""
    manifest = check.module_aware(load_manifest())
    violations = check.find_violations(
        "page.md", f"```python\n{snippet}\n```\n", manifest
    )
    assert [symbol in v.message for v in violations] == [True]


def test_main_reports_and_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(check, "load_manifest", lambda: MANIFEST)
    bad = tmp_path / "bad.md"
    bad.write_text(PRE_FIX_APPS_PAGE, encoding="utf-8")
    good = tmp_path / "good.md"
    good.write_text(
        "```python\nfrom application_sdk.templates import SqlApp\n```\n",
        encoding="utf-8",
    )

    assert check.main([str(good)]) == 0
    assert check.main([str(good), str(bad)]) == 1
    out = capsys.readouterr().out
    assert f"{bad}:6: Imports deprecated SDK symbol 'SqlMetadataExtractor'" in out
    assert "legacy-example" in out


def test_main_fails_closed_on_an_empty_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(check, "load_manifest", lambda: Manifest(symbols=()))
    page = tmp_path / "page.md"
    page.write_text("text\n", encoding="utf-8")
    assert check.main([str(page)]) == 1


def test_changelog_is_excluded() -> None:
    assert "CHANGELOG.md" in check.EXCLUDED
    assert check.main(["CHANGELOG.md"]) == 0
