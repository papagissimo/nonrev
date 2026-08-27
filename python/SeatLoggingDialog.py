"""
SeatLoggingDialog backend: port of Entrydialog.gs.js's getNextBatch /
saveEntryDialog / cadence engine to Python against nonrev.db, replacing
Sheets/PropertiesService.

One real simplification versus the old version: the old sheet had no
reliable flight-number field, so "which readings are for this exact
flight instance today" had to be inferred by fuzzy-matching org/dest/dep
time within +/-30 min. Here, every observation carries a real
flightNumber (assigned from the flightSchedule row it was logged
against), so "today's readings for this flight" is an exact match on
(carrier, flightNumber, flightDate) - no fuzzy matching needed for this
part. depTime itself still deliberately isn't stored on observations
(see schema notes) - it's read from flightSchedule via flightNumber
whenever it's needed for display or hours-until-departure math.

Cross-midnight handling: a flight can still be legitimately "in play"
even after the calendar has rolled over in ET, if its own origin airport
is far enough west - e.g. a 10pm Pacific departure is already 1am ET the
next day. Every batch fetch therefore considers flightSchedule rows for
BOTH today's and yesterday's (ET) day-of-week, each evaluated against
its own real calendar date via timezones.et_equivalent_datetime, and
everything downstream (eligibility, sorting, grouping into "this route's
rows", the flightDate written on save) tracks each candidate's own
correct date rather than assuming a single global "today". Flights that
are simply in the past fall out through the ordinary 45-minute cutoff -
no separate yesterday/today branching is needed once the math is done
in absolute datetimes instead of minutes-since-midnight.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from timezones import et_equivalent_datetime, UnconfirmedAirportError
from settings import load_settings

DEP_CUTOFF_MINUTES = 45
ET_ZONE = ZoneInfo('America/New_York')


def eastern_now():
    return datetime.now(ET_ZONE)


def evaluate_eligibility(hours_until_dep, todays_hrs_list, tiers):
    """
    Direct port of evaluateNextUpEligibility_. Stateless: eligibility is
    recomputed fresh from hoursUntilDep + today's logged hours every call,
    so a flight can never get stuck in a stale tier.
    """
    last_logged_hours = min(todays_hrs_list) if todays_hrs_list else None
    eligible_now = False
    minutes_until_eligible = None

    for tier in tiers:
        if 'targetHours' in tier and 'doneToleranceHours' in tier:
            already_done = any(
                abs(hrs - tier['targetHours']) <= tier['doneToleranceHours']
                for hrs in todays_hrs_list
            )
            if already_done:
                continue  # permanently closed for this flight today

        ceiling = tier['maxHours']
        if last_logged_hours is not None:
            ceiling = min(ceiling, last_logged_hours - tier['recheckGapHours'])

        if tier['minHours'] <= hours_until_dep <= ceiling:
            eligible_now = True
        elif hours_until_dep > ceiling:
            mins = round((hours_until_dep - ceiling) * 60)
            if minutes_until_eligible is None or mins < minutes_until_eligible:
                minutes_until_eligible = mins
        # else hoursUntilDep < tier['minHours']: already past this window today.

    if eligible_now:
        minutes_until_eligible = 0
    return eligible_now, minutes_until_eligible


def minutes_to_12h(dep_minutes):
    h, m = divmod(int(dep_minutes), 60)
    period = 'pm' if h >= 12 else 'am'
    h12 = h % 12 or 12
    return f"{h12}:{m:02d} {period}"


def load_aircraft_options(conn):
    return [r[0] for r in conn.execute(
        "SELECT configKey FROM aircraftConfigs ORDER BY rowid"
    ).fetchall()]


def load_d1_map(conn):
    return {
        row[0].lower(): bool(row[1])
        for row in conn.execute("SELECT configKey, d1 FROM aircraftConfigs").fetchall()
    }


def previous_readings_for(conn, carrier, flight_number, org, dest, flight_date):
    """
    Every reading logged today for this exact flight, sorted
    most-recent-check first (ascending hoursBeforeDep, since it counts
    down as departure approaches) - same as getPreviousReadingsForRow_.
    flight_date is the flight's own schedule date, not necessarily
    "today" in ET (see module docstring).

    Scoped to org/dest as well as carrier/flightNumber - a flight number
    is sometimes reused between the two directions of a route pair (same
    tail, there-and-back) on the same calendar date, and without org/dest
    in the filter this would silently pull the OTHER direction's readings
    in as if they were this flight's own history.
    """
    rows = conn.execute(
        """SELECT hoursBeforeDep, y, cPlus, firstOrPS, d1 FROM observations
           WHERE readingType='avail' AND carrier=? AND flightNumber=? AND org=? AND dest=? AND flightDate=?
           ORDER BY hoursBeforeDep ASC""",
        (carrier, flight_number, org, dest, flight_date),
    ).fetchall()
    return [
        {'hrs': r[0], 'y': r[1], 'cplus': r[2], 'onePS': r[3], 'd1': r[4]}
        for r in rows
    ]


def recent_observations(conn, limit=9):
    """
    The last N real logged observations, globally (any route/flight),
    for seeding the recent-readings panel on page load - it otherwise
    has no memory of anything before the current browser session.
    Returned oldest-of-the-batch first / most-recent last, matching the
    client's own recentlyLogged accumulation order. Deliberately doesn't
    carry flightNumber - his call, not wanted in this particular view.
    """
    rows = conn.execute(
        """SELECT org, dest, hoursBeforeDep, y, cPlus, firstOrPS, d1
           FROM observations WHERE readingType='avail'
           ORDER BY checkTimestamp DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    result = [
        {'org': org, 'dest': dest, 'hrs': hrs, 'y': y, 'cplus': cplus, 'onePS': ps, 'd1': d1}
        for org, dest, hrs, y, cplus, ps, d1 in rows
    ]
    return list(reversed(result))


