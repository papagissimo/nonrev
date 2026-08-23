"""
Run this whenever flightSchedule might contain an airport code that
hasn't been confirmed yet - most naturally, right after adding a new
route. Also what you run once, the very first time, to establish the
baseline set of airports this project already trusts.

    python confirm_airports.py

For each org/dest code found in flightSchedule that ISN'T already in
confirmedAirports, this looks the code up in airportsdata (the
third-party, maintained table of real-world airports - see
timezones.py's docstring for why that split exists), shows you exactly
what it resolved to, and asks for an explicit y/n before adding it to
confirmedAirports. Nothing gets trusted without you looking at it once.

Says "already confirmed" and does nothing if there's nothing new -
completely safe to run any time, including as a routine check.
"""

import os
import sqlite3
from datetime import datetime, timezone

import airportsdata

import timezones

DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'nonrev.db')

_AIRPORTS = airportsdata.load('IATA')


def codes_in_use(conn):
    rows = conn.execute("SELECT DISTINCT org FROM flightSchedule UNION SELECT DISTINCT dest FROM flightSchedule").fetchall()
    return sorted({r[0].strip().upper() for r in rows if r[0]})


def already_confirmed(conn):
    timezones.ensure_table(conn)
    rows = conn.execute("SELECT code FROM confirmedAirports").fetchall()
    return {r[0] for r in rows}


def main():
    conn = sqlite3.connect(DB_PATH)
    in_use = codes_in_use(conn)
    confirmed = already_confirmed(conn)
    pending = [c for c in in_use if c not in confirmed]

    if not pending:
        print(f"All {len(in_use)} airport code(s) currently in flightSchedule are already confirmed. Nothing to do.")
        return

    print(f"{len(pending)} airport code(s) in flightSchedule need confirming:\n")

    for code in pending:
        rec = _AIRPORTS.get(code)
        if rec is None:
            print(f"  {code}: NOT a real IATA code as far as airportsdata knows. "
                  f"This is almost certainly a typo in flightSchedule - fix it there, "
                  f"not here. Skipping.\n")
            continue

        print(f"  {code}: {rec['name']}, {rec['city']}, {rec['country']}  (timezone: {rec['tz']})")
        answer = input(f"    Confirm this is correct? [y/N] ").strip().lower()
        if answer == 'y':
            conn.execute(
                """INSERT INTO confirmedAirports (code, name, country, tz, confirmedAt)
                   VALUES (?,?,?,?,?)""",
                (code, rec['name'], rec['country'], rec['tz'],
                 datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')),
            )
            conn.commit()
            print(f"    Confirmed.\n")
        else:
            print(f"    Skipped - '{code}' will keep failing loudly until you run this again and confirm it.\n")

    conn.close()


if __name__ == '__main__':
    main()
