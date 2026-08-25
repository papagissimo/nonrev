"""
FlightScheduleDialog backend: port of Scheduleeditdialog.js's
getScheduleForRouteDay / saveScheduleForRouteDay / copyToOtherDays to
Python against nonrev.db.

Real differences from the Sheets version, not just syntax:
  - Row identity is SQLite `rowid`, not a sheet row number - same pattern
    SeatLoggingDialog.py already uses for `scheduleRow`.
  - depTime is INTEGER minutes-since-midnight (see the depTime migration),
    not a 24h HHMM int - display formatting reuses SeatLoggingDialog's
    minutes_to_12h rather than duplicating it.
  - routeDurations already exists as its own table in nonrev.db (see
    create_db.py) - there's no separate sheet to set up or gate behind a
    one-time menu action the way RouteDurations was in Sheets.
  - Since SQLite has no row-shift-on-delete concern (rowid is stable
    regardless of other rows), the old three-pass update/delete/insert
    order is kept only where it still matters: deletes and inserts can
    run in any order relative to each other now, but updates still use
    original rowids captured before any writes in this save.
"""

import re
from datetime import datetime

from SeatLoggingDialog import minutes_to_12h

BOGUS_RE = re.compile(r'^bogus(\d+)$', re.IGNORECASE)
DOW_ORDER = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']


def normalize_dow(dow):
    """Any case/length in -> 'Mon'-style out, matching flightSchedule's
    existing dayOfWeek storage (from loader.py / SeatLoggingDialog.py's
    strftime('%a')), not the lowercase convention the old Sheets version used."""
    s = str(dow or '').strip()
    return s[:3].capitalize()


def load_aircraft_options(conn):
    return [r[0] for r in conn.execute(
        "SELECT configKey FROM aircraftConfigs ORDER BY rowid"
    ).fetchall()]


def get_next_bogus_number(conn):
    """Highest existing bogusNNN across the WHOLE table (not just one
    route), zero-padded to 3 digits to match the convention already in
    real data (bogus001..bogus345) - a deliberate fix versus the old
    Sheets version, which never zero-padded."""
    rows = conn.execute("SELECT flightNumber FROM flightSchedule").fetchall()
    max_n = 0
    for (flight_number,) in rows:
        m = BOGUS_RE.match(flight_number or '')
        if m:
            max_n = max(max_n, int(m.group(1)))
    return max_n + 1


def get_route_duration(conn, org, dest):
    row = conn.execute(
        "SELECT durationMinutes, confirmed FROM routeDurations WHERE org=? AND dest=?",
        (org, dest),
    ).fetchone()
    if row is None:
        return None
    return {'durationMinutes': row[0], 'confirmed': bool(row[1])}


def save_route_duration(conn, org, dest, duration_minutes):
    conn.execute(
        """INSERT INTO routeDurations (org, dest, durationMinutes, confirmed)
           VALUES (?,?,?,1)
           ON CONFLICT(org, dest) DO UPDATE SET durationMinutes=excluded.durationMinutes, confirmed=1""",
        (org, dest, duration_minutes),
    )


def get_schedule_for_route_day(conn, org, dest, dow):
    org = str(org or '').strip().lower()
    dest = str(dest or '').strip().lower()
    dow = normalize_dow(dow)

    rows = conn.execute(
        """SELECT rowid, carrier, flightNumber, depTime, aircraftConfig, confirmed
           FROM flightSchedule WHERE org=? AND dest=? AND dayOfWeek=?
           ORDER BY depTime""",
        (org, dest, dow),
    ).fetchall()

    any_other_day = conn.execute(
        """SELECT 1 FROM flightSchedule WHERE org=? AND dest=? AND dayOfWeek!=? LIMIT 1""",
        (org, dest, dow),
    ).fetchone() is not None

    row_dicts = [
        {
            'scheduleRow': rowid, 'carrier': carrier or 'dl',
            'flightNumber': flight_number or '', 'dep': dep_time,
            'depDisplay': minutes_to_12h(dep_time), 'aircraftConfig': aircraft_config or '',
            'confirmed': bool(confirmed),
        }
        for rowid, carrier, flight_number, dep_time, aircraft_config, confirmed in rows
    ]

    duration = get_route_duration(conn, org, dest)

    return {
        'org': org, 'dest': dest, 'dow': dow,
        'rows': row_dicts,
        'isNewRoute': len(row_dicts) == 0 and not any_other_day,
        'nextBogusNumber': get_next_bogus_number(conn),
        'aircraftOptions': load_aircraft_options(conn),
        'durationMinutes': duration['durationMinutes'] if duration else '',
        'durationConfirmed': duration['confirmed'] if duration else False,
    }


