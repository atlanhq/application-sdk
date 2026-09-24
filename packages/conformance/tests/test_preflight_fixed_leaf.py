"""Meta-tests for F021 PreflightFixedLeafInBroadExcept (CONNECT-1358)."""

from __future__ import annotations

from pathlib import Path

from conformance.suite.checks.preflight import scan_all
from conformance.suite.rules import get_rule
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
    "PreflightCheck, PreflightInput, PreflightOutput\n"
)


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


def test_rule_metadata() -> None:
    rule = get_rule("F021")
    assert rule.scope is RuleScope.APP
    assert rule.tier is EnforcementTier.WARN
    assert rule.mechanism is RuleMechanism.STATIC
    assert "classify_http_exception" in rule.full_description


def test_fires_on_fixed_permission_leaf(tmp_path: Path) -> None:
    src = _handler(
        _broad("except Exception as exc:", _failed_row("AppPermissionDeniedError"))
    )
    findings = _f021(tmp_path, src)
    assert len(findings) == 1
    assert "classify_http_exception" in findings[0].message


def test_fires_on_fixed_auth_leaf(tmp_path: Path) -> None:
    src = _handler(_broad("except Exception as exc:", _failed_row("AuthError")))
    assert len(_f021(tmp_path, src)) == 1


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


def test_fires_on_bare_except_and_base_exception(tmp_path: Path) -> None:
    for clause in (
        "except:",
        "except BaseException:",
        "except (ValueError, Exception):",
    ):
        src = _handler(_broad(clause, _failed_row("AuthError", cause="None")))
        assert len(_f021(tmp_path, src)) == 1, clause


def test_fires_when_only_transients_are_reraised_first(tmp_path: Path) -> None:
    row = "            _reraise_if_transient(exc)\n" + _failed_row(
        "AppPermissionDeniedError"
    )
    src = _handler(_broad("except Exception as exc:", row))
    assert len(_f021(tmp_path, src)) == 1


def test_fires_when_the_exception_is_only_logged(tmp_path: Path) -> None:
    row = (
        '            logger.debug("probe failed: %s", safe_traceback(exc))\n'
        + _failed_row("AppPermissionDeniedError")
    )
    src = _handler(_broad("except Exception as exc:", row))
    assert len(_f021(tmp_path, src)) == 1


def test_fires_when_an_isinstance_guard_only_reraises(tmp_path: Path) -> None:
    row = (
        "            if isinstance(exc, SourceUnavailableError):\n"
        "                raise\n" + _failed_row("AppPermissionDeniedError")
    )
    src = _handler(_broad("except Exception as exc:", row))
    assert len(_f021(tmp_path, src)) == 1


def test_fires_on_raise_from(tmp_path: Path) -> None:
    row = (
        "            raise AuthError(message='Could not log in.',\n"
        "                suggested_action='Check the credentials.') from exc\n"
    )
    src = _handler(_broad("except Exception as exc:", row))
    assert len(_f021(tmp_path, src)) == 1


def test_fires_in_a_helper_reached_from_preflight_check(tmp_path: Path) -> None:
    src = _handler(
        "        return await self._check_access()\n"
        "    async def _check_access(self):\n"
        + _broad("except Exception as exc:", _failed_row("AppPermissionDeniedError"))
    )
    assert len(_f021(tmp_path, src)) == 1


def test_fires_in_a_check_passed_as_a_callback(tmp_path: Path) -> None:
    extra = "async def _check_access(client):\n" + _broad(
        "except Exception as exc:", _failed_row("AppPermissionDeniedError")
    )
    src = _handler(
        "        return await _run_required(_check_access, self.client)\n", extra
    )
    assert len(_f021(tmp_path, src)) == 1


def test_silent_on_a_narrow_except(tmp_path: Path) -> None:
    src = _handler(
        _broad("except httpx.HTTPStatusError as exc:", _failed_row("AuthError"))
    )
    assert _f021(tmp_path, src) == []


def test_silent_on_a_non_auth_fixed_leaf(tmp_path: Path) -> None:
    for leaf in ("SourceUnavailableError", "InternalError", "PreconditionError"):
        src = _handler(_broad("except Exception as exc:", _failed_row(leaf)))
        assert _f021(tmp_path, src) == [], leaf


