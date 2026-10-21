"""
Bulk check: confirms any airport code in flightSchedule that isn't in
confirmedAirports yet. New airports normally get confirmed in the Edit
Schedule dialog when the route is saved; this is for checking the whole
table at once.

    python confirm_airports.py

For each unconfirmed code, shows what airportsdata says it is and asks
for an explicit y/n before trusting it. Does nothing if there's nothing
new - safe to run any time.
"""

import os
import sqlite3
import sys

import timezones

DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'nonrev.db')


def codes_in_use(conn):
    rows = conn.execute("SELECT DISTINCT org FROM flightSchedule UNION SELECT DISTINCT dest FROM flightSchedule").fetchall()
    return sorted({r[0].strip().upper() for r in rows if r[0]})


def main():
    if not os.path.exists(DB_PATH):
        sys.exit(f"nonrev.db not found at {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    in_use = codes_in_use(conn)
    pending = timezones.unconfirmed_codes(conn, in_use)

    if not pending:
        print(f"All {len(in_use)} airport code(s) currently in flightSchedule are already confirmed. Nothing to do.")
        return

    print(f"{len(pending)} airport code(s) in flightSchedule need confirming:\n")

    for code in pending:
        rec = timezones.lookup_airport(code)
        if rec is None:
            print(f"  {code}: NOT a real IATA code as far as airportsdata knows. "
                  f"This is almost certainly a typo in flightSchedule - fix it there, "
                  f"not here. Skipping.\n")
            continue

        print(f"  {code}: {rec['name']}, {rec['city']}, {rec['country']}  (timezone: {rec['tz']})")
        answer = input(f"    Confirm this is correct? [y/N] ").strip().lower()
        if answer == 'y':
            timezones.confirm_airport(conn, code)
            print(f"    Confirmed.\n")
        else:
            print(f"    Skipped - '{code}' will keep failing loudly until it's confirmed.\n")

    conn.close()


if __name__ == '__main__':
    main()
