"""
Airport code -> real, DST-aware time-zone math - now gated behind an
explicit confirmedAirports table instead of a "does this look weird"
heuristic.

Any code not yet in confirmedAirports - whether a genuinely new airport
or a typo - fails loudly and identically: UnconfirmedAirportError naming
the code. There's no silent fallback to airportsdata at request time;
airportsdata is only consulted at the moment a human is looking at the
result before it's trusted - the Edit Schedule dialog's confirm prompt
when a route with a new airport is saved, or confirm_airports.py.

zoneinfo (Python stdlib) still does the actual date-dependent, DST-aware
UTC offset math once a zone name is known.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import airportsdata

_ET_ZONE = ZoneInfo('America/New_York')
_AIRPORTS = None


class UnconfirmedAirportError(Exception):
    """
    Raised when a code isn't in confirmedAirports yet - covers both a
    genuinely new airport and a fat-fingered code identically, since
    from here they're indistinguishable. Fix: save the route in Edit
    Schedule and confirm the airport there.
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
    it's already been explicitly confirmed.
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
            f"can't tell which from here). Save a route using it in Edit "
            f"Schedule to review and confirm it, then try again."
        )
    return row[0]


def lookup_airport(airport_code):
    """
    What airportsdata says a code is, as {code, name, city, country, tz},
    or None if it isn't a real IATA code. Never trusted on its own -
    only shown to a human, who then calls confirm_airport.
    """
    global _AIRPORTS
    if _AIRPORTS is None:
        _AIRPORTS = airportsdata.load('IATA')
    code = airport_code.strip().upper()
    rec = _AIRPORTS.get(code)
    if rec is None:
        return None
    return {'code': code, 'name': rec['name'], 'city': rec['city'],
            'country': rec['country'], 'tz': rec['tz']}


def unconfirmed_codes(conn, airport_codes):
    ensure_table(conn)
    pending = []
    for code in sorted({c.strip().upper() for c in airport_codes}):
        row = conn.execute(
            "SELECT 1 FROM confirmedAirports WHERE code = ?", (code,)
        ).fetchone()
        if row is None:
            pending.append(code)
    return pending


def confirm_airport(conn, airport_code):
    ensure_table(conn)
    rec = lookup_airport(airport_code)
    if rec is None:
        raise ValueError(f"'{airport_code.strip().upper()}' isn't a real airport code "
                         f"as far as airportsdata knows - check the spelling.")
    conn.execute(
        """INSERT OR IGNORE INTO confirmedAirports (code, name, country, tz, confirmedAt)
           VALUES (?,?,?,?,?)""",
        (rec['code'], rec['name'], rec['country'], rec['tz'],
         datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')),
    )
    conn.commit()
    return rec


def et_equivalent_datetime(conn, dep_minutes, airport_code, on_date):
    """
    Returns the real, absolute, timezone-aware ET datetime for a local
    departure time - given as minutes-since-midnight (e.g. 455 for
    07:35) - at airport_code, on a specific calendar date (matters
    because the correct UTC offset can differ depending on the date,
    DST).

    Because this is an actual instant in time (not a same-day-relative
    number), it's safe to compare or subtract directly against any other
    aware datetime - e.g. "now" - even when the two sides fall on
    different calendar dates. That's what makes it possible to reason
    about a flight whose origin-local calendar date is still "yesterday"
    even though it's already a new day in ET.
    """
    h, m = divmod(int(dep_minutes), 60)

    tz_name = get_confirmed_timezone(conn, airport_code)
    local_tz = ZoneInfo(tz_name)

    local_dt = datetime(on_date.year, on_date.month, on_date.day, h, m, tzinfo=local_tz)
    return local_dt.astimezone(_ET_ZONE)


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
    et_equivalent_minutes values computed with the SAME on_date, never
    displayed directly. Comparing values computed against two different
    on_date anchors will be off by whatever the ET midnight-to-midnight
    gap between those two dates actually was (normally exactly 1440
    minutes, but not on the two nights DST changes) - use
    et_equivalent_datetime instead for any comparison that might cross a
    calendar date.
    """
    et_dt = et_equivalent_datetime(conn, dep_minutes, airport_code, on_date)
    et_midnight = datetime(on_date.year, on_date.month, on_date.day, tzinfo=_ET_ZONE)
    return (et_dt - et_midnight).total_seconds() / 60
