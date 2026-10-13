"""Meta-tests for F021 PreflightFixedLeafInBroadExcept (CONNECT-1358)."""

from __future__ import annotations

from pathlib import Path

import pytest
from conformance.suite.checks.preflight import scan_all
from conformance.suite.rules import get_rule
from conformance.suite.schema.catalog import RemediationKind
from conformance.suite.schema.disposition import (
    EnforcementTier,
    RuleMechanism,
    RuleScope,
)
from conformance.suite.schema.findings import Finding

_IMPORTS = (
    "import httpx\n"
    "from application_sdk.errors import (\n"
    "    AppError, AppPermissionDeniedError, AuthError, InternalError,\n"
    "    PreconditionError, SourceUnavailableError, classify_http_exception,\n"
    ")\n"
    "from application_sdk.handler.base import Handler\n"
    "from application_sdk.handler.contracts import "
    "PreflightCheck, PreflightInput, PreflightOutput, PreflightStatus\n"
)
_AUTH = "AuthError(message='x', suggested_action='y')"
_INTERNAL = "InternalError(message='x', suggested_action='y')"


def _handler(body: str, extra: str = "") -> str:
    return (
        _IMPORTS
        + extra
        + "class H(Handler):\n"
        + "    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:\n"
        + body
    )


def _f021(tmp_path: Path, src: str) -> list[Finding]:
    path = tmp_path / "h.py"
    path.write_text(src)
    return [f for f in scan_all([path], tmp_path) if f.rule_id == "F021"]


def _failed_row(leaf: str, cause: str = "exc") -> str:
    return (
        "            return PreflightOutput(checks=[PreflightCheck(\n"
        '                name="access", passed=False,\n'
        f'                error={leaf}(message="Could not verify access.",\n'
        '                    suggested_action="Grant access, then re-run.",\n'
        f"                    cause={cause}).to_failure_details(),\n"
        "            )])\n"
    )


def _broad(handler: str, row: str) -> str:
    return (
        "        try:\n"
        "            await probe()\n"
        f"        {handler}\n"
        f"{row}"
        "        return PreflightOutput(checks=[])\n"
    )


def _in_broad_except(row: str) -> str:
    return _handler(_broad("except Exception as exc:", row))


def test_rule_metadata() -> None:
    rule = get_rule("F021")
    assert rule.scope is RuleScope.APP
    assert rule.tier is EnforcementTier.WARN
    assert rule.mechanism is RuleMechanism.STATIC
    assert "classify_http_exception" in rule.full_description
    assert rule.remediation_reference is not None
    assert rule.remediation_reference.kind is RemediationKind.GUIDE


def test_fires_on_fixed_permission_leaf(tmp_path: Path) -> None:
    findings = _f021(
        tmp_path, _in_broad_except(_failed_row("AppPermissionDeniedError"))
    )
    assert len(findings) == 1
    assert "classify_http_exception" in findings[0].message


def test_fires_on_fixed_auth_leaf(tmp_path: Path) -> None:
    assert len(_f021(tmp_path, _in_broad_except(_failed_row("AuthError")))) == 1


def test_fires_on_app_subclass_of_permission_leaf(tmp_path: Path) -> None:
    extra = (
        "class SourcePermissionDenied(AppPermissionDeniedError):\n"
        '    code = "SOURCE_PERMISSION"\n'
    )
    src = _handler(
        _broad("except Exception as exc:", _failed_row("SourcePermissionDenied")),
        extra,
    )
    assert len(_f021(tmp_path, src)) == 1


def test_fires_on_an_sdk_shipped_auth_leaf(tmp_path: Path) -> None:
    extra = "from application_sdk.clients.sql_errors import SqlClientAuthFailedError\n"
    row = "            raise SqlClientAuthFailedError(cause=exc) from exc\n"
    src = _handler(_broad("except Exception as exc:", row), extra)
    assert len(_f021(tmp_path, src)) == 1


def test_fires_on_an_sdk_leaf_imported_through_a_re_export(tmp_path: Path) -> None:
    extra = "from application_sdk.credentials import CredentialError\n"
    row = "            raise CredentialError(message='x', suggested_action='y')\n"
    src = _handler(_broad("except Exception as exc:", row), extra)
    assert len(_f021(tmp_path, src)) == 1


def test_fires_on_an_app_subclass_of_an_sdk_shipped_leaf(tmp_path: Path) -> None:
    extra = (
        "from application_sdk.credentials.oauth import OAuthTokenError\n"
        "class SourceTokenError(OAuthTokenError):\n"
        '    code = "SOURCE_TOKEN"\n'
    )
    row = "            raise SourceTokenError(message='x', suggested_action='y')\n"
    src = _handler(_broad("except Exception as exc:", row), extra)
    assert len(_f021(tmp_path, src)) == 1


