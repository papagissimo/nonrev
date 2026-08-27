"""
One-off migration: adds flightDayFlag and routeDayFlag to an existing
nonrev.db. Safe to run more than once (checks before creating). Delete
this file once you've run it and confirmed it - create_db.py already
reflects the same end state, same convention as migrate_deptime.py
before it.

    python migrate_add_flags.py
"""

import sqlite3

DB_PATH = "../nonrev.db"

FLIGHT_DAY_FLAG = """
CREATE TABLE IF NOT EXISTS flightDayFlag (
    carrier       TEXT NOT NULL,
    flightNumber  TEXT NOT NULL,
    org           TEXT NOT NULL,
    dest          TEXT NOT NULL,
    flightDate    TEXT NOT NULL,
    flag          TEXT,
    PRIMARY KEY (carrier, flightNumber, org, dest, flightDate)
);
"""

ROUTE_DAY_FLAG = """
CREATE TABLE IF NOT EXISTS routeDayFlag (
    carrier     TEXT NOT NULL,
    org         TEXT NOT NULL,
    dest        TEXT NOT NULL,
    flightDate  TEXT NOT NULL,
    flag        TEXT,
    PRIMARY KEY (carrier, org, dest, flightDate)
);
"""


def migrate():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(FLIGHT_DAY_FLAG)
    conn.executescript(ROUTE_DAY_FLAG)
    conn.commit()
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name IN ('flightDayFlag', 'routeDayFlag')"
    ).fetchall()]
    conn.close()
    print(f"Present after migration: {tables}")


if __name__ == "__main__":
    migrate()
