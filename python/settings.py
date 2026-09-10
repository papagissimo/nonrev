"""
Cadence settings (tier windows, recheck gaps) - what used to live in Apps
Script's PropertiesService. Local equivalent is a tiny key/value table
inside nonrev.db itself, so there's still exactly one file holding
everything about this project, not a second settings file living
alongside it.

load_settings/save_settings take an optional `key`/`defaults` pair so
other modules can store their own settings blob in this same table
(e.g. ServiceGrouping's open/full thresholds) without a second table or
file - existing callers that pass neither keep working exactly as before,
reading/writing the 'nextUpSettings' blob.
"""

import json

SETTINGS_KEY = 'nextUpSettings'

# Same gut-feel starting number as the old DEFAULT_NEXT_UP_SETTINGS_ in
# Entrydialog.gs.js - not derived, just where the old system started too.
# One tier only, spanning the whole candidate window (see get_next_batch's
# 4-day schedule_days pool) - maxHours is a fixed ceiling well past
# anything that pool can ever produce, not something he needs to tune;
# recheckGapHours is the one real knob (settings pane) - how soon a route
# can come back up after being logged. There's no separate "logEverything"
# mode anymore: this single wide-open tier plus the normal 45-minute
# departure cutoff (DEP_CUTOFF_MINUTES) already covers "keep offering me
# the same flights every day until they depart."
DEFAULT_SETTINGS = {
    'tiers': [
        {'minHours': 0, 'maxHours': 100, 'recheckGapHours': 0.5},
    ],
    # "Golden ticket" - his term for a reading close enough to departure
    # to trust as the real go/no-go signal. Configurable rather than
    # hardcoded since he expects to tune it, but tuning it never touches
    # already-logged data - see goldenTicketHours usage in get_launcher_summary.
    'goldenTicketHours': 1.5,
}


def ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    conn.commit()


def load_settings(conn, key=SETTINGS_KEY, defaults=None):
    if defaults is None:
        defaults = DEFAULT_SETTINGS
    ensure_table(conn)
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (key,)
    ).fetchone()
    if row is None:
        return dict(defaults)
    try:
        loaded = json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return dict(defaults)
    # Backfill any top-level key an older saved blob predates rather than
    # replacing the whole thing - his real choices stay intact.
    merged = dict(defaults)
    merged.update(loaded)
    return merged


DECLINE_CURVE_SETTINGS_KEY = 'declineCurveSettings'

# Tunables for DeclineCurveFit.py's step-change correction loop (see that
# module's docstring for the algorithm itself). Exposed on the Pooling
# Settings page since decline-curve fitting is one of that page's named
# pooling consumers - not a separate settings surface.
DEFAULT_DECLINE_CURVE_SETTINGS = {
    # RMSE (seats) on an instance's own interior (1-8) readings, below
    # which the correction loop stops - a round-number pick, not derived,
    # meant to be tuned by eye against real reports.
    'stepChangeRmseThreshold': 0.75,
    # Hard cap on correction passes per instance. Necessary because
    # nothing about this loop is guaranteed to converge on its own -
    # correcting a point in place (rather than removing it) means the
    # candidate set never shrinks, so there's no structural bound on
    # runtime without one. Not tied to the old duplicate-row issue
    # (confirmed fixed/pre-server-era) - this is a general safety cap.
    'stepChangeMaxIterations': 15,
}


def save_settings(conn, settings, key=SETTINGS_KEY):
    ensure_table(conn)
    conn.execute(
        """INSERT INTO settings (key, value) VALUES (?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
        (key, json.dumps(settings)),
    )
    conn.commit()
