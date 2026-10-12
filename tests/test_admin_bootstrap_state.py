from pathlib import Path
import sqlite3
import tempfile
import unittest

from scripts.admin_bootstrap_state import needs_bootstrap


class AdminBootstrapTests(unittest.TestCase):
    def test_missing_database_needs_bootstrap(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertTrue(needs_bootstrap(Path(directory) / "new.db", "admin"))

    def test_existing_account_is_not_bootstrapped_again(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "server.db"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE whitelist (username TEXT)")
                connection.execute("INSERT INTO whitelist VALUES ('admin')")
            self.assertFalse(needs_bootstrap(database, "admin"))
            self.assertTrue(needs_bootstrap(database, "another-admin"))

    def test_corrupt_database_fails_instead_of_sending_password_to_console(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "server.db"
            database.write_text("not a SQLite database")
            with self.assertRaises(sqlite3.DatabaseError):
                needs_bootstrap(database, "admin")


if __name__ == "__main__":
    unittest.main()
