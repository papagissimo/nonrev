"""
One-off migration: adds the verdictType column (info/warning) to
flightSchedule on the live nonrev.db, alongside the existing verdict
text field. Run once, then delete - create_db.py is the permanent
schema doc and already reflects this end state.

    python3 migrate_add_verdict_type.py
"""

import sqlite3

DB_PATH = 'nonrev.db'


def main():
    con = sqlite3.connect(DB_PATH)
    cols = [r[1] for r in con.execute("PRAGMA table_info(flightSchedule)").fetchall()]
    if 'verdictType' in cols:
        print("verdictType already exists - nothing to do.")
        return

    con.execute(
        "ALTER TABLE flightSchedule ADD COLUMN verdictType TEXT NOT NULL DEFAULT 'info'"
    )
    con.commit()
    print("Added verdictType (default 'info') to flightSchedule.")


if __name__ == '__main__':
    main()