def test_fires_when_the_classification_is_ignored(tmp_path: Path) -> None:
    row = (
        "            leaf = classify_http_exception(exc)\n"
        "            if leaf is None:\n"
        "                leaf = InternalError\n" + _failed_row("AuthError")
    )
    src = _handler(_broad("except Exception as exc:", row))
    assert len(_f021(tmp_path, src)) == 1


def test_silent_when_the_classified_leaf_is_used(tmp_path: Path) -> None:
    row = (
        "            leaf = classify_http_exception(exc)\n"
        "            if leaf is None:\n"
        "                leaf = InternalError\n"
        "            raise leaf(message='x', suggested_action='y') from exc\n"
    )
    src = _handler(_broad("except Exception as exc:", row))
    assert _f021(tmp_path, src) == []


def test_fires_on_a_customer_blaming_classifier_default(tmp_path: Path) -> None:
    row = (
        "            error = _classify(exc, AppPermissionDeniedError(\n"
        '                message="Cannot list objects.",\n'
        '                suggested_action="Grant read access."))\n'
        "            return PreflightOutput(checks=[PreflightCheck(\n"
        '                name="access", passed=False,\n'
        "                error=error.to_failure_details())])\n"
    )
    src = _handler(_broad("except Exception as exc:", row))
    assert len(_f021(tmp_path, src)) == 1


def test_silent_on_a_classifier_with_a_safe_default(tmp_path: Path) -> None:
    row = (
        "            error = _classify(exc, InternalError(\n"
        '                message="Could not list objects.",\n'
        '                suggested_action="Retry, then contact support."))\n'
        "            raise error\n"
    )
    src = _handler(_broad("except Exception as exc:", row))
    assert _f021(tmp_path, src) == []


def test_fires_when_a_guard_copies_a_value_from_before_the_exception(
    tmp_path: Path,
) -> None:
    row = (
        "            flag = True\n"
        "            ready = flag\n"
        "            flag = isinstance(exc, PermissionError)\n"
        "            if ready:\n"
        "                raise AuthError(message='x', suggested_action='y')\n"
    )
    src = _handler(_broad("except Exception as exc:", row))
    assert len(_f021(tmp_path, src)) == 1


def test_fires_when_the_exception_dependent_store_is_on_one_branch_only(
    tmp_path: Path,
) -> None:
    row = (
        "            ready = True\n"
        "            if self.strict:\n"
        "                ready = isinstance(exc, PermissionError)\n"
        "            if ready:\n"
        "                raise AuthError(message='x', suggested_action='y')\n"
    )
    src = _handler(_broad("except Exception as exc:", row))
    assert len(_f021(tmp_path, src)) == 1


def test_silent_when_the_guard_name_is_reassigned_inside_its_branch(
    tmp_path: Path,
) -> None:
    row = (
        "            ready = isinstance(exc, PermissionError)\n"
        "            if ready:\n"
        "                ready = False\n"
        "                raise AuthError(message='x', suggested_action='y')\n"
    )
    src = _handler(_broad("except Exception as exc:", row))
    assert _f021(tmp_path, src) == []


def test_silent_on_a_walrus_classification_read_by_a_later_guard(
    tmp_path: Path,
) -> None:
    row = (
        "            if (kind := classify(exc)) is None:\n"
        "                raise InternalError(message='x', suggested_action='y')\n"
        "            if kind == 'auth':\n"
        "                raise AuthError(message='x', suggested_action='y')\n"
    )
    src = _handler(_broad("except Exception as exc:", row))
    assert _f021(tmp_path, src) == []


def test_fires_under_a_stored_guard_that_is_always_true(tmp_path: Path) -> None:
    for stored in ("exc is not None", "isinstance(exc, Exception)", "not exc is None"):
        row = (
            f"            ready = {stored}\n"
            "            if ready:\n"
            "                raise AuthError(message='x', suggested_action='y')\n"
        )
        src = _handler(_broad("except Exception as exc:", row))
        assert len(_f021(tmp_path, src)) == 1, stored