def get_flight_day_flag(conn, carrier, flight_number, org, dest, flight_date):
    row = conn.execute(
        """SELECT flag FROM flightDayFlag
           WHERE carrier=? AND flightNumber=? AND org=? AND dest=? AND flightDate=?""",
        (carrier, flight_number, org, dest, flight_date),
    ).fetchone()
    return row[0] if row else ''


def get_route_day_flag(conn, carrier, org, dest, flight_date):
    row = conn.execute(
        """SELECT flag FROM routeDayFlag
           WHERE carrier=? AND org=? AND dest=? AND flightDate=?""",
        (carrier, org, dest, flight_date),
    ).fetchone()
    return row[0] if row else ''


def save_flight_day_flag(conn, carrier, flight_number, org, dest, flight_date, flag_text):
    conn.execute(
        """INSERT INTO flightDayFlag (carrier, flightNumber, org, dest, flightDate, flag)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(carrier, flightNumber, org, dest, flightDate)
           DO UPDATE SET flag=excluded.flag""",
        (carrier, flight_number, org, dest, flight_date, flag_text),
    )
    conn.commit()
    return {'saved': True}


def save_route_day_flag(conn, carrier, org, dest, flight_date, flag_text):
    conn.execute(
        """INSERT INTO routeDayFlag (carrier, org, dest, flightDate, flag)
           VALUES (?,?,?,?,?)
           ON CONFLICT(carrier, org, dest, flightDate)
           DO UPDATE SET flag=excluded.flag""",
        (carrier, org, dest, flight_date, flag_text),
    )
    conn.commit()
    return {'saved': True}


