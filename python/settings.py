"""
Small key/value settings blobs (nextUpSettings, decline-curve tunables,
etc.) - what used to live in Apps Script's PropertiesService. Local
equivalent is a tiny key/value table inside nonrev.db itself, so there's
still exactly one file holding everything about this project, not a
second settings file living alongside it.

load_settings/save_settings take an optional `key`/`defaults` pair so
other modules can store their own settings blob in this same table
(e.g. ServiceGrouping's open/full thresholds) without a second table or
file - existing callers that pass neither keep working exactly as before,
reading/writing the 'nextUpSettings' blob.
"""

import json

SETTINGS_KEY = 'nextUpSettings'

# No cadence/eligibility tiers here anymore - his real workflow is
# walking every scheduled flight in departure order once per session
# (get_next_batch), logging or blank-skipping each in turn; a tiered
# eligibility engine sitting on top of that just gave already-handled
# flights a way to silently cut back in line ahead of ones he hadn't
# reached yet. goldenTicketHours is the one setting left here.
DEFAULT_SETTINGS = {
    # "Golden ticket" - his term for a reading close enough to departure
    # to trust as the real go/no-go signal. Configurable rather than
    # hardcoded since he expects to tune it, but tuning it never touches
    # already-logged data.
    'goldenTicketHours': 1.5,
    'lookaheadDays': 2,
}


def validate_next_up_settings(new_settings):
    lookahead = new_settings.get('lookaheadDays')
    if isinstance(lookahead, bool) or not isinstance(lookahead, int) or lookahead < 0:
        raise ValueError('lookaheadDays must be a whole number, 0 or more')


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
    # Day/night decline-rate split (see DeclineCurveFit.py's
    # piecewise_model / effective_hours_between): the nightly window, in
    # Eastern clock hours (not origin-airport-local - this is measured
    # against checkTimestamp, which is always logged in ET regardless of
    # which airport the flight departs from), during which decline runs
    # at slope * nightSlopeRatio (below) instead of the full daytime
    # slope. A step, not a smooth curve - his call. 21:00-07:00, set
    # 2026-09-17 - confirmed against real data that the exact boundary
    # barely moves the fitted values (median difference: zero, across
    # 458 real instances compared at 22-06 vs 21-07), but a wider window
    # does meaningfully increase how many instances have real night-side
    # evidence at all (120 -> 150) rather than falling back to the
    # global default below - picked for that reason, not for precision.
    'nightStartHour': 21,
    'nightEndHour': 7,
}


DECLINE_CURVE_GLOBAL_DEFAULTS_KEY = 'declineCurveGlobalDefaults'

# Tier 1 of the coefficients hierarchy (see DeclineCurveFit.py's
# "COEFFICIENTS HIERARCHY" docstring section) - one c1Hours/
# slopeSeatsPerHour pair per cabin, applied to any (org, dest,
# dayOfWeek, depTime) with nothing more specific available. Starting
# values are placeholders (a round c1 of 3h before departure, a round
# slope of 2 seats/hour) - meant to be edited from the Pooling Settings
# page once he has a feel for reasonable starting numbers, not derived
# from anything.
#
# nightSlopeRatio: fraction of the daytime slope that applies overnight
# (see nightStartHour/nightEndHour above) - 0.25 is his starting number
# from the 2026-09-13 same-instance day-vs-night comparison, applied
# globally to every cabin until a service has enough of its own
# overnight-spanning instances to derive its own (tier 4 - not built
# yet, see DeclineCurveFit.py module docstring). d1 has essentially no
# data on his current routes either way, so its number is a placeholder
# like everything else about d1 here.
DEFAULT_DECLINE_CURVE_GLOBAL_DEFAULTS = {
    'y':         {'c1Hours': 3.0, 'slopeSeatsPerHour': 2.0, 'nightSlopeRatio': 0.25},
    'cPlus':     {'c1Hours': 3.0, 'slopeSeatsPerHour': 2.0, 'nightSlopeRatio': 0.25},
    'firstOrPS': {'c1Hours': 3.0, 'slopeSeatsPerHour': 2.0, 'nightSlopeRatio': 0.25},
    'd1':        {'c1Hours': 3.0, 'slopeSeatsPerHour': 2.0, 'nightSlopeRatio': 0.25},
}


def save_settings(conn, settings, key=SETTINGS_KEY):
    ensure_table(conn)
    conn.execute(
        """INSERT INTO settings (key, value) VALUES (?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
        (key, json.dumps(settings)),
    )
    conn.commit()
