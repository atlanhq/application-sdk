from pathlib import Path

import pytest
from conformance.suite.checks.preflight._common import build_registry
from conformance.suite.checks.preflight._contracts import scan

IMPORTS = """from application_sdk.handler.base import Handler
from application_sdk.handler.contracts import PreflightInput, PreflightOutput, PreflightCheck
from application_sdk.errors import AuthError, FailureDetails
"""


def check(
    tmp_path: Path,
    body: str,
    extra: str = "",
    signature: str = "self, input: PreflightInput) -> PreflightOutput",
) -> list[str]:
    path = tmp_path / "handler.py"
    path.write_text(
        IMPORTS
        + extra
        + "\nclass H(Handler):\n    async def preflight_check("
        + signature
        + ":\n"
        + "\n".join("        " + line for line in body.splitlines())
        + "\n"
    )
    return [f.rule_id for f in scan(build_registry([path], tmp_path))]


@pytest.mark.parametrize("signature", ["self, input)", "self, input: dict) -> dict"])
def test_handler_contract_rejects_untyped(tmp_path: Path, signature: str) -> None:
    assert "F006" in check(tmp_path, "return None", signature=signature)


def test_handler_contract_alias(tmp_path: Path) -> None:
    assert (
        check(
            tmp_path,
            "return PreflightOutput(checks=[])",
            "from application_sdk.handler.contracts import PreflightInput as Request, PreflightOutput as Result\n",
            "self, input: Request) -> Result",
        )
        == []
    )


def test_handler_legacy_result(tmp_path: Path) -> None:
    assert "F006" in check(tmp_path, 'return {"success": True}')


@pytest.mark.parametrize("action", ["None", '"  "'])
def test_blank_failure_action(tmp_path: Path, action: str) -> None:
    assert "F007" in check(
        tmp_path,
        f'return PreflightOutput(checks=[PreflightCheck(passed=False, error=FailureDetails(message="Probe failed", suggested_action={action}))])',
    )


def test_missing_failure_action(tmp_path: Path) -> None:
    assert "F007" in check(
        tmp_path,
        'return PreflightOutput(checks=[PreflightCheck(passed=False, error=FailureDetails(message="Probe failed"))])',
    )


def test_inherited_action(tmp_path: Path) -> None:
    extra = 'class ProbeError(AuthError):\n    message: str = "Probe failed"\n    suggested_action: str = "Check the configured permission."\n'
    assert (
        check(
            tmp_path,
            "return PreflightOutput(checks=[PreflightCheck(passed=False, error=ProbeError().to_failure_details())])",
            extra,
        )
        == []
    )


def test_unknown_factory_action(tmp_path: Path) -> None:
    assert check(
        tmp_path,
        "return PreflightOutput(checks=[PreflightCheck(passed=False, error=external_factory())])",
    ) == ["F019"]


def test_expected_error_escapes(tmp_path: Path) -> None:
    assert "F008" in check(tmp_path, 'raise AuthError(message="Probe failed")')


def test_helper_error_escapes(tmp_path: Path) -> None:
    assert "F008" in check(
        tmp_path,
        "return probe()",
        'def probe():\n    raise AuthError(message="Probe failed")\n',
    )


def test_helper_error_converted(tmp_path: Path) -> None:
    assert "F008" not in check(
        tmp_path,
        "try:\n    return probe()\nexcept AuthError as exc:\n    return PreflightOutput(checks=[PreflightCheck(passed=False, error=exc.to_failure_details())])",
        'def probe():\n    raise AuthError(message="Probe failed")\n',
    )


def test_unrelated_raise_not_probe_failure(tmp_path: Path) -> None:
    assert "F008" not in check(tmp_path, 'raise RuntimeError("Unexpected invariant")')


def test_ready_failed_check(tmp_path: Path) -> None:
    assert "F009" in check(
        tmp_path,
        'return PreflightOutput(status="READY", checks=[PreflightCheck(passed=False, error=external_factory())])',
    )


def test_not_ready_successful_checks(tmp_path: Path) -> None:
    assert "F009" in check(
        tmp_path,
        'return PreflightOutput(status="NOT_READY", checks=[PreflightCheck(passed=True)])',
    )


def test_partial_failed_check_is_valid(tmp_path: Path) -> None:
    assert check(
        tmp_path,
        'return PreflightOutput(status="PARTIAL", checks=[PreflightCheck(passed=False, error=external_factory())])',
    ) == ["F019"]


def test_interactive_input_without_entrypoint_valid(tmp_path: Path) -> None:
    assert (
        check(
            tmp_path,
            "request = PreflightInput(credentials=[])\nreturn PreflightOutput(checks=[])",
        )
        == []
    )


def test_workflow_input_wrong_entrypoint(tmp_path: Path) -> None:
    path = tmp_path / "app.py"
    path.write_text(
        IMPORTS
        + 'from application_sdk.app import entrypoint\n@entrypoint(name="crawl")\nasync def crawl(input):\n    return PreflightInput(entrypoint="mine")\n'
    )
    assert [f.rule_id for f in scan(build_registry([path], tmp_path))] == ["F010"]