def test_silent_on_a_non_auth_sdk_leaf_outside_errors(tmp_path: Path) -> None:
    extra = "from application_sdk.storage.errors import StorageNotFoundError\n"
    row = "            raise StorageNotFoundError(message='x', key='k')\n"
    src = _handler(_broad("except Exception as exc:", row), extra)
    assert _f021(tmp_path, src) == []


def test_sdk_customer_leaves_match_the_sdk_source() -> None:
    import ast

    from conformance.suite.checks.preflight._fixed_leaf import (
        _CUSTOMER_ROOTS,
        _SDK_CUSTOMER_LEAVES,
    )

    import application_sdk

    bases: dict[str, set[str]] = {}
    for path in Path(application_sdk.__file__).parent.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ClassDef):
                names = {ast.unparse(b).rsplit(".", 1)[-1] for b in node.bases}
                bases.setdefault(node.name, set()).update(names)
    closure = set(_CUSTOMER_ROOTS)
    grown = {name for name, parents in bases.items() if parents & closure}
    while not grown <= closure:
        closure |= grown
        grown = {name for name, parents in bases.items() if parents & closure}
    assert closure - _CUSTOMER_ROOTS == _SDK_CUSTOMER_LEAVES


@pytest.mark.parametrize(
    "clause",
    [
        "except:",
        "except BaseException:",
        "except (ValueError, Exception):",
        "except Exception:",
    ],
)
def test_fires_on_every_broad_clause(tmp_path: Path, clause: str) -> None:
    src = _handler(_broad(clause, _failed_row("AuthError", cause="None")))
    assert len(_f021(tmp_path, src)) == 1


def test_fires_on_a_qualified_builtin_exception(tmp_path: Path) -> None:
    src = _handler(
        _broad("except builtins.Exception as exc:", _failed_row("AuthError")),
        "import builtins\n",
    )
    assert len(_f021(tmp_path, src)) == 1


@pytest.mark.parametrize(
    "clause",
    ["except httpx.HTTPStatusError as exc:", "except pyodbc.Exception as exc:"],
    ids=["driver-error", "driver-class-named-exception"],
)
def test_silent_on_a_narrow_except(tmp_path: Path, clause: str) -> None:
    src = _handler(_broad(clause, _failed_row("AuthError")), "import pyodbc\n")
    assert _f021(tmp_path, src) == []


@pytest.mark.parametrize(
    "leaf", ["SourceUnavailableError", "InternalError", "PreconditionError"]
)
def test_silent_on_a_non_auth_fixed_leaf(tmp_path: Path, leaf: str) -> None:
    assert _f021(tmp_path, _in_broad_except(_failed_row(leaf))) == []


@pytest.mark.parametrize(
    "row",
    [
        f"            raise {_AUTH} from exc\n",
        f"            return PreflightOutput(checks=[PreflightCheck(name='a', passed=False, error={_AUTH})])\n",
        f"            return PreflightOutput(error={_AUTH}.to_failure_details())\n",
        f"            return PreflightCheck(name='a', passed=False, error={_AUTH}.to_failure_details())\n",
        f"            return PreflightOutput(status=PreflightStatus.NOT_READY, checks=[*checks, PreflightCheck(name='a', passed=False, error={_AUTH})])\n",
        f"            checks.append(PreflightCheck(name='a', passed=False, error={_AUTH}.to_failure_details()))\n",
        f"            checks.extend([PreflightCheck(name='a', passed=False, error={_AUTH})])\n",
        f"            return (PreflightCheck(name='a', passed=False, error={_AUTH}), None)\n",
        f"            with self.lock:\n                raise {_AUTH}\n",
        f"            try:\n                raise {_AUTH}\n            finally:\n                self.close()\n",
        "            raise AppPermissionDeniedError\n",
        "            raise AuthError from exc\n",
    ],
    ids=[
        "raise-from",
        "row-error",
        "output-error",
        "returned-row",
        "spread-checks-row",
        "appended-row",
        "extended-row",
        "tuple-return",
        "with-body",
        "try-finally",
        "bare-class-raise",
        "bare-class-raise-from",
    ],
)
def test_supported_reporting_shapes_fire(tmp_path: Path, row: str) -> None:
    assert len(_f021(tmp_path, _in_broad_except(row))) == 1


