"""Tests for A5: RewriteHandlersCodemod."""

from __future__ import annotations

import libcst as cst

from tests.unit.tools.test_codemods.conftest import transform
from tools.migrate_v3.codemods.rewrite_handlers import RewriteHandlersCodemod

_HANDLER_IMPORT = "from application_sdk.handler import Handler\n\n"


class TestHandlerMethodRewrite:
    def test_test_auth_rewritten(self) -> None:
        source = (
            _HANDLER_IMPORT + "class MyHandler(Handler):\n"
            "    async def test_auth(self, *args, **kwargs) -> bool:\n"
            "        return True\n"
        )
        code, changes = transform(source, RewriteHandlersCodemod)
        assert "*args" not in code
        assert "**kwargs" not in code
        assert "input: AuthInput" in code
        assert "AuthOutput" in code
        assert any("test_auth" in c for c in changes)

    def test_preflight_check_rewritten(self) -> None:
        source = (
            _HANDLER_IMPORT + "class MyHandler(Handler):\n"
            "    async def preflight_check(self, **kwargs):\n"
            "        pass\n"
        )
        code, changes = transform(source, RewriteHandlersCodemod)
        assert "input: PreflightInput" in code
        assert "PreflightOutput" in code

    def test_fetch_metadata_rewritten(self) -> None:
        source = (
            _HANDLER_IMPORT + "class MyHandler(Handler):\n"
            "    async def fetch_metadata(self, *args, **kwargs):\n"
            "        pass\n"
        )
        code, changes = transform(source, RewriteHandlersCodemod)
        assert "input: MetadataInput" in code
        assert "MetadataOutput" in code

    def test_load_method_deleted(self) -> None:
        source = (
            _HANDLER_IMPORT + "class MyHandler(Handler):\n"
            "    def load(self):\n"
            "        self.x = 1\n\n"
            "    async def test_auth(self, *args, **kwargs) -> bool:\n"
            "        return True\n"
        )
        code, changes = transform(source, RewriteHandlersCodemod)
        assert "def load(" not in code
        assert any("load" in c and "Deleted" in c for c in changes)

    def test_already_typed_skipped(self) -> None:
        source = (
            _HANDLER_IMPORT + "class MyHandler(Handler):\n"
            "    async def test_auth(self, input: AuthInput) -> AuthOutput:\n"
            '        return AuthOutput(status="success")\n'
        )
        code, changes = transform(source, RewriteHandlersCodemod)
        # No changes to test_auth
        assert not any("test_auth" in c for c in changes)
        assert code.count("AuthInput") == 1


class TestNonHandlerClass:
    def test_non_handler_class_not_touched(self) -> None:
        source = (
            "class SomeOtherClass:\n"
            "    async def test_auth(self, *args, **kwargs) -> bool:\n"
            "        return True\n\n"
            "    def load(self):\n"
            "        pass\n"
        )
        code, changes = transform(source, RewriteHandlersCodemod)
        assert not changes
        assert "*args" in code
        assert "def load(" in code


class TestMultipleHandlers:
    def test_multiple_handlers_in_file_both_rewritten(self) -> None:
        source = (
            _HANDLER_IMPORT + "class HandlerA(Handler):\n"
            "    async def test_auth(self, *args, **kwargs) -> bool:\n"
            "        return True\n\n"
            "class HandlerB(Handler):\n"
            "    async def preflight_check(self, **kwargs):\n"
            "        pass\n"
        )
        code, changes = transform(source, RewriteHandlersCodemod)
        assert "*args" not in code
        assert "**kwargs" not in code
        assert "input: AuthInput" in code
        assert "input: PreflightInput" in code


class TestImportsToAdd:
    def test_handler_imports_collected(self) -> None:
        source = (
            _HANDLER_IMPORT + "class MyHandler(Handler):\n"
            "    async def fetch_metadata(self, *args, **kwargs):\n"
            "        pass\n"
        )
        tree = cst.parse_module(source)
        codemod = RewriteHandlersCodemod()
        codemod.transform(tree)
        names = [n for _, n in codemod.imports_to_add]
        assert "MetadataInput" in names
        assert "MetadataOutput" in names
        modules = {m for m, _ in codemod.imports_to_add}
        assert "application_sdk.handler.contracts" in modules