def test_workflow_input_correct_entrypoint(tmp_path: Path) -> None:
    path = tmp_path / "app.py"
    path.write_text(
        IMPORTS
        + 'from application_sdk.app import entrypoint\n@entrypoint(name="crawl")\nasync def crawl(input):\n    return PreflightInput(entrypoint="crawl")\n'
    )
    assert scan(build_registry([path], tmp_path)) == []


def test_imported_failure_inherits_missing_action(tmp_path: Path):
    errors = tmp_path / "failures.py"
    errors.write_text(
        "from application_sdk.errors import AuthError\nclass ProbeDenied(AuthError):\n    pass\n"
    )
    handler = tmp_path / "handler.py"
    handler.write_text(
        IMPORTS
        + 'from failures import ProbeDenied\nclass H(Handler):\n    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:\n        return PreflightOutput(status="not_ready", checks=[PreflightCheck(name="auth", passed=False, error=ProbeDenied(message="Permission denied").to_failure_details())])\n'
    )
    assert "F007" in {
        f.rule_id for f in scan(build_registry([errors, handler], tmp_path))
    }


def test_imported_classifier_raise_is_detected(tmp_path: Path):
    helper = tmp_path / "failures.py"
    helper.write_text(
        'from application_sdk.errors import RateLimitedError\ndef classify():\n    raise RateLimitedError(message="Try later")\n'
    )
    handler = tmp_path / "handler.py"
    handler.write_text(
        IMPORTS
        + "from failures import classify\nclass H(Handler):\n    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:\n        return classify()\n"
    )
    assert "F008" in {
        f.rule_id for f in scan(build_registry([helper, handler], tmp_path))
    }


def test_unrelated_exception_catch_does_not_hide_raise(tmp_path: Path):
    assert "F008" in check(
        tmp_path,
        'try:\n    raise AuthError(message="Denied")\nexcept ValueError:\n    return PreflightOutput(status="ready")',
    )


_PREP_ERROR = "from application_sdk.errors import InternalError\nclass PrepError(InternalError):\n    pass\n"


@pytest.mark.parametrize(
    "extra, raised, caught",
    [
        (
            _PREP_ERROR + "from application_sdk.errors import AppError\n",
            "PrepError",
            "AppError",
        ),
        (
            _PREP_ERROR + "from application_sdk.errors.base import AppError\n",
            "PrepError",
            "AppError",
        ),
        (
            "from application_sdk.errors import AppTimeoutError, TaskStalledError\n",
            "TaskStalledError",
            "AppTimeoutError",
        ),
        (
            "from application_sdk.errors import InvalidInputValueError\n",
            "InvalidInputValueError",
            "ValueError",
        ),
    ],
    ids=["app-leaf-AppError", "submodule-AppError", "sdk-category", "builtin-base"],
)
def test_sdk_ancestor_catch_converts_raise(tmp_path, extra, raised, caught):
    assert "F008" not in check(
        tmp_path,
        f'try:\n    raise {raised}(message="Failure")\nexcept {caught} as exc:\n    return PreflightOutput(checks=[PreflightCheck(passed=False, error=exc.to_failure_details())])',
        extra,
    )


def test_sdk_ancestor_catch_converts_helper_raise(tmp_path):
    assert "F008" not in check(
        tmp_path,
        "try:\n    return probe()\nexcept AppError as exc:\n    return PreflightOutput(checks=[PreflightCheck(passed=False, error=exc.to_failure_details())])",
        _PREP_ERROR
        + "from application_sdk.errors import AppError\n"
        + 'def probe():\n    raise PrepError(message="Failure")\n',
    )


@pytest.mark.parametrize("caught", ["AuthError", "InvalidInputError"])
def test_sdk_sibling_catch_does_not_hide_raise(tmp_path, caught):
    assert "F008" in check(
        tmp_path,
        f'try:\n    raise PrepError(message="Failure")\nexcept {caught}:\n    return PreflightOutput(status="ready")',
        _PREP_ERROR + "from application_sdk.errors import InvalidInputError\n",
    )


def test_sdk_error_ancestry_matches_runtime_mro():
    from conformance.suite.checks.preflight._contracts import sdk_error_ancestry

    import application_sdk.errors as sdk_errors

    for name in sdk_errors.__all__:
        cls = getattr(sdk_errors, name)
        if not (isinstance(cls, type) and issubclass(cls, BaseException)):
            continue
        expected = {
            f"application_sdk.errors.{base.__name__}"
            if base.__module__.startswith("application_sdk.errors")
            else base.__name__
            for base in cls.__mro__
            if base is not object
        }
        assert sdk_error_ancestry(f"application_sdk.errors.{name}") == expected, name


@pytest.mark.parametrize(
    "catch, raised", [("Exception", ""), ("AuthError as exc", "exc")]
)
def test_typed_failure_reraise(tmp_path, catch, raised):
    assert "F008" in check(
        tmp_path,
        f'try:\n    raise AuthError(message="Failure")\nexcept {catch}:\n    raise {raised}',
    )


