"""
Airport code -> real, DST-aware time-zone math - now gated behind an
explicit confirmedAirports table instead of a "does this look weird"
heuristic.

Any code not yet in confirmedAirports - whether a genuinely new airport
or a typo - fails loudly and identically: UnconfirmedAirportError naming
the code and pointing at confirm_airports.py. There's no silent
fallback to airportsdata at request time; airportsdata is only consulted
by confirm_airports.py, at the one moment a human is actually looking at
the result before it's trusted.

zoneinfo (Python stdlib) still does the actual date-dependent, DST-aware
UTC offset math once a zone name is known - that part was never in
question, see the confirm_airports.py docstring for the full split.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

_ET_ZONE = ZoneInfo('America/New_York')


class UnconfirmedAirportError(Exception):
    """
    Raised when a code isn't in confirmedAirports yet - covers both a
    genuinely new airport and a fat-fingered code identically, since
    from here they're indistinguishable. Fix: run confirm_airports.py.
    """
    pass


def ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS confirmedAirports (
            code         TEXT PRIMARY KEY,
            name         TEXT NOT NULL,
            country      TEXT NOT NULL,
            tz           TEXT NOT NULL,
            confirmedAt  TEXT NOT NULL
        )
    """)
    conn.commit()


def get_confirmed_timezone(conn, airport_code):
    """
    Returns the IANA time zone name for a 3-letter code, but ONLY if
    it's already been explicitly confirmed (see confirm_airports.py).
    Raises UnconfirmedAirportError otherwise - no fallback lookup here.
    """
    ensure_table(conn)
    code = airport_code.strip().upper()
    row = conn.execute(
        "SELECT tz FROM confirmedAirports WHERE code = ?", (code,)
    ).fetchone()
    if row is None:
        raise UnconfirmedAirportError(
            f"'{code}' hasn't been confirmed yet (new airport or a typo - "
            f"can't tell which from here). Run `python confirm_airports.py` "
            f"to review and confirm it, then try again."
        )
    return row[0]


def et_equivalent_minutes(conn, dep_minutes, airport_code, on_date):
    """
    Converts a local departure time - given as minutes-since-midnight
    (e.g. 455 for 07:35) - at airport_code, on a specific calendar date
    (matters because the correct UTC offset can differ depending on the
    date, DST), into "ET-equivalent minutes since midnight ET" on that
    same date.

    May come back negative or over 1440 if the real ET-equivalent instant
    falls on the adjacent calendar day - expected and fine, this value is
    only ever used for relative comparison against other
    et_equivalent_minutes values, never displayed directly.
    """
    h, m = divmod(int(dep_minutes), 60)

    tz_name = get_confirmed_timezone(conn, airport_code)
    local_tz = ZoneInfo(tz_name)

    local_dt = datetime(on_date.year, on_date.month, on_date.day, h, m, tzinfo=local_tz)
    et_dt = local_dt.astimezone(_ET_ZONE)
    et_midnight = datetime(on_date.year, on_date.month, on_date.day, tzinfo=_ET_ZONE)

    return (et_dt - et_midnight).total_seconds() / 60