def test_fires_on_the_fallback_of_an_isinstance_pass_through(tmp_path: Path) -> None:
    row = (
        "            error = exc if isinstance(exc, AppError) else AuthError(\n"
        '                message="Could not log in.", suggested_action="Check it.",\n'
        "                cause=exc)\n"
        "            return PreflightOutput(checks=[PreflightCheck(\n"
        '                name="access", passed=False,\n'
        "                error=error.to_failure_details())])\n"
    )
    src = _handler(_broad("except Exception as exc:", row))
    assert len(_f021(tmp_path, src)) == 1


def test_fires_on_the_else_of_a_condition_on_the_exception(tmp_path: Path) -> None:
    row = (
        "            if isinstance(exc, TimeoutError):\n"
        "                raise SourceUnavailableError(message='x', suggested_action='y')\n"
        "            else:\n"
        "                raise AuthError(message='x', suggested_action='y')\n"
    )
    src = _handler(_broad("except Exception as exc:", row))
    assert len(_f021(tmp_path, src)) == 1


def test_silent_on_an_elif_on_the_exception(tmp_path: Path) -> None:
    row = (
        "            if isinstance(exc, TimeoutError):\n"
        "                raise SourceUnavailableError(message='x', suggested_action='y')\n"
        "            elif is_privilege_error(exc):\n"
        "                raise AuthError(message='x', suggested_action='y')\n"
        "            raise InternalError(message='x', suggested_action='y')\n"
    )
    src = _handler(_broad("except Exception as exc:", row))
    assert _f021(tmp_path, src) == []


def test_silent_under_a_predicate_on_the_caught_exception(tmp_path: Path) -> None:
    row = (
        "            if is_privilege_error(exc):\n"
        + "    "
        + _failed_row("AppPermissionDeniedError").replace(
            "\n            ", "\n                "
        )
    )
    src = _handler(_broad("except Exception as exc:", row))
    assert _f021(tmp_path, src) == []


def test_silent_when_the_exception_goes_to_a_helper_that_types_it(
    tmp_path: Path,
) -> None:
    row = "            return self._preflight_error(exc)\n"
    src = _handler(_broad("except Exception as exc:", row))
    assert _f021(tmp_path, src) == []


def test_fires_when_str_of_the_exception_is_the_only_use(tmp_path: Path) -> None:
    row = "            detail = str(exc)\n" + _failed_row("AppPermissionDeniedError")
    src = _handler(_broad("except Exception as exc:", row))
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


def test_fires_when_the_classifier_only_wraps_the_message(tmp_path: Path) -> None:
    row = (
        "            raise AuthError(message=redact(exc),\n"
        "                suggested_action='Check the credentials.')\n"
    )
    src = _handler(_broad("except Exception as exc:", row))
    assert len(_f021(tmp_path, src)) == 1


def test_fires_when_the_classifier_is_in_a_sibling_branch(tmp_path: Path) -> None:
    row = (
        "            if self.verbose:\n"
        "                detail = describe(exc)\n"
        + _failed_row("AppPermissionDeniedError")
    )
    src = _handler(_broad("except Exception as exc:", row))
    assert len(_f021(tmp_path, src)) == 1


def test_fires_on_the_fallthrough_after_a_walrus_classifier(tmp_path: Path) -> None:
    row = (
        "            if (leaf := classify_http_exception(exc)) is not None:\n"
        "                raise leaf(message='x', suggested_action='y') from exc\n"
        + _failed_row("AuthError")
    )
    src = _handler(_broad("except Exception as exc:", row))
    assert len(_f021(tmp_path, src)) == 1


def test_silent_under_a_condition_on_a_name_derived_from_the_exception(
    tmp_path: Path,
) -> None:
    row = (
        "            code = exc.response.status_code\n"
        "            if code == 403:\n"
        "                raise AuthError(message='x', suggested_action='y')\n"
    )
    src = _handler(_broad("except Exception as exc:", row))
    assert _f021(tmp_path, src) == []


def test_fires_when_the_first_failure_is_kept_in_a_variable(tmp_path: Path) -> None:
    row = (
        "            if error is None:\n"
        "                error = AuthError(message='x', suggested_action='y', cause=exc)\n"
    )
    src = _handler("        error = None\n" + _broad("except Exception as exc:", row))
    assert len(_f021(tmp_path, src)) == 1


