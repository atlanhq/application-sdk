"""Internal tests for the DuckDB connection factory."""

import unittest

import duckdb

from application_sdk.lakehouse._duckdb.connection import make_duckdb_connection


class TestMakeDuckdbConnection(unittest.TestCase):
    def test_returns_connection(self):
        conn = make_duckdb_connection()
        try:
            self.assertIsInstance(conn, duckdb.DuckDBPyConnection)
        finally:
            conn.close()

    def test_default_settings_match_popularity_app(self):
        """Default settings mirror popularity-app's make_duckdb_connection."""
        conn = make_duckdb_connection()
        try:
            threads = conn.execute("SELECT current_setting('threads')").fetchone()[0]
            self.assertEqual(int(threads), 1)

            preserve = conn.execute(
                "SELECT current_setting('preserve_insertion_order')"
            ).fetchone()[0]
            self.assertEqual(str(preserve).lower(), "false")

            mem = conn.execute("SELECT current_setting('memory_limit')").fetchone()[0]
            # DuckDB normalises '8GB' to '7.4 GiB' (8e9 / 2^30) — assert it's
            # the right neighbourhood rather than parsing the unit string.
            self.assertIn("GiB", mem)
        finally:
            conn.close()

    def test_threads_param(self):
        conn = make_duckdb_connection(threads=4)
        try:
            threads = conn.execute("SELECT current_setting('threads')").fetchone()[0]
            self.assertEqual(int(threads), 4)
        finally:
            conn.close()

    def test_memory_limit_param(self):
        conn = make_duckdb_connection(memory_limit="2GB")
        try:
            mem = conn.execute("SELECT current_setting('memory_limit')").fetchone()[0]
            # 2 GB is normalised by DuckDB; just assert it's smaller than the default.
            self.assertIn("GiB", mem)
            # Compare numerically — '1.8 GiB' < '7.4 GiB'.
            value = float(mem.split()[0])
            self.assertLess(value, 5.0)
        finally:
            conn.close()

    def test_preserve_insertion_order_param(self):
        conn = make_duckdb_connection(preserve_insertion_order=True)
        try:
            value = conn.execute(
                "SELECT current_setting('preserve_insertion_order')"
            ).fetchone()[0]
            self.assertEqual(str(value).lower(), "true")
        finally:
            conn.close()

    def test_temp_dir_param(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            target = f"{tmp}/duckdb_spill"
            conn = make_duckdb_connection(temp_dir=target)
            try:
                self.assertTrue(os.path.isdir(target))
                td = conn.execute(
                    "SELECT current_setting('temp_directory')"
                ).fetchone()[0]
                self.assertEqual(td, target)
            finally:
                conn.close()


class TestValidationRejectsInjection(unittest.TestCase):
    """``memory_limit`` and ``temp_dir`` go into SET statements via f-string;
    untrusted-shaped values must be rejected at the boundary."""

    def test_rejects_memory_limit_with_quote(self):
        with self.assertRaises(ValueError):
            make_duckdb_connection(memory_limit="8GB'; DROP TABLE x;--")

    def test_rejects_memory_limit_unparseable(self):
        with self.assertRaises(ValueError):
            make_duckdb_connection(memory_limit="huge")

    def test_accepts_memory_limit_decimals_and_units(self):
        # Should not raise.
        for value in ("8GB", "512MB", "1.5GiB", "1024KiB"):
            conn = make_duckdb_connection(memory_limit=value)
            conn.close()

    def test_rejects_temp_dir_with_quote(self):
        with self.assertRaises(ValueError):
            make_duckdb_connection(temp_dir="/tmp/x'; rm -rf /;--")

    def test_rejects_temp_dir_path_traversal(self):
        with self.assertRaises(ValueError):
            make_duckdb_connection(temp_dir="/tmp/foo/../../etc")

    def test_rejects_temp_dir_with_newline(self):
        with self.assertRaises(ValueError):
            make_duckdb_connection(temp_dir="/tmp/x\ninjected")


if __name__ == "__main__":
    unittest.main()
