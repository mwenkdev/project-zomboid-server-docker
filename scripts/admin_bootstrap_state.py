#!/usr/bin/env python3
"""Report whether the configured admin account exists in the server database."""

import argparse
from pathlib import Path
import sqlite3


def needs_bootstrap(database: Path, username: str) -> bool:
    if not database.exists():
        return True
    # Read-only mode prevents an accidental empty DB from being created while
    # checking. A corrupt or inaccessible DB must fail startup, not cause us to
    # feed a password into the running game's console.
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        tables = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='whitelist'"
        ).fetchone()
        if not tables:
            return True
        return connection.execute(
            "SELECT 1 FROM whitelist WHERE username = ? LIMIT 1", (username,)
        ).fetchone() is None


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("username")
    args = parser.parse_args()
    print("needed" if needs_bootstrap(args.database, args.username) else "exists")