def get_next_batch(conn, skip_route_keys=None):
    skip_set = set(skip_route_keys or [])
    settings = load_settings(conn)
    now = eastern_now()

    # Consider both today's and yesterday's (ET) day-of-week schedule
    # rows - see module docstring. Each is evaluated against its own
    # real calendar date, so a flight that's already departed just falls
    # out through the normal 45-minute cutoff below, same as always.
    schedule_days = [now.date(), now.date() - timedelta(days=1)]

    candidates = []
    unconfirmed_codes = set()
    departed_by_route = {}  # (org, dest, flightDate) -> count - see summary_text below
    for schedule_date in schedule_days:
        dow = schedule_date.strftime('%a')
        flight_date_str = schedule_date.isoformat()

        sched_rows = conn.execute(
            """SELECT rowid, carrier, flightNumber, org, dest, depTime, aircraftConfig
               FROM flightSchedule WHERE dayOfWeek = ? AND ignore = 0""",
            (dow,),
        ).fetchall()

        for rowid, carrier, flight_number, org, dest, dep_time, aircraft_config in sched_rows:
            try:
                dep_dt = et_equivalent_datetime(conn, dep_time, org, schedule_date)
            except UnconfirmedAirportError:
                unconfirmed_codes.add(org)
                continue

            hours_until_dep = (dep_dt - now).total_seconds() / 3600
            if hours_until_dep * 60 <= DEP_CUTOFF_MINUTES:
                # Departed (or within the no-longer-offered window). This is
                # the only place that's actually true - a candidate that
                # makes it past this line can never trip this same test
                # again later, so counting it anywhere downstream (as the
                # old per-route loop tried to) can never find anything.
                # Per his call: back to per-route scope (not whole-day) -
                # bucketed here, by the route+date it actually belongs to,
                # so it's still counted at the only place it's real.
                route_key = (org, dest, flight_date_str)
                departed_by_route[route_key] = departed_by_route.get(route_key, 0) + 1
                continue

            if settings['logEverything']:
                eligible_now, minutes_until_eligible = True, 0
            else:
                todays_hrs = [
                    r[0] for r in conn.execute(
                        """SELECT hoursBeforeDep FROM observations
                           WHERE readingType='avail' AND carrier=? AND flightNumber=? AND org=? AND dest=? AND flightDate=?
                           AND hoursBeforeDep IS NOT NULL""",
                        (carrier, flight_number, org, dest, flight_date_str),
                    ).fetchall()
                ]
                eligible_now, minutes_until_eligible = evaluate_eligibility(
                    hours_until_dep, todays_hrs, settings['tiers']
                )

            candidates.append({
                'scheduleRow': rowid, 'org': org, 'dest': dest, 'car': carrier,
                'dep': dep_time, 'depEtDatetime': dep_dt,
                'flightDate': flight_date_str, 'dow': dow,
                'aircraftConfig': aircraft_config or 'TBD', 'flightNumber': flight_number or '',
                'hoursUntilDep': hours_until_dep,
                'eligibleNow': eligible_now, 'minutesUntilEligible': minutes_until_eligible,
            })

    if unconfirmed_codes:
        codes = ', '.join(sorted(unconfirmed_codes))
        raise UnconfirmedAirportError(
            f"Can't compute departure times - these airport code(s) aren't confirmed yet: "
            f"{codes}. Run `python confirm_airports.py`, then try again."
        )

    candidates.sort(key=lambda c: c['depEtDatetime'])

    # Skip-key format stays org|dest (no date) to match the client's
    # existing session-skip list - skipping a route mid-session skips it
    # regardless of which calendar day it's currently grouped under,
    # which is the right behavior (the person thinks of it as "that
    # route", not "that route on that specific date").
    next_candidate = next(
        (c for c in candidates
         if c['eligibleNow'] and f"{c['org']}|{c['dest']}" not in skip_set),
        None,
    )

    if next_candidate is None:
        total_departed = sum(departed_by_route.values())
        summary_text = (
            f"{total_departed} flight{'s' if total_departed != 1 else ''} already left, not shown"
            if total_departed > 0 else ''
        )
        future_waits = [c['minutesUntilEligible'] for c in candidates if c['minutesUntilEligible'] is not None]
        wait_minutes = min(future_waits) if future_waits else None
        wait_message = (
            f"Next flight due for a check in {wait_minutes} min."
            if wait_minutes is not None else "Nothing left to check today."
        )
        return {
            'dowDisplay': now.strftime('%a'), 'dateDisplay': now.strftime('%b %-d'),
            'flightDate': now.date().isoformat(),
            'routeOrg': None, 'routeDest': None, 'summaryText': summary_text,
            'waitMinutes': wait_minutes, 'waitMessage': wait_message, 'rows': [],
            'aircraftOptions': load_aircraft_options(conn), 'settings': settings,
            'recentObservations': recent_observations(conn),
        }

    d1_map = load_d1_map(conn)
    route_rows = []
    for c in candidates:
        # Same route AND same schedule date - two different calendar
        # days' worth of the same org/dest must never be shown together
        # in one batch, or per-row flightDate would be ambiguous.
        if (c['org'] != next_candidate['org'] or c['dest'] != next_candidate['dest']
                or c['flightDate'] != next_candidate['flightDate']):
            continue
        # Note: a candidate reaching this loop already cleared the
        # DEP_CUTOFF_MINUTES check above, by construction - nothing here
        # can still be departed. (departed_by_route, computed above, is
        # the one and only place that count is real.)
        route_rows.append({
            'scheduleRow': c['scheduleRow'], 'org': c['org'], 'dest': c['dest'], 'car': c['car'],
            'dep': c['dep'], 'depDisplay': minutes_to_12h(c['dep']),
            'flightNumber': c['flightNumber'], 'aircraftConfig': c['aircraftConfig'],
            'hasD1': d1_map.get(str(c['aircraftConfig']).lower(), False),
            'hoursUntilDep': round(c['hoursUntilDep'], 1),
            'isNext': c['scheduleRow'] == next_candidate['scheduleRow'],
            'previousReadings': previous_readings_for(conn, c['car'], c['flightNumber'], c['org'], c['dest'], c['flightDate']),
            'flag': get_flight_day_flag(conn, c['car'], c['flightNumber'], c['org'], c['dest'], c['flightDate']),
        })

    route_flag = get_route_day_flag(
        conn, next_candidate['car'], next_candidate['org'], next_candidate['dest'], next_candidate['flightDate'],
    )

    route_key = (next_candidate['org'], next_candidate['dest'], next_candidate['flightDate'])
    route_departed_count = departed_by_route.get(route_key, 0)
    summary_text = (
        f"{route_departed_count} flight{'s' if route_departed_count != 1 else ''} already left, not shown"
        if route_departed_count > 0 else ''
    )

    next_date = datetime.strptime(next_candidate['flightDate'], '%Y-%m-%d').date()
    return {
        'dowDisplay': next_candidate['dow'], 'dateDisplay': next_date.strftime('%b %-d'),
        'flightDate': next_candidate['flightDate'],
        'routeOrg': next_candidate['org'], 'routeDest': next_candidate['dest'],
        'carrier': next_candidate['car'], 'routeFlag': route_flag,
        'summaryText': summary_text, 'waitMinutes': None, 'rows': route_rows,
        'aircraftOptions': load_aircraft_options(conn), 'settings': settings,
        'recentObservations': recent_observations(conn),
    }


