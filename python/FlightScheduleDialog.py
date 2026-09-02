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

from SeatLoggingDialog import minutes_to_12h, ET_ZONE
from timezones import et_equivalent_datetime, UnconfirmedAirportError
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
        """SELECT rowid, carrier, flightNumber, depTime, aircraftConfig, confirmed, ignore, verdict, verdictType
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
            'confirmed': bool(confirmed), 'ignore': bool(ignore),
            'verdict': verdict or '', 'verdictType': verdict_type or 'info',
            'openFull': format_open_full(
                get_open_full_counts(conn, org, dest, dow, carrier or 'dl', flight_number or '')
            ),
        }
        for rowid, carrier, flight_number, dep_time, aircraft_config, confirmed, ignore, verdict, verdict_type in rows
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


def recompute_hours_before_dep(conn, carrier, flight_number, org, dest, dow, dep_time):
    """
    Fixes the stale-hoursBeforeDep bug: that value used to be computed
    once at logging time from whatever depTime was current then, and
    never touched again - so a later depTime correction (typo fix,
    Delta schedule churn) left already-logged rows showing an
    increasingly wrong number of hours-before-departure. Departure time
    is itself an observation, refined over the day same as seat counts,
    so the stored value gets corrected in place here instead.

    Recomputes every observation currently attributed to (carrier,
    flightNumber, org, dest) on this exact dayOfWeek, using the flight's
    current depTime. Called both when a row's depTime is edited directly,
    and after cascade_flight_number_rename reassigns observations onto a
    new flightNumber - either can leave hoursBeforeDep stale.

    Scoped to this exact dow, same as cascade_flight_number_rename and
    for the same reason: a flight number can be reused across different
    days of the week, so matching without dow filtering would sweep up
    a different day's readings. Deliberately inherits cascade's known
    blind spot rather than fixing it: if Delta drops a flight and a
    surviving neighbor absorbs its number, this can recompute
    hoursBeforeDep against the wrong depTime for misattributed
    historical rows. Accepted risk, not addressed here - see project
    handoff on scope.
    """
    candidates = conn.execute(
        """SELECT observationId, flightDate, checkTimestamp FROM observations
           WHERE carrier=? AND flightNumber=? AND org=? AND dest=?""",
        (carrier, flight_number, org, dest),
    ).fetchall()

    updated = 0
    for obs_id, flight_date_str, check_timestamp in candidates:
        flight_date = datetime.strptime(flight_date_str, '%Y-%m-%d').date()
        if normalize_dow(flight_date.strftime('%a')) != dow:
            continue

        try:
            dep_dt = et_equivalent_datetime(conn, dep_time, org, flight_date)
        except UnconfirmedAirportError:
            continue  # can't compute right now - leave the existing value alone

        check_dt = datetime.strptime(check_timestamp, '%Y-%m-%d %H:%M').replace(tzinfo=ET_ZONE)
        hours_before_dep = (dep_dt - check_dt).total_seconds() / 3600
        conn.execute(
            "UPDATE observations SET hoursBeforeDep=? WHERE observationId=?",
            (hours_before_dep, obs_id),
        )
        updated += 1
    return updated


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

        old_row = conn.execute(
            "SELECT carrier, flightNumber, depTime FROM flightSchedule WHERE rowid=?",
            (entry['scheduleRow'],),
        ).fetchone()
        new_carrier = entry.get('carrier') or 'dl'
        new_flight_number = entry['flightNumber']

        if old_row is not None:
            old_carrier, old_flight_number, old_dep_time = old_row
            cascade_flight_number_rename(
                conn, org, dest, dow,
                old_carrier, old_flight_number,
                new_carrier, new_flight_number,
            )

        conn.execute(
            """UPDATE flightSchedule
               SET carrier=?, flightNumber=?, depTime=?, aircraftConfig=?, confirmed=1, ignore=?, verdict=?, verdictType=?
               WHERE rowid=?""",
            (new_carrier, new_flight_number, entry['dep'],
             entry['aircraftConfig'], 1 if entry.get('ignore') else 0,
             (entry.get('verdict') or '').strip() or None,
             entry.get('verdictType') or 'info', entry['scheduleRow']),
        )

        # A depTime correction or a flightNumber rename can both leave
        # already-logged observations' hoursBeforeDep stale - see
        # recompute_hours_before_dep's docstring. Only bother when
        # something that actually feeds the calculation changed.
        if old_row is not None:
            dep_changed = old_dep_time != entry['dep']
            flight_changed = (old_carrier, old_flight_number) != (new_carrier, new_flight_number)
            if dep_changed or flight_changed:
                recompute_hours_before_dep(
                    conn, new_carrier, new_flight_number, org, dest, dow, entry['dep'],
                )

    to_delete = [e['scheduleRow'] for e in payload['rows'] if e.get('scheduleRow') and e.get('deleted')]
    for rowid in to_delete:
        conn.execute("DELETE FROM flightSchedule WHERE rowid=?", (rowid,))

    new_rows = [e for e in payload['rows'] if not e.get('scheduleRow') and not e.get('deleted')]
    for entry in new_rows:
        conn.execute(
            """INSERT INTO flightSchedule
               (carrier, flightNumber, org, dest, dayOfWeek, depTime, aircraftConfig, confirmed, verdict, verdictType, ignore)
               VALUES (?,?,?,?,?,?,?,1,?,?,?)""",
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
        """SELECT carrier, flightNumber, depTime, aircraftConfig, ignore, verdict, verdictType
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
                   (carrier, flightNumber, org, dest, dayOfWeek, depTime, aircraftConfig, confirmed, verdict, verdictType, ignore)
                   VALUES (?,?,?,?,?,?,?,0,?,?,?)""",
                (carrier, flight_number, org, dest, day, dep_time, aircraft_config, verdict, verdict_type, ignore),
            )
            copied_count += 1

    conn.commit()
    return {'copiedCount': copied_count}