def test_fires_when_the_row_carries_a_message_derived_from_the_exception(
    tmp_path: Path,
) -> None:
    row = (
        "            error_msg = str(exc)\n"
        "            return PreflightOutput(checks=[PreflightCheck(\n"
        '                name="access", passed=False, message=error_msg,\n'
        "                error=AuthError(message='x', suggested_action='y',\n"
        "                    cause=exc).to_failure_details())])\n"
    )
    src = _handler(_broad("except Exception as exc:", row))
    assert len(_f021(tmp_path, src)) == 1


def test_fires_on_a_catch_all_case_of_a_match_on_the_exception(tmp_path: Path) -> None:
    for pattern in ("_", "other"):
        row = (
            "            match exc:\n"
            "                case TimeoutError():\n"
            "                    raise SourceUnavailableError(message='x', suggested_action='y')\n"
            f"                case {pattern}:\n"
            "                    raise AuthError(message='x', suggested_action='y')\n"
        )
        src = _handler(_broad("except Exception as exc:", row))
        assert len(_f021(tmp_path, src)) == 1, pattern


def test_fires_when_only_a_diagnostic_of_the_exception_is_stored(
    tmp_path: Path,
) -> None:
    for store in ("tb = safe_traceback(exc)", "code = getattr(exc, 'status', None)"):
        row = f"            {store}\n" + _failed_row("AuthError")
        src = _handler(_broad("except Exception as exc:", row))
        assert len(_f021(tmp_path, src)) == 1, store


def test_silent_when_a_name_derived_from_the_classification_selects_the_leaf(
    tmp_path: Path,
) -> None:
    row = (
        "            leaf = classify(exc)\n"
        "            advisory = isinstance(leaf, AppPermissionDeniedError)\n"
        "            error = AuthError(message='x', suggested_action='y') if advisory else leaf\n"
        "            raise error\n"
    )
    src = _handler(_broad("except Exception as exc:", row))
    assert _f021(tmp_path, src) == []


def test_fires_when_a_stored_diagnostic_does_not_select_the_leaf(
    tmp_path: Path,
) -> None:
    row = "            detail = redact(exc)\n" + _failed_row("AuthError")
    src = _handler(_broad("except Exception as exc:", row))
    assert len(_f021(tmp_path, src)) == 1


def test_fires_under_a_guard_that_is_always_true_in_a_handler(tmp_path: Path) -> None:
    for guard in (
        "exc",
        "exc is not None",
        "not exc is None",
        "isinstance(exc, Exception)",
    ):
        row = (
            f"            if {guard}:\n"
            "                raise AuthError(message='x', suggested_action='y')\n"
        )
        src = _handler(_broad("except Exception as exc:", row))
        assert len(_f021(tmp_path, src)) == 1, guard


def test_silent_under_a_match_on_the_exception(tmp_path: Path) -> None:
    row = (
        "            match exc:\n"
        "                case PermissionError():\n"
        "                    raise AuthError(message='x', suggested_action='y')\n"
    )
    src = _handler(_broad("except Exception as exc:", row))
    assert _f021(tmp_path, src) == []


def test_nested_try_reports_only_the_inner_handler(tmp_path: Path) -> None:
    row = (
        "            try:\n"
        "                await retry()\n"
        "            except Exception as inner:\n"
        "                raise AuthError(message='x', suggested_action='y') from inner\n"
    )
    findings = _f021(tmp_path, _handler(_broad("except Exception as exc:", row)))
    assert len(findings) == 1


def test_nested_try_that_classifies_inside_does_not_flag_the_outer(
    tmp_path: Path,
) -> None:
    row = (
        "            try:\n"
        "                await retry()\n"
        "            except Exception as inner:\n"
        "                if isinstance(inner, ValueError):\n"
        "                    raise AuthError(message='x', suggested_action='y')\n"
    )
    src = _handler(_broad("except Exception as exc:", row))
    assert _f021(tmp_path, src) == []


def test_keyword_only_parameter_does_not_resolve_to_a_module_function(
    tmp_path: Path,
) -> None:
    extra = "async def check(client):\n" + _broad(
        "except Exception as exc:", _failed_row("AuthError")
    )
    src = _handler(
        "        return await self._runner(check=self._ok)\n"
        "    async def _runner(self, *, check):\n"
        "        return await run(check)\n",
        extra,
    )
    assert _f021(tmp_path, src) == []
