"""
Cadence settings (tier windows, recheck gaps, logEverything toggle) -
what used to live in Apps Script's PropertiesService. Local equivalent is
a tiny key/value table inside nonrev.db itself, so there's still exactly
one file holding everything about this project, not a second settings
file living alongside it.
"""

import json

SETTINGS_KEY = 'nextUpSettings'

# Same gut-feel starting numbers as the old DEFAULT_NEXT_UP_SETTINGS_ in
# Entrydialog.gs.js - not derived, just where the old system started too.
DEFAULT_SETTINGS = {
    'tiers': [
        {'minHours': 0, 'maxHours': 2, 'recheckGapHours': 0.5},
        {'minHours': 3.5, 'maxHours': 4.5, 'targetHours': 4,
         'doneToleranceHours': 0.3, 'recheckGapHours': 1},
    ],
    'logEverything': False,
}


def ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    conn.commit()


def load_settings(conn):
    ensure_table(conn)
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (SETTINGS_KEY,)
    ).fetchone()
    if row is None:
        return DEFAULT_SETTINGS
    try:
        return json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return DEFAULT_SETTINGS


def save_settings(conn, settings):
    ensure_table(conn)
    conn.execute(
        """INSERT INTO settings (key, value) VALUES (?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
        (SETTINGS_KEY, json.dumps(settings)),
    )
    conn.commit()
