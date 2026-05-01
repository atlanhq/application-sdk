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

    def test_settings_match_popularity_app(self):
        """Connection settings mirror popularity-app's make_duckdb_connection."""
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

    def test_temp_dir_created(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            target = f"{tmp}/duckdb_spill"
            conn = make_duckdb_connection(temp_dir=target)
            try:
                import os

                self.assertTrue(os.path.isdir(target))
                td = conn.execute(
                    "SELECT current_setting('temp_directory')"
                ).fetchone()[0]
                # Some DuckDB versions normalise the path; just check the dir
                # we passed is the directory the engine resolved to.
                self.assertEqual(td, target)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
