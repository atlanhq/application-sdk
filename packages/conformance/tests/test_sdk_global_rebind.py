"""B010 ``SdkGlobalRebind`` — an app monkeypatching something it imported from the SDK.

FND-4688: two connector apps changed the worker's request normalization by
rebinding ``application_sdk.handler.service`` globals at import time. These
tests pin both fleet shapes, the other rebind forms, and what stays clean.
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest

from conformance.suite.checks._ast_common import _parse_directives
from conformance.suite.checks.deprecation import scan_all
from conformance.suite.checks.deprecation._sdk_global_rebind import (
    scan_sdk_global_rebind,
)
from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import EnforcementTier, RuleScope


def _b010(src: str) -> list:
    src = textwrap.dedent(src)
    return scan_sdk_global_rebind(
        ast.parse(src), "app/handler.py", _parse_directives(src)
    )


# ── the two fleet shapes ────────────────────────────────────────────────────


def test_b010_fires_on_the_credential_keys_rebind() -> None:
    findings = _b010(
        """
        import application_sdk.handler.service as _sdk_handler_svc

        _sdk_handler_svc._CREDENTIAL_KEYS = _sdk_handler_svc._CREDENTIAL_KEYS | frozenset(
            {"kerberos"}
        )
        """
    )
    assert [f.rule_id for f in findings] == ["B010"]
    message = findings[0].message
    assert "application_sdk.handler.service._CREDENTIAL_KEYS" in message
    assert "extra_credential_keys" in message


def test_b010_fires_on_the_normalize_wrapper_rebind() -> None:
    """``from pkg import module`` binds a module through a public path; B008 misses it."""
    findings = _b010(
        """
        try:
            from application_sdk.handler import service as _sdk_handler_service

            _original_normalize = _sdk_handler_service._normalize_credentials

            def _wrapped(body):
                return _original_normalize(body)

            _sdk_handler_service._normalize_credentials = _wrapped
        except (ImportError, AttributeError):
            pass
        """
    )
    assert [f.rule_id for f in findings] == ["B010"]
    message = findings[0].message
    assert "application_sdk.handler.service._normalize_credentials" in message
    assert "prepare_request_body" in message


# ── the other rebind forms ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "src",
    [
        "import application_sdk.constants\napplication_sdk.constants.X = 1\n",
        "import application_sdk\napplication_sdk.constants.X = 1\n",
        "from application_sdk import constants\nconstants.X += 1\n",
        "from application_sdk.handler.base import Handler\nHandler.hook = None\n",
        "from application_sdk.handler import service\nservice.X: int = 1\n",
        "from application_sdk.handler import service\ndel service.X\n",
        "from application_sdk.handler import service\na, service.X = 1, 2\n",
        "from application_sdk.handler import service\nsetattr(service, 'X', 1)\n",
        "from application_sdk.handler import service\ndelattr(service, 'X')\n",
        "from application_sdk import handler\nsetattr(handler.service, name, 1)\n",
        "from application_sdk.app.base import App as A\nA.config.timeout = 5\n",
    ],
)
def test_b010_fires_on_every_rebind_form(src: str) -> None:
    assert [f.rule_id for f in _b010(src)] == ["B010"]


def test_b010_message_without_a_known_replacement_points_at_raising_a_gap() -> None:
    (finding,) = _b010("from application_sdk import constants\nconstants.X = 1\n")
    assert "application_sdk.constants.X" in finding.message
    assert "gap" in finding.message


# ── what stays clean ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "src",
    [
        # Reading is not rebinding.
        "from application_sdk.handler import service\nkeys = service.X\n",
        # The app's own object, even when its class came from the SDK.
        "from application_sdk.clients.sql import SQLClient\n"
        "client = SQLClient()\nclient.engine = None\n",
        # The app's own subclass.
        "from application_sdk.handler.base import Handler\n"
        "class H(Handler):\n    pass\nH.extra = 1\n",
        # Test-scoped patching undoes itself.
        "from application_sdk.handler import service\n"
        "def test(monkeypatch):\n    monkeypatch.setattr(service, 'X', 1)\n",
        "from unittest.mock import patch\n"
        "with patch('application_sdk.handler.service.X', 1):\n    pass\n",
        # Not the SDK.
        "import pandas as pd\npd.options.display.max_rows = 5\n",
        "from .local import service\nservice.X = 1\n",
        # A star import binds nothing B010 can resolve.
        "from application_sdk.handler.service import *\n",
    ],
)
def test_b010_is_silent(src: str) -> None:
    assert _b010(src) == []


def test_b010_honours_an_inline_ignore() -> None:
    (finding,) = _b010(
        """
        from application_sdk import constants
        # conformance: ignore[B010] no public hook yet - tracked in FND-0000
        constants.X = 1
        """
    )
    assert finding.suppressed


# ── catalog + dispatch ──────────────────────────────────────────────────────


def test_b010_is_a_warn_tier_app_rule() -> None:
    rule = get_rule("B010")
    assert rule.scope is RuleScope.APP
    assert rule.tier is EnforcementTier.WARN
    assert rule.autofixable is False


def test_scan_all_runs_b010_on_an_app(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "atlan-example-app"\nversion = "0.1.0"\n'
    )
    app = tmp_path / "app"
    app.mkdir()
    handler = app / "handler.py"
    handler.write_text(
        "import application_sdk.handler.service as svc\n"
        "svc._CREDENTIAL_KEYS = frozenset()\n"
    )
    rule_ids = {f.rule_id for f in scan_all([handler], tmp_path)}
    assert "B010" in rule_ids