@pytest.mark.parametrize("name", ["ObjectStoreReadError", "DiskFullError"])
def test_sdk_default_action_not_missing(tmp_path, name):
    assert "F007" not in check(
        tmp_path, f"raise {name}()", f"from application_sdk.errors import {name}\n"
    )


@pytest.mark.parametrize(
    "status",
    [
        '"partial"',
        '"PARTIAL"',
        "PreflightStatus.PARTIAL",
        '"partial" if degraded else "ready"',
        '"ready"',
    ],
)
def test_partial_verdict_has_no_preflight_rule(tmp_path, status):
    """No F-series rule reports a PARTIAL verdict — that belongs to B001.

    ``PreflightStatus.PARTIAL`` is deprecated in the SDK, and B001 reports an
    app reading a deprecated enum member fleet-wide from the deprecated-symbol
    manifest, carrying the SDK's own migration guidance.  A preflight-specific
    rule would put a second WARN on the same line, so the preflight series
    deliberately has none.  This pins that: the verdict alone produces no F
    finding.
    """
    assert check(tmp_path, f"return PreflightOutput(status={status}, checks=[])") == []


def findings(tmp_path: Path, body: str, extra: str = ""):
    """``check`` with the whole finding, for assertions on the message."""
    path = tmp_path / "handler.py"
    path.write_text(
        IMPORTS
        + extra
        + "\nclass H(Handler):\n    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:\n"
        + "\n".join("        " + line for line in body.splitlines())
        + "\n"
    )
    return scan(build_registry([path], tmp_path))


AGGREGATION = "checks = []\nchecks.append(PreflightCheck(name='auth', passed=True))\n"


def test_computed_aggregation_is_unresolved(tmp_path: Path) -> None:
    """The baseline the next test is measured against."""
    assert check(tmp_path, AGGREGATION + "return PreflightOutput(checks=checks)") == [
        "F019"
    ]


@pytest.mark.parametrize(
    "aggregation",
    [
        pytest.param("[*checks]", id="starred-copy"),
        pytest.param("[*checks, PreflightCheck(name='spec', passed=True)]", id="mixed"),
        pytest.param("(*checks,)", id="starred-tuple"),
        pytest.param("[*list(checks)]", id="starred-call"),
    ],
)
def test_list_display_does_not_clear_an_opaque_aggregation(
    tmp_path: Path, aggregation: str
) -> None:
    """A cosmetic rewrap is not a resolution.

    ``[*checks]`` is a semantically identical copy of ``checks``: the list has
    ``elts`` so the node-type gate is satisfied, but nothing downstream can read
    a role or a verdict out of the one ``Starred`` node.  If that cleared F019,
    two characters would buy a green rule while an honest restructure bought
    nothing, and the rule's fleet-wide signal would be worthless.
    """
    assert check(
        tmp_path, AGGREGATION + f"return PreflightOutput(checks={aggregation})"
    ) == ["F019"]


def test_opaque_row_names_the_element(tmp_path: Path) -> None:
    """The finding points at the opaque row, not at the whole call."""
    reported = [
        f
        for f in findings(
            tmp_path, AGGREGATION + "return PreflightOutput(checks=[*checks])"
        )
        if f.rule_id == "F019"
    ]
    assert len(reported) == 1
    assert "`*checks`" in reported[0].message


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            "row = PreflightCheck(name='auth', passed=True)\n"
            "return PreflightOutput(checks=[row])",
            id="single-binding",
        ),
        pytest.param(
            "return PreflightOutput(checks=[self._probe()])\n",
            id="method-returning-a-row",
        ),
        pytest.param(
            "return PreflightOutput(checks=[PreflightCheck(name='a', passed=True)"
            " if input else PreflightCheck(name='b', passed=True)])",
            id="conditional-row",
        ),
        pytest.param(
            "return PreflightOutput(checks=[probe()])",
            id="helper-returning-a-row",
        ),
    ],
)
def test_resolvable_rows_are_not_reported(tmp_path: Path, body: str) -> None:
    """The gate is resolvability, so every row the analysis can read stays clean."""
    extra = "def probe():\n    return PreflightCheck(name='auth', passed=True)\n"
    tail = (
        "\n    def _probe(self):\n"
        "        return PreflightCheck(name='auth', passed=True)\n"
    )
    path = tmp_path / "handler.py"
    path.write_text(
        IMPORTS
        + extra
        + "\nclass H(Handler):\n    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:\n"
        + "\n".join("        " + line for line in body.splitlines())
        + tail
    )
    assert [f.rule_id for f in scan(build_registry([path], tmp_path))] == []


def test_rebound_row_is_unresolved(tmp_path: Path) -> None:
    """Two assignments leave the value the list carries at runtime unknown."""
    assert check(
        tmp_path,
        "row = PreflightCheck(name='auth', passed=True)\n"
        "row = reconcile(row)\n"
        "return PreflightOutput(checks=[row])",
    ) == ["F019"]