@pytest.mark.parametrize(
    "before",
    [
        "            _reraise_if_transient(exc)\n",
        '            logger.debug("probe failed: %s", safe_traceback(exc))\n',
        "            detail = str(exc)\n",
        "            code = getattr(exc, 'status', None)\n",
        "            if self.verbose:\n                detail = describe(exc)\n",
        "            leaf = classify_http_exception(exc)\n"
        "            if leaf is None:\n"
        "                leaf = InternalError\n",
        "            try:\n"
        "                await self.client.close()\n"
        "            except Exception:\n"
        "                pass\n",
    ],
    ids=[
        "transients-reraised-by-a-call",
        "logged",
        "stringified",
        "diagnostic-stored",
        "non-exiting-if",
        "classification-ignored",
        "cleanup-try",
    ],
)
def test_fires_when_earlier_statements_cannot_leave_the_handler(
    tmp_path: Path, before: str
) -> None:
    src = _in_broad_except(before + _failed_row("AppPermissionDeniedError"))
    assert len(_f021(tmp_path, src)) == 1


@pytest.mark.parametrize(
    "row",
    [
        f"            if isinstance(exc, PermissionError):\n                raise {_AUTH}\n",
        f"            if self.strict:\n                raise {_AUTH}\n",
        f"            if exc is not None:\n                raise {_AUTH}\n",
        f"            ready = exc is not None\n            if ready:\n                raise {_AUTH}\n",
        f"            if isinstance(exc, TimeoutError):\n                raise {_INTERNAL}\n            else:\n                raise {_AUTH}\n",
        f"            match exc:\n                case PermissionError():\n                    raise {_AUTH}\n",
        f"            match exc:\n                case _:\n                    raise {_AUTH}\n",
        f"            while self.retrying:\n                raise {_AUTH}\n",
        f"            for attempt in self.attempts:\n                raise {_AUTH}\n",
        f"            return PreflightOutput(checks=[PreflightCheck(name='a', passed=False, error={_AUTH}) if self.strict else ok])\n",
        f"            raise (isinstance(exc, PermissionError) and {_AUTH} or {_INTERNAL})\n",
        "            if isinstance(exc, SourceUnavailableError):\n                raise\n"
        + _failed_row("AppPermissionDeniedError"),
        "            if (leaf := classify_http_exception(exc)) is not None:\n"
        "                raise leaf(message='x', suggested_action='y') from exc\n"
        + _failed_row("AuthError"),
        "            assert isinstance(exc, PermissionError)\n"
        + _failed_row("AuthError"),
        f"            raise {_INTERNAL}\n" + _failed_row("AuthError"),
        f"            try:\n                await retry()\n            except ValueError:\n                pass\n            else:\n                raise {_AUTH}\n",
    ],
    ids=[
        "isinstance-guard",
        "guard-on-app-state",
        "always-true-guard",
        "stored-guard",
        "else-arm",
        "typed-case",
        "catch-all-case",
        "while-body",
        "for-body",
        "conditional-row",
        "boolean-operand",
        "early-exit-on-a-type",
        "early-exit-on-a-classifier",
        "assert",
        "after-an-unconditional-raise",
        "try-else",
    ],
)
def test_any_condition_on_the_way_is_not_reported(tmp_path: Path, row: str) -> None:
    assert _f021(tmp_path, _in_broad_except(row)) == []


@pytest.mark.parametrize(
    "row",
    [
        f"            {_AUTH}\n            raise {_INTERNAL}\n",
        f"            unused = {_AUTH}\n            raise {_INTERNAL}\n",
        f"            raise choose_error({_AUTH})\n",
        f"            return pick(exc.response.status_code, default=PreflightCheck(name='a', passed=False, error={_AUTH}))\n",
        f"            return wrap(PreflightCheck(name='a', passed=False, error={_AUTH}), cause=exc)\n",
        f"            PreflightCheck(name='a', passed=False, error={_AUTH})\n            raise {_INTERNAL}\n",
        f"            PreflightOutput(checks=[PreflightCheck(name='a', passed=False, error={_AUTH})])\n            raise {_INTERNAL}\n",
        f"            [PreflightCheck(name='a', passed=False, error={_AUTH})]\n            raise {_INTERNAL}\n",
        f"            return await PreflightCheck(name='a', passed=False, error={_AUTH})\n",
        f"            try:\n                raise {_AUTH}\n            except Exception:\n                raise {_INTERNAL}\n",
        "            error = _classify(exc, AppPermissionDeniedError(\n"
        '                message="Cannot list objects.",\n'
        '                suggested_action="Grant read access."))\n'
        "            return PreflightOutput(checks=[PreflightCheck(\n"
        '                name="access", passed=False,\n'
        "                error=error.to_failure_details())])\n",
        "            error = exc if isinstance(exc, AppError) else AuthError(\n"
        '                message="Could not log in.", suggested_action="Check it.",\n'
        "                cause=exc)\n"
        "            raise error\n",
    ],
    ids=[
        "unused-constructor",
        "stored-leaf",
        "leaf-passed-to-a-wrapper",
        "row-as-wrapper-default",
        "row-in-wrapper",
        "unused-row",
        "discarded-output",
        "discarded-list",
        "awaited-row",
        "try-body-with-handler",
        "classifier-default",
        "pass-through-else-leaf",
    ],
)
def test_unmodelled_reporting_is_not_reported(tmp_path: Path, row: str) -> None:
    assert _f021(tmp_path, _in_broad_except(row)) == []


