"""
FlightScheduleDialog backend: port of Scheduleeditdialog.js's
getScheduleForRouteDay / saveScheduleForRouteDay / copyToOtherDays to
Python against nonrev.db.

Real differences from the Sheets version, not just syntax:
  - Row identity is SQLite `rowid`, not a sheet row number, and not
    flightNumber either - flightNumber isn't a stable identifier at all
    (see domainKnowledge.md / the carriersFltNum_notStable_DO_NOT_USE
    column name) - it's stored purely as decoration from here on, never
    matched or joined on anywhere in this file.
  - depTime is INTEGER minutes-since-midnight (see the depTime
    migration), not a 24h HHMM int - display formatting reuses
    SeatLoggingDialog's minutes_to_12h rather than duplicating it.
  - Editing a schedule row's depTime or flightNumber no longer touches
    observations at all - each observation carries its own immutable
    depTime/hoursBeforeDep snapshot from when it was logged (see
    SeatLoggingDialog.save_entry_dialog), so there's nothing here left
    to cascade-rename or recompute.
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

from SeatLoggingDialog import minutes_to_12h
from ServiceGrouping import (
    get_day_grouping_row, save_day_grouping, get_known_day_groupings,
    get_open_full_counts, format_open_full,
)

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
    rows = conn.execute("SELECT carriersFltNum_notStable_DO_NOT_USE FROM flightSchedule").fetchall()
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
        """SELECT rowid, carrier, carriersFltNum_notStable_DO_NOT_USE, depTime, aircraftConfig, ignore, verdict, verdictType
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
            'ignore': bool(ignore),
            'verdict': verdict or '', 'verdictType': verdict_type or 'info',
            'openFull': format_open_full(
                get_open_full_counts(conn, org, dest, dow, dep_time)
            ),
        }
        for rowid, carrier, flight_number, dep_time, aircraft_config, ignore, verdict, verdict_type in rows
    ]

    duration = get_route_duration(conn, org, dest)
    day_grouping = get_day_grouping_row(conn, org, dest, dow)

    return {
        'org': org, 'dest': dest, 'dow': dow,
        'rows': row_dicts,
        'isNewRoute': len(row_dicts) == 0 and not any_other_day,
        'nextBogusNumber': get_next_bogus_number(conn),
        'aircraftOptions': load_aircraft_options(conn),
        'durationMinutes': duration['durationMinutes'] if duration else '',
        'durationConfirmed': duration['confirmed'] if duration else False,
        'dayGrouping': day_grouping['dayGrouping'],
        'knownDayGroupings': get_known_day_groupings(conn),
    }


def save_schedule_for_route_day(conn, payload):
    """
    payload: {
      org, dest, dow,
      rows: [{ scheduleRow (existing rowid, or null for a new row),
               carrier, flightNumber, dep (minutes-since-midnight int),
               aircraftConfig, ignore (bool), deleted (bool) }],
      durationMinutes: '' or a number,
      dayGrouping: '' or a string (see ServiceGrouping.save_day_grouping)
    }
    """
    org = str(payload['org']).strip().lower()
    dest = str(payload['dest']).strip().lower()
    dow = normalize_dow(payload['dow'])

    for entry in payload['rows']:
        if not entry.get('scheduleRow') or entry.get('deleted'):
            continue

        conn.execute(
            """UPDATE flightSchedule
               SET carrier=?, carriersFltNum_notStable_DO_NOT_USE=?, depTime=?, aircraftConfig=?, ignore=?, verdict=?, verdictType=?
               WHERE rowid=?""",
            (entry.get('carrier') or 'dl', entry['flightNumber'], entry['dep'],
             entry['aircraftConfig'], 1 if entry.get('ignore') else 0,
             (entry.get('verdict') or '').strip() or None,
             entry.get('verdictType') or 'info', entry['scheduleRow']),
        )

    to_delete = [e['scheduleRow'] for e in payload['rows'] if e.get('scheduleRow') and e.get('deleted')]
    for rowid in to_delete:
        conn.execute("DELETE FROM flightSchedule WHERE rowid=?", (rowid,))

    new_rows = [e for e in payload['rows'] if not e.get('scheduleRow') and not e.get('deleted')]
    for entry in new_rows:
        conn.execute(
            """INSERT INTO flightSchedule
               (carrier, carriersFltNum_notStable_DO_NOT_USE, org, dest, dayOfWeek, depTime, aircraftConfig, verdict, verdictType, ignore)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (entry.get('carrier') or 'dl', entry['flightNumber'], org, dest, dow,
             entry['dep'], entry['aircraftConfig'],
             (entry.get('verdict') or '').strip() or None,
             entry.get('verdictType') or 'info', 1 if entry.get('ignore') else 0),
        )

    duration_minutes = payload.get('durationMinutes')
    if duration_minutes not in ('', None):
        save_route_duration(conn, org, dest, int(duration_minutes))

    day_grouping = payload.get('dayGrouping')
    if day_grouping not in ('', None):
        save_day_grouping(conn, org, dest, dow, str(day_grouping).strip())

    conn.commit()

    return {
        'savedCount': len(payload['rows']),
        'org': org, 'dest': dest, 'dow': dow,
    }


def copy_to_other_days(conn, org, dest, source_dow):
    """
    Blanket overwrite: replaces whatever's currently on the other 6 days
    for this route with a fresh copy of source_dow's flights. Deliberately
    no per-day picker or merge - he'd rather wipe and redo than try to
    reason about which of 6 days is still correct from memory.
    """
    org = str(org).strip().lower()
    dest = str(dest).strip().lower()
    source_dow = normalize_dow(source_dow)

    source_rows = conn.execute(
        """SELECT carrier, carriersFltNum_notStable_DO_NOT_USE, depTime, aircraftConfig, ignore, verdict, verdictType
           FROM flightSchedule WHERE org=? AND dest=? AND dayOfWeek=?""",
        (org, dest, source_dow),
    ).fetchall()

    target_days = [d for d in DOW_ORDER if d != source_dow]

    conn.execute(
        """DELETE FROM flightSchedule WHERE org=? AND dest=? AND dayOfWeek IN (%s)"""
        % ','.join('?' for _ in target_days),
        (org, dest, *target_days),
    )

    copied_count = 0
    for day in target_days:
        for carrier, flight_number, dep_time, aircraft_config, ignore, verdict, verdict_type in source_rows:
            conn.execute(
                """INSERT INTO flightSchedule
                   (carrier, carriersFltNum_notStable_DO_NOT_USE, org, dest, dayOfWeek, depTime, aircraftConfig, verdict, verdictType, ignore)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (carrier, flight_number, org, dest, day, dep_time, aircraft_config, verdict, verdict_type, ignore),
            )
            copied_count += 1

    conn.commit()
    return {'copiedCount': copied_count}
