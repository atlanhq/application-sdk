"""Tests for the Daft ADLS scoped-key patch."""

import sys
import types
import unittest

from application_sdk.lakehouse._polaris.azure_adls_patch import patch_daft_adls_io


class TestPatchDaftAdlsIo(unittest.TestCase):
    def setUp(self):
        self._orig_modules = {}
        for name in (
            "daft",
            "daft.io",
            "daft.io.iceberg",
            "daft.io.iceberg._iceberg",
        ):
            self._orig_modules[name] = sys.modules.get(name)

        daft_mod = types.ModuleType("daft")
        io_mod = types.ModuleType("daft.io")
        ice_mod = types.ModuleType("daft.io.iceberg")
        inner = types.ModuleType("daft.io.iceberg._iceberg")
        self._calls = []

        def _converter(props: dict):
            self._calls.append(dict(props))
            return {"converted": True, "props": props}

        inner._convert_iceberg_file_io_properties_to_io_config = _converter
        sys.modules["daft"] = daft_mod
        sys.modules["daft.io"] = io_mod
        sys.modules["daft.io.iceberg"] = ice_mod
        sys.modules["daft.io.iceberg._iceberg"] = inner
        self._inner = inner

    def tearDown(self):
        for name, mod in self._orig_modules.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod

    def test_normalises_sas_token_account_scoped_key(self):
        patch_daft_adls_io()
        patched = self._inner._convert_iceberg_file_io_properties_to_io_config
        result = patched(
            {"adls.sas-token.myacct.dfs.core.windows.net": "sv=...&sig=..."}
        )
        self.assertEqual(len(self._calls), 1)
        normalised = self._calls[0]
        self.assertEqual(normalised["adls.sas-token"], "sv=...&sig=...")
        self.assertEqual(normalised["adls.account-name"], "myacct")
        self.assertTrue(result["converted"])

    def test_normalises_account_key_scoped_key(self):
        patch_daft_adls_io()
        patched = self._inner._convert_iceberg_file_io_properties_to_io_config
        patched({"adls.account-key.acct2.dfs.core.windows.net": "k1=="})
        normalised = self._calls[0]
        self.assertEqual(normalised["adls.account-key"], "k1==")
        self.assertEqual(normalised["adls.account-name"], "acct2")

    def test_passes_through_non_adls_keys(self):
        patch_daft_adls_io()
        patched = self._inner._convert_iceberg_file_io_properties_to_io_config
        patched({"s3.access-key-id": "AKIA..."})
        normalised = self._calls[0]
        self.assertEqual(normalised["s3.access-key-id"], "AKIA...")
        self.assertNotIn("adls.sas-token", normalised)

    def test_preserves_existing_bare_keys(self):
        patch_daft_adls_io()
        patched = self._inner._convert_iceberg_file_io_properties_to_io_config
        patched(
            {
                "adls.sas-token": "explicit-bare",
                "adls.sas-token.myacct.dfs.core.windows.net": "scoped-value",
            }
        )
        normalised = self._calls[0]
        self.assertEqual(normalised["adls.sas-token"], "explicit-bare")

    def test_idempotent(self):
        patch_daft_adls_io()
        first = self._inner._convert_iceberg_file_io_properties_to_io_config
        patch_daft_adls_io()
        second = self._inner._convert_iceberg_file_io_properties_to_io_config
        self.assertIs(first, second)

    def test_no_op_when_daft_missing(self):
        for name in (
            "daft",
            "daft.io",
            "daft.io.iceberg",
            "daft.io.iceberg._iceberg",
        ):
            sys.modules.pop(name, None)
        patch_daft_adls_io()  # must not raise


if __name__ == "__main__":
    unittest.main()