def test_silent_when_the_classified_leaf_is_used(tmp_path: Path) -> None:
    row = (
        "            leaf = classify_http_exception(exc)\n"
        "            if leaf is None:\n"
        "                leaf = InternalError\n"
        "            raise leaf(message='x', suggested_action='y') from exc\n"
    )
    assert _f021(tmp_path, _in_broad_except(row)) == []


def test_silent_when_the_exception_goes_to_a_helper_that_types_it(
    tmp_path: Path,
) -> None:
    row = "            return self._preflight_error(exc)\n"
    assert _f021(tmp_path, _in_broad_except(row)) == []


def test_fires_in_a_helper_reached_from_preflight_check(tmp_path: Path) -> None:
    src = _handler(
        "        return await self._check_access()\n"
        "    async def _check_access(self):\n"
        + _broad("except Exception as exc:", _failed_row("AppPermissionDeniedError"))
    )
    assert len(_f021(tmp_path, src)) == 1


def test_a_check_only_passed_as_a_callback_is_not_analysed(tmp_path: Path) -> None:
    extra = "async def _check_access(client):\n" + _broad(
        "except Exception as exc:", _failed_row("AppPermissionDeniedError")
    )
    src = _handler(
        "        return await _run_required(_check_access, self.client)\n", extra
    )
    assert _f021(tmp_path, src) == []


def test_a_directly_called_module_check_is_analysed(tmp_path: Path) -> None:
    extra = "async def _check_access(client):\n" + _broad(
        "except Exception as exc:", _failed_row("AppPermissionDeniedError")
    )
    src = _handler("        return await _check_access(self.client)\n", extra)
    assert len(_f021(tmp_path, src)) == 1


def test_silent_outside_preflight_reach(tmp_path: Path) -> None:
    src = _handler(
        "        return PreflightOutput(checks=[])\n"
        "    async def extract(self):\n"
        + _broad("except Exception as exc:", _failed_row("AuthError"))
    )
    assert _f021(tmp_path, src) == []


def test_suppressed(tmp_path: Path) -> None:
    clause = (
        "# conformance: ignore[F021] login probe, any failure is auth\n"
        "        except Exception as exc:"
    )
    findings = _f021(tmp_path, _handler(_broad(clause, _failed_row("AuthError"))))
    assert len(findings) == 1
    assert findings[0].suppressed is True


def test_nested_try_reports_only_the_inner_handler(tmp_path: Path) -> None:
    row = (
        "            try:\n"
        "                await retry()\n"
        "            except Exception as inner:\n"
        f"                raise {_AUTH} from inner\n"
    )
    assert len(_f021(tmp_path, _in_broad_except(row))) == 1


def test_nested_try_that_classifies_inside_does_not_flag_the_outer(
    tmp_path: Path,
) -> None:
    row = (
        "            try:\n"
        "                await retry()\n"
        "            except Exception as inner:\n"
        "                if isinstance(inner, ValueError):\n"
        f"                    raise {_AUTH}\n"
    )
    assert _f021(tmp_path, _in_broad_except(row)) == []


def test_keyword_only_parameter_does_not_resolve_to_a_module_function(
    tmp_path: Path,
) -> None:
    extra = "async def check(client):\n" + _broad(
        "except Exception as exc:", _failed_row("AuthError")
    )
    src = _handler(
        "        return await self._runner(check=self._ok)\n"
        "    async def _runner(self, *, check):\n"
        "        return await check(self.client)\n",
        extra,
    )
    assert _f021(tmp_path, src) == []


@pytest.mark.parametrize("signature", ["check, /", "*, check", "*check", "**check"])
def test_shadowing_parameters_do_not_resolve_to_a_module_function(
    tmp_path: Path, signature: str
) -> None:
    extra = "async def check(client):\n" + _broad(
        "except Exception as exc:", _failed_row("AuthError")
    )
    src = _handler(
        "        return await self._runner(self._ok)\n"
        f"    async def _runner(self, {signature}):\n"
        "        return await check(self.client)\n",
        extra,
    )
    assert _f021(tmp_path, src) == []