def save_entry_dialog(conn, payload):
    """
    payload: {
      flightDate: "YYYY-MM-DD",   # the flight's own schedule date - may
                                   # be "yesterday" per the cross-midnight
                                   # handling above, not always ET-today
      entries: [{ scheduleRow, org, dest, car, dep (HHMM string),
                  aircraftConfig, flightNumber, scheduleEdited (bool),
                  ignore (bool),
                  y, cplus, onePS, d1 (each '' or a value as typed) }]
    }
    """
    flight_date = payload['flightDate']
    flight_date_obj = datetime.strptime(flight_date, '%Y-%m-%d').date()

    for entry in payload['entries']:
        if entry.get('scheduleEdited'):
            # Local import to dodge a circular import - FlightScheduleDialog
            # already imports minutes_to_12h from this module, so importing
            # back at module load time would deadlock; a function-local
            # import only resolves once both modules have finished loading.
            from FlightScheduleDialog import (
                cascade_flight_number_rename, normalize_dow, recompute_hours_before_dep,
            )

            old_row = conn.execute(
                "SELECT carrier, flightNumber, depTime FROM flightSchedule WHERE rowid=?",
                (entry['scheduleRow'],),
            ).fetchone()
            if old_row is not None:
                old_carrier, old_flight_number, old_dep_time = old_row
                dow = normalize_dow(flight_date_obj.strftime('%a'))
                cascade_flight_number_rename(
                    conn, entry['org'], entry['dest'], dow,
                    old_carrier, old_flight_number,
                    old_carrier, entry['flightNumber'],  # this backdoor never edits carrier
                )

            conn.execute(
                """UPDATE flightSchedule SET depTime=?, aircraftConfig=?, flightNumber=?, ignore=?
                   WHERE rowid=?""",
                (entry['dep'], entry['aircraftConfig'], entry['flightNumber'],
                 1 if entry.get('ignore') else 0, entry['scheduleRow']),
            )

            # Same stale-hoursBeforeDep fix as FlightScheduleDialog's save
            # path - this pencil-edit is the other place depTime/flightNumber
            # get corrected, so it needs the same recompute.
            if old_row is not None:
                dep_changed = old_dep_time != entry['dep']
                flight_changed = old_flight_number != entry['flightNumber']
                if dep_changed or flight_changed:
                    recompute_hours_before_dep(
                        conn, old_carrier, entry['flightNumber'],
                        entry['org'], entry['dest'], dow, entry['dep'],
                    )

    to_write = [
        e for e in payload['entries']
        if any(e.get(f, '') not in ('', None) for f in ('y', 'cplus', 'onePS', 'd1'))
    ]
    if not to_write:
        conn.commit()
        return {'logged': 0}

    now = eastern_now()
    check_timestamp = now.strftime('%Y-%m-%d %H:%M')

    for entry in to_write:
        def num(field):
            v = entry.get(field, '')
            return None if v in ('', None) else float(v)

        try:
            # flight_date_obj (the flight's own schedule date), not
            # now.date() - a "yesterday" West Coast flight's departure
            # time is only correct when computed against its own actual
            # calendar date.
            dep_dt = et_equivalent_datetime(conn, entry['dep'], entry['org'], flight_date_obj)
            hours_before_dep = (dep_dt - now).total_seconds() / 3600
        except UnconfirmedAirportError as e:
            print(f"Warning: couldn't compute hoursBeforeDep for logged entry ({entry['org']}->{entry['dest']}): {e}")
            hours_before_dep = None

        conn.execute(
            """INSERT INTO observations
               (carrier, flightNumber, org, dest, flightDate, checkTimestamp,
                hoursBeforeDep, readingType, y, cPlus, firstOrPS, d1)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (entry['car'], entry['flightNumber'], entry['org'], entry['dest'],
             flight_date, check_timestamp, hours_before_dep, 'avail',
             num('y'), num('cplus'), num('onePS'), num('d1')),
        )

    conn.commit()
    return {'logged': len(to_write)}


def save_and_get_next_batch(conn, payload, skip_route_keys=None):
    save_result = save_entry_dialog(conn, payload)
    next_result = get_next_batch(conn, skip_route_keys)
    return {'logged': save_result['logged'], 'next': next_result}
