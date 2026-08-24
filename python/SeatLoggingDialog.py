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
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from timezones import et_equivalent_minutes, UnconfirmedAirportError
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


def hhmm_to_12h(hhmm):
    hhmm = int(hhmm)
    h, m = hhmm // 100, hhmm % 100
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


def previous_readings_for(conn, carrier, flight_number, flight_date):
    """
    Every reading logged today for this exact flight, sorted
    most-recent-check first (ascending hoursBeforeDep, since it counts
    down as departure approaches) - same as getPreviousReadingsForRow_.
    """
    rows = conn.execute(
        """SELECT hoursBeforeDep, y, cPlus, firstOrPS, d1 FROM observations
           WHERE readingType='avail' AND carrier=? AND flightNumber=? AND flightDate=?
           ORDER BY hoursBeforeDep ASC""",
        (carrier, flight_number, flight_date),
    ).fetchall()
    return [
        {'hrs': r[0], 'y': r[1], 'cplus': r[2], 'onePS': r[3], 'd1': r[4]}
        for r in rows
    ]


def get_next_batch(conn, skip_route_keys=None):
    skip_set = set(skip_route_keys or [])
    settings = load_settings(conn)
    now = eastern_now()
    dow = now.strftime('%a')  # e.g. 'Tue' - matches loader.py's day_of_week format
    today_str = now.strftime('%Y-%m-%d')
    date_display = now.strftime('%-m/%-d')
    now_minutes = now.hour * 60 + now.minute

    sched_rows = conn.execute(
        """SELECT rowid, carrier, flightNumber, org, dest, depTime, aircraftConfig
           FROM flightSchedule WHERE dayOfWeek = ?""",
        (dow,),
    ).fetchall()

    candidates = []
    unconfirmed_codes = set()
    for rowid, carrier, flight_number, org, dest, dep_time, aircraft_config in sched_rows:
        try:
            dep_et_minutes = et_equivalent_minutes(conn, dep_time, org, now.date())
        except UnconfirmedAirportError:
            unconfirmed_codes.add(org)
            continue

        hours_until_dep = (dep_et_minutes - now_minutes) / 60
        if hours_until_dep * 60 <= DEP_CUTOFF_MINUTES:
            continue

        if settings['logEverything']:
            eligible_now, minutes_until_eligible = True, 0
        else:
            todays_hrs = [
                r[0] for r in conn.execute(
                    """SELECT hoursBeforeDep FROM observations
                       WHERE readingType='avail' AND carrier=? AND flightNumber=? AND flightDate=?
                       AND hoursBeforeDep IS NOT NULL""",
                    (carrier, flight_number, today_str),
                ).fetchall()
            ]
            eligible_now, minutes_until_eligible = evaluate_eligibility(
                hours_until_dep, todays_hrs, settings['tiers']
            )

        candidates.append({
            'scheduleRow': rowid, 'org': org, 'dest': dest, 'car': carrier,
            'dep': dep_time, 'depEtMinutes': dep_et_minutes,
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

    candidates.sort(key=lambda c: c['depEtMinutes'])

    next_candidate = next(
        (c for c in candidates if c['eligibleNow'] and f"{c['org']}|{c['dest']}" not in skip_set),
        None,
    )

    if next_candidate is None:
        future_waits = [c['minutesUntilEligible'] for c in candidates if c['minutesUntilEligible'] is not None]
        wait_minutes = min(future_waits) if future_waits else None
        wait_message = (
            f"Next flight due for a check in {wait_minutes} min."
            if wait_minutes is not None else "Nothing left to check today."
        )
        return {
            'dowDisplay': dow, 'dateDisplay': date_display,
            'flightDate': today_str,
            'routeOrg': None, 'routeDest': None, 'summaryText': '',
            'waitMinutes': wait_minutes, 'waitMessage': wait_message, 'rows': [],
            'aircraftOptions': load_aircraft_options(conn), 'settings': settings,
        }

    d1_map = load_d1_map(conn)
    route_rows = []
    route_departed_count = 0
    for c in candidates:
        if c['org'] != next_candidate['org'] or c['dest'] != next_candidate['dest']:
            continue
        if c['hoursUntilDep'] * 60 <= DEP_CUTOFF_MINUTES:
            route_departed_count += 1
            continue
        route_rows.append({
            'scheduleRow': c['scheduleRow'], 'org': c['org'], 'dest': c['dest'], 'car': c['car'],
            'dep': c['dep'], 'depDisplay': hhmm_to_12h(c['dep']),
            'flightNumber': c['flightNumber'], 'aircraftConfig': c['aircraftConfig'],
            'hasD1': d1_map.get(str(c['aircraftConfig']).lower(), False),
            'hoursUntilDep': round(c['hoursUntilDep'], 1),
            'isNext': c['scheduleRow'] == next_candidate['scheduleRow'],
            'previousReadings': previous_readings_for(conn, c['car'], c['flightNumber'], today_str),
        })

    summary_text = (
        f"{route_departed_count} flight{'s' if route_departed_count != 1 else ''} already left, not shown"
        if route_departed_count > 0 else ''
    )

    return {
        'dowDisplay': dow, 'dateDisplay': date_display,
        'flightDate': today_str,
        'routeOrg': next_candidate['org'], 'routeDest': next_candidate['dest'],
        'summaryText': summary_text, 'waitMinutes': None, 'rows': route_rows,
        'aircraftOptions': load_aircraft_options(conn), 'settings': settings,
    }


def save_entry_dialog(conn, payload):
    """
    payload: {
      flightDate: "YYYY-MM-DD",
      entries: [{ scheduleRow, org, dest, car, dep (HHMM string),
                  aircraftConfig, flightNumber, scheduleEdited (bool),
                  y, cplus, onePS, d1 (each '' or a value as typed) }]
    }
    """
    flight_date = payload['flightDate']

    for entry in payload['entries']:
        if entry.get('scheduleEdited'):
            conn.execute(
                """UPDATE flightSchedule SET depTime=?, aircraftConfig=?, flightNumber=?
                   WHERE rowid=?""",
                (entry['dep'], entry['aircraftConfig'], entry['flightNumber'], entry['scheduleRow']),
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
            dep_et_minutes = et_equivalent_minutes(conn, entry['dep'], entry['org'], now.date())
            hours_before_dep = (dep_et_minutes - (now.hour * 60 + now.minute)) / 60
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