def cascade_flight_number_rename(conn, org, dest, dow, old_carrier, old_flight_number,
                                  new_carrier, new_flight_number):
    """
    When a schedule row's identity changes (typically a placeholder bogus
    number getting corrected to a real one), any observations already
    logged against the OLD (carrier, flightNumber) for this exact row need
    to move to the new identity too - otherwise they're silently orphaned:
    SeatLoggingDialog's cadence check (get_next_batch/previous_readings_for)
    keys strictly on (carrier, flightNumber, flightDate), so an orphaned
    reading becomes invisible to it, and a flight already logged today can
    get offered again as if it never was.

    Scoped to observations whose flightDate actually falls on this row's
    dayOfWeek (not just matching org/dest/old flightNumber) - a real Delta
    flight number can be reused across multiple days of the week, so a
    real->real correction must not sweep up a different day's readings
    that happen to share the old number.
    """
    if (old_carrier, old_flight_number) == (new_carrier, new_flight_number):
        return 0  # nothing actually changed - don't touch observations

    candidates = conn.execute(
        """SELECT observationId, flightDate FROM observations
           WHERE carrier=? AND flightNumber=? AND org=? AND dest=?""",
        (old_carrier, old_flight_number, org, dest),
    ).fetchall()

    matching_ids = [
        obs_id for obs_id, flight_date in candidates
        if normalize_dow(datetime.strptime(flight_date, '%Y-%m-%d').strftime('%a')) == dow
    ]
    if not matching_ids:
        return 0

    placeholders = ','.join('?' for _ in matching_ids)
    conn.execute(
        f"""UPDATE observations SET carrier=?, flightNumber=?
            WHERE observationId IN ({placeholders})""",
        (new_carrier, new_flight_number, *matching_ids),
    )
    return len(matching_ids)


def save_schedule_for_route_day(conn, payload):
    """
    payload: {
      org, dest, dow,
      rows: [{ scheduleRow (existing rowid, or null for a new row),
               carrier, flightNumber, dep (minutes-since-midnight int),
               aircraftConfig, deleted (bool) }],
      durationMinutes: '' or a number
    }
    """
    org = str(payload['org']).strip().lower()
    dest = str(payload['dest']).strip().lower()
    dow = normalize_dow(payload['dow'])

    # Snapshot BEFORE any writes - the copy-to-other-days offer must only
    # ever fire for a route with genuinely zero rows anywhere else.
    is_new_route = get_schedule_for_route_day(conn, org, dest, dow)['isNewRoute']

    for entry in payload['rows']:
        if not entry.get('scheduleRow') or entry.get('deleted'):
            continue

        old_row = conn.execute(
            "SELECT carrier, flightNumber FROM flightSchedule WHERE rowid=?",
            (entry['scheduleRow'],),
        ).fetchone()
        new_carrier = entry.get('carrier') or 'dl'
        new_flight_number = entry['flightNumber']

        if old_row is not None:
            cascade_flight_number_rename(
                conn, org, dest, dow,
                old_row[0], old_row[1],
                new_carrier, new_flight_number,
            )

        conn.execute(
            """UPDATE flightSchedule
               SET carrier=?, flightNumber=?, depTime=?, aircraftConfig=?, confirmed=1
               WHERE rowid=?""",
            (new_carrier, new_flight_number, entry['dep'],
             entry['aircraftConfig'], entry['scheduleRow']),
        )

    to_delete = [e['scheduleRow'] for e in payload['rows'] if e.get('scheduleRow') and e.get('deleted')]
    for rowid in to_delete:
        conn.execute("DELETE FROM flightSchedule WHERE rowid=?", (rowid,))

    new_rows = [e for e in payload['rows'] if not e.get('scheduleRow') and not e.get('deleted')]
    for entry in new_rows:
        conn.execute(
            """INSERT INTO flightSchedule
               (carrier, flightNumber, org, dest, dayOfWeek, depTime, aircraftConfig, confirmed, classification)
               VALUES (?,?,?,?,?,?,?,1,NULL)""",
            (entry.get('carrier') or 'dl', entry['flightNumber'], org, dest, dow,
             entry['dep'], entry['aircraftConfig']),
        )

    duration_minutes = payload.get('durationMinutes')
    if duration_minutes not in ('', None):
        save_route_duration(conn, org, dest, int(duration_minutes))

    conn.commit()

    return {
        'savedCount': len(payload['rows']),
        'offerCopy': is_new_route and len(new_rows) > 0,
        'org': org, 'dest': dest, 'dow': dow,
    }


def copy_to_other_days(conn, org, dest, source_dow):
    org = str(org).strip().lower()
    dest = str(dest).strip().lower()
    source_dow = normalize_dow(source_dow)

    source_rows = conn.execute(
        """SELECT carrier, flightNumber, depTime, aircraftConfig
           FROM flightSchedule WHERE org=? AND dest=? AND dayOfWeek=?""",
        (org, dest, source_dow),
    ).fetchall()

    target_days = [d for d in DOW_ORDER if d != source_dow]
    copied_count = 0
    for day in target_days:
        for carrier, flight_number, dep_time, aircraft_config in source_rows:
            conn.execute(
                """INSERT INTO flightSchedule
                   (carrier, flightNumber, org, dest, dayOfWeek, depTime, aircraftConfig, confirmed, classification)
                   VALUES (?,?,?,?,?,?,?,0,NULL)""",
                (carrier, flight_number, org, dest, day, dep_time, aircraft_config),
            )
            copied_count += 1

    conn.commit()
    return {'copiedCount': copied_count}
