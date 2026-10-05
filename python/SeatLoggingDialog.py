"""
SeatLoggingDialog backend: port of Entrydialog.gs.js's getNextBatch /
saveEntryDialog / cadence engine to Python against nonrev.db, replacing
Sheets/PropertiesService.

Flight identity: a real Delta flight number is not a stable identifier
(see domainKnowledge.md / flightSchedule's carriersFltNum_notStable_
DO_NOT_USE column) - never matched on, anywhere. "Today's readings for
this flight" is an exact match on (carrier, org, dest, depTime,
flightDate) for any date OTHER than today: depTime is captured directly
on each observation at logging time and never read back from
flightSchedule afterward, so once flightDate is in the past that row's
own recorded time can never go stale - flightSchedule itself only ever
holds THIS WEEK's values anyway (see its CREATE TABLE comment in
create_db.py), so there'd be nothing meaningful to compare an old row
against even if we wanted to.

For flightDate == today specifically, exact depTime match is WRONG
(settled 2026-09-18, real bug, not a hypothetical): a same-day schedule
correction changes flightSchedule's depTime while every reading already
logged under the old value keeps its own old depTime forever, so exact
match would silently drop them from view the moment the correction
lands, same day, same flight. previous_readings_for instead clusters
today's own readings together with the current depTime (clustering.
cluster_services, 60-min gap) and recomputes each kept reading's
displayed hours-before-dep from its own checkTimestamp against the
CURRENT depTime - safe specifically because it's the same calendar day,
same actual departure, not a claim that spans across weeks (that
broader claim is false - see flightSchedule's comment).

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

import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from timezones import et_equivalent_datetime, UnconfirmedAirportError
from clustering import cluster_services
from deptime_convergence import converge_flight_date
from settings import load_settings, DECLINE_CURVE_SETTINGS_KEY, DEFAULT_DECLINE_CURVE_SETTINGS
from ServiceGrouping import load_open_full_settings
from Pools import snapshot as pool_snapshot
from TrustPools import cadence_reading, next_now_hours, snapshot as trust_snapshot
from DeclineCurveFit import piecewise_model, effective_hours_between, slide_c1_through_readings
from DeclineCurveHierarchy import resolve_coefficients
from T1Estimator import compute_t1_replay_column, held_t1_replay_column, CABIN_KEY_TO_COLUMN
from Scenarios import studied_cells
from Grading import FlightGrades, load_grading_settings, settings_form
from AircraftConfigs import load_aircraft_list
from observation_filters import SEAT_MAP_COLUMNS, not_seat_map_only_where_clause
from FloorEstimates import load_floor_estimates, seats_from_glance
import InsiderReadings

DEP_CUTOFF_MINUTES = 45
ET_ZONE = ZoneInfo('America/New_York')


def eastern_now():
    return datetime.now(ET_ZONE)


def minutes_to_12h(dep_minutes):
    h, m = divmod(int(dep_minutes), 60)
    period = 'pm' if h >= 12 else 'am'
    h12 = h % 12 or 12
    return f"{h12}:{m:02d} {period}"


# Every field a logging entry can carry, entry key -> observations column.
# One list drives the save gate (an entry with any of these non-blank is
# logged) and is sent to the page so its own "is there anything to log"
# check reads the same list. Adding a field means adding it here and giving
# it an input in SeatLoggingDialog.html.
CAN_BUY_ENTRY_COLUMNS = {
    'y': 'y', 'cplus': 'cPlus', 'onePS': 'firstOrPS', 'd1': 'd1',
}
GLANCE_ENTRY_COLUMNS = {
    'glanceY': 'cheapY', 'glanceCplus': 'cheapCPlus', 'glanceOnePS': 'cheapFirstOrPS', 'glanceD1': 'cheapD1',
}
GLANCE_ACTUAL_FIELD = {'glanceY': 'y', 'glanceCplus': 'cplus', 'glanceOnePS': 'onePS', 'glanceD1': 'd1'}
SEAT_MAP_ENTRY_COLUMNS = {column: column for column in SEAT_MAP_COLUMNS}
ENTRY_COLUMNS = {**CAN_BUY_ENTRY_COLUMNS, **GLANCE_ENTRY_COLUMNS, **SEAT_MAP_ENTRY_COLUMNS}


def entry_field_config():
    return {
        'canBuyFields': list(CAN_BUY_ENTRY_COLUMNS),
        'glanceActualField': GLANCE_ACTUAL_FIELD,
        'seatMapFields': list(SEAT_MAP_ENTRY_COLUMNS),
        'insiderFields': InsiderReadings.NUMBER_FIELDS,
        'insiderVerdicts': InsiderReadings.VERDICTS,
    }


def load_d1_map(conn):
    return {
        row[0].lower(): bool(row[1])
        for row in conn.execute("SELECT configKey, d1 FROM aircraftConfigs").fetchall()
    }


def resolved_coefficients_for_row(conn, org, dest, dow, dep_time):
    """Per-cabin resolved coefficients for display next to the curve
    estimate hint - c1 (hours), gap (hours: the daytime-rate 9-to-0
    crossing duration, 9/slope - same "gap" DeclineCurveFit's own
    console report and DeclineCurveDialog already show; a display-only
    derived number, not persisted or used in any live calculation
    here), and nightRatio. Whichever tier each quantity actually
    resolves from (same hierarchy the curve estimate itself already
    uses) - None for a quantity with nothing resolvable at all
    (practically only possible if the global default itself is
    incomplete for that cabin)."""
    result = {}
    for cabin_key, cabin_col in CABIN_KEY_TO_COLUMN.items():
        resolved = resolve_coefficients(conn, org, dest, dow, dep_time, cabin_col)
        slope = resolved.get('slope')
        result[cabin_key] = {
            'c1': resolved.get('c1'),
            'gap': (9.0 / slope) if slope else None,
            'nightRatio': resolved.get('nightRatio'),
        }
    return result


def curve_estimates_for_row(conn, org, dest, dow, dep_time, hours_until_dep, departure_dt,
                             night_start_hour, night_end_hour, today_c1_by_cabin=None):
    """The decline curve's predicted seat count per cabin at this exact
    hoursUntilDep. His call (2026-09-15): anchors off today's own
    readings when there are any - today_c1_by_cabin (from
    previous_readings_for) is each cabin's C1 as of the most recent
    reading actually logged today, already slid to stay consistent with
    that history (see DeclineCurveFit.slide_c1_through_readings) - so
    the hint can't contradict a reading that's already on the board the
    way it used to (a real gap, not a design choice: this used to always
    show the plain pooled curve regardless of today's evidence, which
    could show a rising hint after a lower actual reading was already
    logged).

    Falls back to the plain pooled C1 for any cabin with nothing logged
    yet today, or when today_c1_by_cabin isn't supplied at all - the
    coefficients hierarchy always resolves SOMETHING (falls all the way
    back to the global default if nothing more specific exists yet - see
    DeclineCurveHierarchy), so this always has a number to show, even
    for a flight that's never been logged before - "the estimator has an
    estimate for everything... don't hide it from me."

    Returns dict cabin_key ('y'/'cplus'/'onePS'/'d1') -> one-decimal
    float, one entry per cabin whose coefficients actually resolved
    (practically always all four, given the global default, but not
    assumed - a cabin can still come back missing if a hand-set override
    somewhere leaves a NULL with nothing beneath it)."""
    if departure_dt is not None and departure_dt.tzinfo is not None:
        # et_equivalent_datetime returns tz-aware ET; DeclineCurveFit's
        # day/night math works in naive local time throughout (same
        # convention its own departure_dt construction from flightDate +
        # depTime already uses) - strip it here rather than pushing this
        # detail onto every caller.
        departure_dt = departure_dt.replace(tzinfo=None)

    today_c1_by_cabin = today_c1_by_cabin or {}
    result = {}
    for cabin_key, cabin_col in CABIN_KEY_TO_COLUMN.items():
        resolved = resolve_coefficients(conn, org, dest, dow, dep_time, cabin_col)
        if resolved['slope'] is None or resolved['c1'] is None:
            continue
        c1 = today_c1_by_cabin.get(cabin_key, resolved['c1'])
        night_ratio = resolved.get('nightRatio') or 1.0
        val = piecewise_model(hours_until_dep, c1, resolved['slope'],
                               night_ratio, departure_dt, night_start_hour, night_end_hour)
        result[cabin_key] = round(float(val), 1)
    return result


def previous_readings_for(conn, carrier, dep_time, org, dest, flight_date, floor_estimates):
    """
    Every reading logged today for this exact flight, sorted
    most-recent-check first (ascending hoursBeforeDep, since it counts
    down as departure approaches) - same as getPreviousReadingsForRow_.
    flight_date is the flight's own schedule date, not necessarily
    "today" in ET (see module docstring).

    Matched on (carrier, org, dest, depTime, flightDate) for any date
    OTHER than today - never flightNumber (see module docstring). Scoped
    to org/dest as well as depTime - two different routes can share a
    depTime by coincidence, and without org/dest in the filter this
    would silently pull the OTHER route's readings in as if they were
    this flight's own history.

    For flightDate == today, depTime match is by clustering instead of
    exact equality (see module docstring for why), and each kept
    reading's 'hrs' is recomputed live from its own checkTimestamp
    against the CURRENT depTime rather than trusting the value stored
    at logging time - the one place in this file a reading's displayed
    hours-before-dep can differ from what's in the observations table.

    Each cabin value is the real actual if one was logged, else the
    glance's seat count (FloorEstimates.seats_from_glance) if one was
    glanced, else None. 'estimated' lists the cabins whose value came from
    a glance floor rather than an exact count.

    A reading with only seat-map numbers (no can-buy counts) is included
    as a row of its own: its can-buy cabins are None, its 't1' is None,
    and it gets no 'steps'/'c1After' entries. Each row carries 'solo'
    (cabin_key -> solo available-to-select count) and 'blocked' (the X
    count), None wherever nothing was observed.

    Each row also carries 'insider': that sitting's insider reading
    (InsiderReadings.NUMBER_FIELDS plus 'verdict'), or None. An insider
    reading with no observation at the same checkTimestamp is a row of its
    own, every can-buy and seat-map value None.

    Each row also carries 't1': the live T1 estimate as of that point in
    the day (see T1Estimator.compute_t1_replay_column) - the ONE T1 value
    shown anywhere in the dialog (his call - he
    never wants two different T1 numbers displayed side by side), computed
    server-side rather than in the browser (his call - no good reason for
    real calculation to live client-side). None where no estimate is
    resolvable yet for that row (see T1Estimator's docstring for when
    that happens) - the client renders that as a blank cell, same as any
    other missing value. 'held' is the same row's held total before the
    typical move is added (T1Estimator.held_t1_replay_column); the trust
    pool's T1 range is built around it, since the range's own moves already
    include the drift.

    Each row also carries 'steps': a dict cabin_key -> integer step
    change, one per cabin - his design (2026-09-15), see
    DeclineCurveFit.slide_c1_through_readings for the exact mechanism:
    each cabin's C1 starts at the service's pooled value and slides as
    needed to stay consistent with that cabin's own readings so far
    today, in order. An interior reading always pins C1 exactly (the
    step is however far the PRE-update C1 was predicting). A rail
    reading (9 or 0) only bounds C1 - if the current running C1 is
    already consistent with it, the step is 0 regardless of how long
    the gap since the last reading was; only an inconsistent rail forces
    a slide, and only in the one direction that resolves it (a 9 can
    only pull C1 down, a 0 can only push it up). Positive means more
    seats than the running curve predicted (his example: a
    cancellation); negative means fewer. A cabin's first reading of the
    day still gets a real step (measured against the pooled C1, not
    against nothing) - only a genuinely unresolvable slope leaves that
    cabin's entry out of the dict entirely, for every row of that cabin.

    Each row also carries 'c1After': the same cabin's running C1
    immediately after that reading's slide was applied (2026-09-16,
    his ask - the display column showing this sits to the left of
    Steps) - already computed by slide_c1_through_readings as part of
    producing 'steps' above, just not previously kept. Missing for a
    cabin/row exactly where 'steps' is also missing for it (no
    departure_dt, or nothing logged for that cabin that day).

    Returns (readings, today_c1_by_cabin) - the second element is each
    cabin's running C1 after folding in every reading logged so far
    today (or the plain pooled C1 for a cabin with nothing logged yet) -
    feed this to curve_estimates_for_row to make the hint agree with
    today's own evidence instead of ignoring it (see that function's
    docstring for why it used to ignore it and why that was a real gap,
    not by design past today).
    """
    is_today = flight_date == eastern_now().strftime('%Y-%m-%d')
    flight_date_obj = datetime.strptime(flight_date, '%Y-%m-%d').date()
    insider_rows = InsiderReadings.readings_for_route_day(conn, carrier, org, dest, flight_date)

    if is_today:
        rows = conn.execute(
            f"""SELECT hoursBeforeDep, checkTimestamp, y, cPlus, firstOrPS, d1,
                      soloY, soloCPlus, soloFirstOrPS, soloD1, blockedTotal,
                      cheapY, cheapCPlus, cheapFirstOrPS, cheapD1, depTime
               FROM observations
               WHERE carrier=? AND org=? AND dest=? AND flightDate=?""",
            (carrier, org, dest, flight_date),
        ).fetchall()

        # Cluster today's own logged depTimes together with the CURRENT
        # depTime so a same-day schedule correction can't silently drop
        # readings logged under the old value - see module docstring.
        clusters = cluster_services([r[-1] for r in rows] + [ins['depTime'] for ins in insider_rows] + [dep_time])
        this_flights_cluster = next(c for c in clusters if dep_time in c)
        current_dep_dt = et_equivalent_datetime(conn, dep_time, org, flight_date_obj)

        def live_hours(check_ts):
            check_dt = datetime.strptime(check_ts, '%Y-%m-%d %H:%M').replace(tzinfo=current_dep_dt.tzinfo)
            return round((current_dep_dt - check_dt).total_seconds() / 3600, 2)

        readings_raw = [(live_hours(r[1]), *r[1:-1]) for r in rows if r[-1] in this_flights_cluster]
        insider_kept = [(live_hours(ins['checkTimestamp']), ins)
                        for ins in insider_rows if ins['depTime'] in this_flights_cluster]
    else:
        readings_raw = conn.execute(
            f"""SELECT hoursBeforeDep, checkTimestamp, y, cPlus, firstOrPS, d1,
                      soloY, soloCPlus, soloFirstOrPS, soloD1, blockedTotal,
                      cheapY, cheapCPlus, cheapFirstOrPS, cheapD1
               FROM observations
               WHERE carrier=? AND depTime=? AND org=? AND dest=? AND flightDate=?""",
            (carrier, dep_time, org, dest, flight_date),
        ).fetchall()
        flight_insider = [ins for ins in insider_rows if ins['depTime'] == dep_time]
        try:
            dep_dt = et_equivalent_datetime(conn, dep_time, org, flight_date_obj)
            insider_kept = [
                (round((dep_dt - datetime.strptime(ins['checkTimestamp'], '%Y-%m-%d %H:%M')
                        .replace(tzinfo=dep_dt.tzinfo)).total_seconds() / 3600, 2), ins)
                for ins in flight_insider
            ]
        except UnconfirmedAirportError as e:
            print(f"Warning: couldn't place insider readings for {org}->{dest}: {e}")
            insider_kept = []

    def blank_reading(hrs):
        return {'hrs': hrs, 'solo': {'y': None, 'cplus': None, 'onePS': None, 'd1': None},
                'blocked': None, 'estimated': [], 'insider': None,
                **{cabin_key: None for cabin_key in CABIN_KEY_TO_COLUMN}}

    readings = []
    reading_by_check = {}
    for r in readings_raw:
        reading = blank_reading(r[0])
        reading['solo'] = {'y': r[6], 'cplus': r[7], 'onePS': r[8], 'd1': r[9]}
        reading['blocked'] = r[10]
        for cabin_key, actual, glance in zip(CABIN_KEY_TO_COLUMN, r[2:6], r[11:15]):
            if actual is None and glance is not None:
                actual = seats_from_glance(floor_estimates, CABIN_KEY_TO_COLUMN[cabin_key], glance)
                if glance not in (0, 9):
                    reading['estimated'].append(cabin_key)
            reading[cabin_key] = actual
        readings.append(reading)
        reading_by_check.setdefault(r[1], reading)

    # An insider reading submitted in the same sitting as an observation
    # shares its checkTimestamp and joins that row; otherwise it's a row
    # of its own, like a seat-map-only reading.
    for hrs, ins in insider_kept:
        values = {f: ins[f] for f in InsiderReadings.NUMBER_FIELDS + ['verdict']}
        host = reading_by_check.get(ins['checkTimestamp'])
        if host is not None and host['insider'] is None:
            host['insider'] = values
        else:
            reading = blank_reading(hrs)
            reading['insider'] = values
            readings.append(reading)

    readings.sort(key=lambda rd: (rd['hrs'] is None, rd['hrs'] if rd['hrs'] is not None else 0))
    held_column = held_t1_replay_column(readings)
    t1_column = compute_t1_replay_column(conn, org, dest, flight_date, dep_time, readings)
    for reading, held, t1 in zip(readings, held_column, t1_column):
        has_can_buy = any(reading[cabin_key] is not None for cabin_key in CABIN_KEY_TO_COLUMN)
        reading['held'] = held if has_can_buy else None
        reading['t1'] = t1 if has_can_buy else None

    day_of_week = datetime.strptime(flight_date, "%Y-%m-%d").strftime("%a")
    decline_settings = load_settings(conn, key=DECLINE_CURVE_SETTINGS_KEY, defaults=DEFAULT_DECLINE_CURVE_SETTINGS)
    night_start_hour = decline_settings['nightStartHour']
    night_end_hour = decline_settings['nightEndHour']
    try:
        # dep_time is minutes-since-midnight ORIGIN-local (see
        # et_equivalent_datetime) - NOT HHMM digits, a bug that lived
        # here from 2026-09-14 to 2026-09-15. ET, not origin-local,
        # because that's the zone checkTimestamp is actually logged in
        # (eastern_now()) - what the night-ratio default was calibrated
        # against.
        flight_date_obj = datetime.strptime(flight_date, "%Y-%m-%d").date()
        departure_dt = et_equivalent_datetime(conn, dep_time, org, flight_date_obj).replace(tzinfo=None)
    except (ValueError, TypeError, UnconfirmedAirportError):
        departure_dt = None

    coeffs_by_cabin = {}
    for cabin_key, cabin_col in CABIN_KEY_TO_COLUMN.items():
        resolved = resolve_coefficients(conn, org, dest, day_of_week, dep_time, cabin_col)
        if resolved['slope'] and resolved['c1'] is not None:
            coeffs_by_cabin[cabin_key] = (resolved['c1'], resolved['slope'], resolved.get('nightRatio') or 1.0)

    for reading in readings:
        reading['steps'] = {}
        reading['c1After'] = {}
    today_c1_by_cabin = {}

    if departure_dt is not None:
        for cabin_key, (pooled_c1, slope, night_ratio) in coeffs_by_cabin.items():
            # readings is newest-first; slide_c1_through_readings needs
            # chronological order, and needs to know each pair's
            # original index to write its step back to the right row.
            pairs = [
                (idx, readings[idx]['hrs'], readings[idx][cabin_key])
                for idx in range(len(readings) - 1, -1, -1)
                if readings[idx][cabin_key] is not None
            ]
            if not pairs:
                today_c1_by_cabin[cabin_key] = pooled_c1
                continue
            slid = slide_c1_through_readings(
                [(hrs, val) for _, hrs, val in pairs], pooled_c1, slope, night_ratio,
                departure_dt, night_start_hour, night_end_hour,
            )
            for (idx, _, _), (_, step, c1_after) in zip(pairs, slid):
                readings[idx]['steps'][cabin_key] = step
                readings[idx]['c1After'][cabin_key] = c1_after
            today_c1_by_cabin[cabin_key] = slid[-1][2]  # c1_after of the most recent reading
    else:
        for cabin_key, (pooled_c1, _, _) in coeffs_by_cabin.items():
            today_c1_by_cabin[cabin_key] = pooled_c1

    return readings, today_c1_by_cabin


def recent_observations(conn, limit=9):
    """
    The last N real logged observations, globally (any route/flight),
    for seeding the recent-readings panel on page load - it otherwise
    has no memory of anything before the current browser session.
    Returned oldest-of-the-batch first / most-recent last, matching the
    client's own recentlyLogged accumulation order.

    depTime is read straight off the observation row (each one carries
    its own snapshot from logging time) - no flightSchedule lookup
    needed, so this can never go blank due to a since-renamed/changed
    schedule row the way a join-based lookup could.
    """
    rows = conn.execute(
        f"""SELECT org, dest, hoursBeforeDep, y, cPlus, firstOrPS, d1, depTime
           FROM observations
           WHERE {not_seat_map_only_where_clause()}
           ORDER BY checkTimestamp DESC LIMIT ?""",
        (limit,),
    ).fetchall()

    result = []
    for org, dest, hrs, y, cplus, ps, d1, dep_time in rows:
        dep_display = minutes_to_12h(dep_time) if dep_time is not None else ''
        result.append({
            'org': org, 'dest': dest, 'depDisplay': dep_display,
            'hrs': hrs, 'y': y, 'cplus': cplus, 'onePS': ps, 'd1': d1,
        })
    return list(reversed(result))


def get_flight_day_flag(conn, carrier, dep_time, org, dest, flight_date):
    row = conn.execute(
        """SELECT flag FROM flightDayFlag
           WHERE carrier=? AND depTime=? AND org=? AND dest=? AND flightDate=?""",
        (carrier, dep_time, org, dest, flight_date),
    ).fetchone()
    return row[0] if row else ''


def get_route_day_flag(conn, carrier, org, dest, flight_date):
    row = conn.execute(
        """SELECT flag FROM routeDayFlag
           WHERE carrier=? AND org=? AND dest=? AND flightDate=?""",
        (carrier, org, dest, flight_date),
    ).fetchone()
    return row[0] if row else ''


def save_flight_day_flag(conn, carrier, dep_time, org, dest, flight_date, flag_text):
    conn.execute(
        """INSERT INTO flightDayFlag (carrier, depTime, org, dest, flightDate, flag)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(carrier, org, dest, depTime, flightDate)
           DO UPDATE SET flag=excluded.flag""",
        (carrier, dep_time, org, dest, flight_date, flag_text),
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


def cadence_reading_hours(prev_readings):
    return [reading['hrs'] for reading in prev_readings
            if reading.get('hrs') is not None
            and any(reading.get(cabin) is not None for cabin in CABIN_KEY_TO_COLUMN)]


def pool_and_cadence_reading(conn, trust, settings, candidate, prev_readings, last_chance_hours):
    pool = trust.pool_record(conn, candidate['org'], candidate['dest'], candidate['dow'], candidate['dep'])
    state, urgency = cadence_reading(cadence_reading_hours(prev_readings), candidate['hoursUntilDep'],
                                     settings['goldenTicketHours'], pool['curve'] if pool else None,
                                     settings['cadenceNowPoints'], settings['cadenceSkipPoints'],
                                     last_chance_hours)
    return pool, state, urgency


def pool_and_cadence(conn, trust, settings, candidate, prev_readings, last_chance_hours, now):
    pool, state, _ = pool_and_cadence_reading(conn, trust, settings, candidate, prev_readings, last_chance_hours)
    next_hours = next_now_hours(cadence_reading_hours(prev_readings), candidate['hoursUntilDep'],
                                settings['goldenTicketHours'], pool['curve'] if pool else None,
                                settings['cadenceNowPoints'])
    next_reading = None
    if state == 'last':
        next_reading = {'label': 'last rdg', 'note': 'full count'}
    elif next_hours is not None and next_hours >= candidate['hoursUntilDep']:
        next_reading = {'label': 'next rdg', 'note': 'now'}
    elif next_hours is not None:
        when = candidate['depEtDatetime'] - timedelta(hours=next_hours)
        next_reading = {'label': 'next rdg', 'atMs': round(when.timestamp() * 1000)}
    return {'pool': pool, 'cadence': state, 'nextReading': next_reading}


def last_chance_hours_for(back_at_ms, settings, now):
    if back_at_ms is None:
        return None
    back_at = datetime.fromtimestamp(back_at_ms / 1000, ET_ZONE)
    if back_at <= now:
        return None
    return (back_at - now).total_seconds() / 3600 + settings['awayMarginHours']


def most_pressing_route(conn, trust, settings, candidates, skip_set, floor_estimates, readings_cache,
                        last_chance_hours):
    best = None
    for c in candidates:
        route_key = (c['org'], c['dest'], c['flightDate'])
        if route_key in skip_set:
            continue
        prev_readings = readings_for_candidate(conn, c, floor_estimates, readings_cache)
        _, _, urgency = pool_and_cadence_reading(conn, trust, settings, c, prev_readings, last_chance_hours)
        rank = route_day_rank(urgency, c['hoursUntilDep']) + (urgency, c['depEtDatetime'])
        if best is None or rank < best[0]:
            best = (rank, c)
    return None if best is None else best[1]


def route_day_rank(urgency, hours_until_dep):
    if urgency[0] == 2:
        return (1, 0)
    return (0, max(0, math.ceil(hours_until_dep / 24) - 1))


def readings_for_candidate(conn, c, floor_estimates, readings_cache):
    flight = (c['scheduleRow'], c['flightDate'])
    if flight not in readings_cache:
        readings_cache[flight] = previous_readings_for(
            conn, c['car'], c['dep'], c['org'], c['dest'], c['flightDate'], floor_estimates)[0]
    return readings_cache[flight]


def attach_t1_ranges(conn, trust, settings, candidate, prev_readings):
    for reading in prev_readings:
        reading['goldenTicket'] = False
        reading['t1Range'] = None
        if reading.get('held') is None or reading.get('hrs') is None:
            continue
        if reading['hrs'] <= settings['goldenTicketHours']:
            reading['goldenTicket'] = True
            continue
        reading['t1Range'] = trust.t1_range(conn, candidate['org'], candidate['dest'], candidate['dow'],
                                            candidate['dep'], candidate['flightDate'], reading['hrs'], reading['held'])


def get_next_batch(conn, skip_route_days=None, include_departed=False, forced_route=None, back_at_ms=None):
    settings = load_settings(conn)
    decline_settings = load_settings(conn, key=DECLINE_CURVE_SETTINGS_KEY, defaults=DEFAULT_DECLINE_CURVE_SETTINGS)
    night_start_hour = decline_settings['nightStartHour']
    night_end_hour = decline_settings['nightEndHour']
    now = eastern_now()
    last_chance_hours = last_chance_hours_for(back_at_ms, settings, now)

    studied = studied_cells(conn)
    days_ahead_of_today = settings['lookaheadDays']
    schedule_days = [now.date() - timedelta(days=1)] + [
        now.date() + timedelta(days=offset) for offset in range(days_ahead_of_today + 1)
    ]

    candidates = []
    departed_candidates = []  # always populated (see forced_route below) -
                               # include_departed only gates whether these
                               # end up rendered in the response's rows
    unconfirmed_codes = set()
    departed_by_route = {}  # (org, dest, flightDate) -> count - see summary_text below
    for schedule_date in schedule_days:
        dow = schedule_date.strftime('%a')
        flight_date_str = schedule_date.isoformat()

        sched_rows = conn.execute(
            """SELECT rowid, carrier, carriersFltNum_notStable_DO_NOT_USE, org, dest,
                      depTime, aircraftConfig
               FROM flightSchedule
               WHERE dayOfWeek = ? AND ignore = 0""",
            (dow,),
        ).fetchall()

        for rowid, carrier, flight_number, org, dest, dep_time, aircraft_config in sched_rows:
            if (org, dest, dow) not in studied:
                continue
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
                departed_candidates.append({
                    'scheduleRow': rowid, 'org': org, 'dest': dest, 'car': carrier,
                    'dep': dep_time, 'depEtDatetime': dep_dt,
                    'flightDate': flight_date_str, 'dow': dow,
                    'aircraftConfig': aircraft_config or 'TBD', 'flightNumber': flight_number or '',
                    'hoursUntilDep': hours_until_dep,
                })
                continue

            candidates.append({
                'scheduleRow': rowid, 'org': org, 'dest': dest, 'car': carrier,
                'dep': dep_time, 'depEtDatetime': dep_dt,
                'flightDate': flight_date_str, 'dow': dow,
                'aircraftConfig': aircraft_config or 'TBD', 'flightNumber': flight_number or '',
                'hoursUntilDep': hours_until_dep,
            })

    if unconfirmed_codes:
        codes = ', '.join(sorted(unconfirmed_codes))
        raise UnconfirmedAirportError(
            f"Can't compute departure times - these airport code(s) aren't confirmed yet: "
            f"{codes}. Run `python confirm_airports.py`, then try again."
        )

    candidates.sort(key=lambda c: c['depEtDatetime'])

    skip_set = {
        (r['org'], r['dest'], r['flightDate']) for r in (skip_route_days or [])
    }

    next_candidate = None
    if forced_route is not None:
        # Used after the schedule-edit modal closes (return to the exact
        # route+day just edited) and right after a real log (reshow the
        # same route with fresh values/T1 estimate) - never advances past
        # anything, just re-fetches, regardless of skip status.
        # Checked against both pools since the route may have finished
        # departing (or a flight may have just crossed the cutoff) while
        # the modal was open.
        next_candidate = next(
            (c for c in candidates + departed_candidates
             if c['org'] == forced_route['org'] and c['dest'] == forced_route['dest']
             and c['flightDate'] == forced_route['flightDate']),
            None,
        )
    trust = trust_snapshot(conn)
    floor_estimates = load_floor_estimates(conn)
    readings_cache = {}
    if next_candidate is None:
        next_candidate = most_pressing_route(conn, trust, settings, candidates, skip_set,
                                             floor_estimates, readings_cache, last_chance_hours)

    if next_candidate is None:
        total_departed = sum(departed_by_route.values())
        summary_text = (
            f"{total_departed} flight{'s' if total_departed != 1 else ''} already left"
            if total_departed > 0 else ''
        )
        wait_message = (
            "Nothing left to check this session - refresh to start over."
            if studied else
            "Nothing is being studied - switch on a scenario in Scenarios, then refresh."
        )
        return {
            'dowDisplay': now.strftime('%a'), 'dateDisplay': now.strftime('%b %-d'),
            'flightDate': now.date().isoformat(),
            'routeOrg': None, 'routeDest': None, 'summaryText': summary_text,
            'waitMinutes': None, 'waitMessage': wait_message, 'rows': [],
            'aircraftList': load_aircraft_list(conn), 'settings': settings,
            'openFullSettings': load_open_full_settings(conn),
            'gradingSettings': settings_form(load_grading_settings(conn)),
            'recentObservations': recent_observations(conn),
            **entry_field_config(),
        }

    d1_map = load_d1_map(conn)
    pools = pool_snapshot(conn)
    grades = FlightGrades(conn)
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
        prev_readings = readings_for_candidate(conn, c, floor_estimates, readings_cache)
        attach_t1_ranges(conn, trust, settings, c, prev_readings)
        route_rows.append({
            'scheduleRow': c['scheduleRow'], 'org': c['org'], 'dest': c['dest'], 'car': c['car'],
            'dep': c['dep'], 'depDisplay': minutes_to_12h(c['dep']),
            'flightNumber': c['flightNumber'], 'aircraftConfig': c['aircraftConfig'],
            'hasD1': d1_map.get(str(c['aircraftConfig']).lower(), False),
            'hoursUntilDep': round(c['hoursUntilDep'], 1),
            'minutesUntilDep': round(c['hoursUntilDep'] * 60),
            'isNext': c['scheduleRow'] == next_candidate['scheduleRow'],
            'departed': False,
            'previousReadings': prev_readings,
            'historyRange': pools.history_ranges(
                conn, c['org'], c['dest'], c['dow'], c['dep'], c['hoursUntilDep'], c['flightDate'],
            ),
            'coefficientsHint': resolved_coefficients_for_row(conn, c['org'], c['dest'], c['dow'], c['dep']),
            'flag': get_flight_day_flag(conn, c['car'], c['dep'], c['org'], c['dest'], c['flightDate']),
            'grade': grades.text_for(c['org'], c['dest'], c['dow'], c['dep']),
            'openFull': pools.own_record(conn, c['org'], c['dest'], c['dow'], c['dep']),
            **pool_and_cadence(conn, trust, settings, c, prev_readings, last_chance_hours, now),
        })

    if include_departed:
        for c in departed_candidates:
            if c['org'] != next_candidate['org'] or c['dest'] != next_candidate['dest'] \
                    or c['flightDate'] != next_candidate['flightDate']:
                continue
            prev_readings, today_c1 = previous_readings_for(conn, c['car'], c['dep'], c['org'], c['dest'],
                                                            c['flightDate'], floor_estimates)
            attach_t1_ranges(conn, trust, settings, c, prev_readings)
            route_rows.append({
                'scheduleRow': c['scheduleRow'], 'org': c['org'], 'dest': c['dest'], 'car': c['car'],
                'dep': c['dep'], 'depDisplay': minutes_to_12h(c['dep']),
                'flightNumber': c['flightNumber'], 'aircraftConfig': c['aircraftConfig'],
                'hasD1': d1_map.get(str(c['aircraftConfig']).lower(), False),
                'hoursUntilDep': round(c['hoursUntilDep'], 1),
                'minutesUntilDep': round(c['hoursUntilDep'] * 60),
                'isNext': False,
                'departed': True,
                'previousReadings': prev_readings,
                'historyRange': pools.history_ranges(
                    conn, c['org'], c['dest'], c['dow'], c['dep'], c['hoursUntilDep'], c['flightDate'],
                ),
                'coefficientsHint': resolved_coefficients_for_row(conn, c['org'], c['dest'], c['dow'], c['dep']),
                'flag': get_flight_day_flag(conn, c['car'], c['dep'], c['org'], c['dest'], c['flightDate']),
                'grade': grades.text_for(c['org'], c['dest'], c['dow'], c['dep']),
                'openFull': pools.own_record(conn, c['org'], c['dest'], c['dow'], c['dep']),
                'pool': trust.pool_record(conn, c['org'], c['dest'], c['dow'], c['dep']),
                'cadence': 'skip',
            })
        # Chronological, same order Delta's own site lists a route's day -
        # departed flights (earlier dep times, by construction) end up
        # first, active ones after, without needing a separate "departed"
        # section of the table.
        route_rows.sort(key=lambda r: r['dep'])

    route_flag = get_route_day_flag(
        conn, next_candidate['car'], next_candidate['org'], next_candidate['dest'], next_candidate['flightDate'],
    )

    route_key = (next_candidate['org'], next_candidate['dest'], next_candidate['flightDate'])
    route_departed_count = departed_by_route.get(route_key, 0)
    summary_text = (
        f"{route_departed_count} flight{'s' if route_departed_count != 1 else ''} already left"
        if route_departed_count > 0 else ''
    )

    next_date = datetime.strptime(next_candidate['flightDate'], '%Y-%m-%d').date()
    return {
        'dowDisplay': next_candidate['dow'], 'dateDisplay': next_date.strftime('%b %-d'),
        'flightDate': next_candidate['flightDate'],
        'routeOrg': next_candidate['org'], 'routeDest': next_candidate['dest'],
        'carrier': next_candidate['car'], 'routeFlag': route_flag,
        'summaryText': summary_text, 'waitMinutes': None, 'rows': route_rows,
        'aircraftList': load_aircraft_list(conn), 'settings': settings,
        'openFullSettings': load_open_full_settings(conn),
        'gradingSettings': settings_form(load_grading_settings(conn)),
        'recentObservations': recent_observations(conn),
        **entry_field_config(),
    }


def save_entry_dialog(conn, payload):
    """
    payload: {
      flightDate: "YYYY-MM-DD",   # the flight's own schedule date - may
                                   # be "yesterday" per the cross-midnight
                                   # handling above, not always ET-today
      entries: [{ scheduleRow, org, dest, car, dep, aircraftConfig,
                  flightNumber,
                  every key in ENTRY_COLUMNS (each '' or a value as typed),
                  insider: {InsiderReadings.NUMBER_FIELDS..., verdict, checkTime} }]
    }
    An entry is logged when any ENTRY_COLUMNS field is non-blank - a
    seat-map-only or glance-only entry is valid, blank can-buy just means
    not observed. Glance fields land in the cheap* columns.
    """
    flight_date = payload['flightDate']
    flight_date_obj = datetime.strptime(flight_date, '%Y-%m-%d').date()

    now = eastern_now()
    InsiderReadings.save_from_logging(conn, flight_date, payload['entries'], now)

    to_write = [
        e for e in payload['entries']
        if any(e.get(f, '') not in ('', None) for f in ENTRY_COLUMNS)
    ]
    if not to_write:
        conn.commit()
        return {'logged': 0}

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
            f"""INSERT INTO observations
               (carrier, carriersFltNum_notStable_DO_NOT_USE, org, dest, flightDate, checkTimestamp,
                hoursBeforeDep, depTime, {', '.join(ENTRY_COLUMNS.values())})
               VALUES ({', '.join('?' * (8 + len(ENTRY_COLUMNS)))})""",
            (entry['car'], entry['flightNumber'], entry['org'], entry['dest'],
             flight_date, check_timestamp, hours_before_dep, entry['dep'],
             *[num(field) for field in ENTRY_COLUMNS]),
        )

    for org, dest in {(e['org'], e['dest']) for e in to_write}:
        converge_flight_date(conn, org, dest, flight_date)

    conn.commit()
    return {'logged': len(to_write)}


def save_and_get_next_batch(conn, payload, include_departed=False, forced_route=None, back_at_ms=None):
    save_result = save_entry_dialog(conn, payload)
    next_result = get_next_batch(conn, None, include_departed, forced_route, back_at_ms)
    return {'logged': save_result['logged'], 'next': next_result}
