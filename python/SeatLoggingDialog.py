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

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from timezones import et_equivalent_datetime, UnconfirmedAirportError
from clustering import cluster_services
from deptime_convergence import converge_flight_date
from settings import load_settings, DECLINE_CURVE_SETTINGS_KEY, DEFAULT_DECLINE_CURVE_SETTINGS
from ServiceGrouping import get_open_full_counts, format_open_full, load_open_full_settings
from DeclineCurveFit import piecewise_model, effective_hours_between, slide_c1_through_readings
from DeclineCurveHierarchy import resolve_coefficients
from T1Estimator import compute_t1_replay_column, CABIN_KEY_TO_COLUMN

DEP_CUTOFF_MINUTES = 45
ET_ZONE = ZoneInfo('America/New_York')


def eastern_now():
    return datetime.now(ET_ZONE)


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


def previous_readings_for(conn, carrier, dep_time, org, dest, flight_date):
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

    Each cabin value is the real actual if one was logged, else blank -
    no glance/cheap fallback of any kind (retired 2026-09-14, his call:
    "every whiff of it, gone" - the old FloorEstimates-derived decimal
    substitute is fully removed, not just hidden).

    Each row also carries 't1': the curve-slide T1 estimate as of that
    point in the day (see T1Estimator.compute_t1_replay_column) - this
    is now the ONE T1 value shown anywhere in the dialog (his call - he
    never wants two different T1 numbers displayed side by side), computed
    server-side rather than in the browser (his call - no good reason for
    real calculation to live client-side). None where no estimate is
    resolvable yet for that row (see T1Estimator's docstring for when
    that happens) - the client renders that as a blank cell, same as any
    other missing value.

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

    if is_today:
        rows = conn.execute(
            """SELECT hoursBeforeDep, y, cPlus, firstOrPS, d1, checkTimestamp, depTime
               FROM observations
               WHERE readingType='avail' AND carrier=? AND org=? AND dest=? AND flightDate=?""",
            (carrier, org, dest, flight_date),
        ).fetchall()

        # Cluster today's own logged depTimes together with the CURRENT
        # depTime so a same-day schedule correction can't silently drop
        # readings logged under the old value - see module docstring.
        clusters = cluster_services([r[6] for r in rows] + [dep_time])
        this_flights_cluster = next(c for c in clusters if dep_time in c)

        current_dep_dt = et_equivalent_datetime(
            conn, dep_time, org, datetime.strptime(flight_date, '%Y-%m-%d').date()
        )
        readings_raw = []
        for hbd, y, cplus, first_or_ps, d1, check_ts, own_dep in rows:
            if own_dep not in this_flights_cluster:
                continue
            check_dt = datetime.strptime(check_ts, '%Y-%m-%d %H:%M').replace(tzinfo=current_dep_dt.tzinfo)
            live_hbd = round((current_dep_dt - check_dt).total_seconds() / 3600, 2)
            readings_raw.append((live_hbd, y, cplus, first_or_ps, d1))
        readings_raw.sort(key=lambda r: r[0])  # most-recent-check first, same convention as below
    else:
        readings_raw = conn.execute(
            """SELECT hoursBeforeDep, y, cPlus, firstOrPS, d1
               FROM observations
               WHERE readingType='avail' AND carrier=? AND depTime=? AND org=? AND dest=? AND flightDate=?
               ORDER BY hoursBeforeDep ASC""",
            (carrier, dep_time, org, dest, flight_date),
        ).fetchall()

    readings = [
        {
            'hrs': r[0],
            'y': r[1],
            'cplus': r[2],
            'onePS': r[3],
            'd1': r[4],
        }
        for r in readings_raw
    ]
    t1_column = compute_t1_replay_column(conn, org, dest, flight_date, dep_time, readings)
    for reading, t1 in zip(readings, t1_column):
        reading['t1'] = t1

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
        """SELECT org, dest, hoursBeforeDep, y, cPlus, firstOrPS, d1, depTime
           FROM observations WHERE readingType='avail'
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


def get_launcher_summary(conn):
    """
    Whole-day, all-routes tally for the launcher page: how many scheduled
    flights are left to check, how many have already departed, and how
    many have already caught a "golden ticket" reading (a real logged
    hoursBeforeDep at or under the configurable threshold in settings) -
    a glance-at-once view of where the day's logging stands, distinct
    from get_next_batch's per-route walk.
    """
    settings = load_settings(conn)
    threshold = settings.get('goldenTicketHours', 1.5)
    now = eastern_now()
    schedule_days = [now.date(), now.date() - timedelta(days=1)]

    total = 0
    departed = 0
    golden = 0

    for schedule_date in schedule_days:
        dow = schedule_date.strftime('%a')
        flight_date_str = schedule_date.isoformat()

        sched_rows = conn.execute(
            """SELECT carrier, org, dest, depTime
               FROM flightSchedule WHERE dayOfWeek = ? AND ignore = 0""",
            (dow,),
        ).fetchall()

        for carrier, org, dest, dep_time in sched_rows:
            try:
                dep_dt = et_equivalent_datetime(conn, dep_time, org, schedule_date)
            except UnconfirmedAirportError:
                continue

            hours_until_dep = (dep_dt - now).total_seconds() / 3600
            total += 1
            if hours_until_dep * 60 <= DEP_CUTOFF_MINUTES:
                departed += 1
                continue

            best_hrs = conn.execute(
                """SELECT MIN(hoursBeforeDep) FROM observations
                   WHERE readingType='avail' AND carrier=? AND depTime=?
                   AND org=? AND dest=? AND flightDate=?
                   AND hoursBeforeDep IS NOT NULL""",
                (carrier, dep_time, org, dest, flight_date_str),
            ).fetchone()[0]
            if best_hrs is not None and best_hrs <= threshold:
                golden += 1

    return {
        'totalScheduled': total,
        'departed': departed,
        'remaining': total - departed,
        'goldenTickets': golden,
        'goldenTicketHours': threshold,
    }


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


def get_next_batch(conn, skip_route_days=None, include_departed=False, forced_route=None):
    settings = load_settings(conn)
    decline_settings = load_settings(conn, key=DECLINE_CURVE_SETTINGS_KEY, defaults=DEFAULT_DECLINE_CURVE_SETTINGS)
    night_start_hour = decline_settings['nightStartHour']
    night_end_hour = decline_settings['nightEndHour']
    now = eastern_now()

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
            """SELECT fs.rowid, fs.carrier, fs.carriersFltNum_notStable_DO_NOT_USE, fs.org, fs.dest,
                      fs.depTime, fs.aircraftConfig, fs.verdict, fs.verdictType
               FROM flightSchedule fs
               LEFT JOIN routeSettings rs ON rs.org = fs.org AND rs.dest = fs.dest
               WHERE fs.dayOfWeek = ? AND fs.ignore = 0 AND COALESCE(rs.studyThisRoute, 1) = 1""",
            (dow,),
        ).fetchall()

        for rowid, carrier, flight_number, org, dest, dep_time, aircraft_config, verdict, verdict_type in sched_rows:
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
                    'hoursUntilDep': hours_until_dep, 'verdict': verdict or '',
                    'verdictType': verdict_type or 'info',
                })
                continue

            # 'axed'/'starred' suppression is OFF (his call - too many
            # routes had accumulated a full set of one or the other,
            # silently making whole routes unreachable with no signal
            # that they'd dropped out). verdict/verdictType are still
            # computed, stored, and shown below - only ever a display
            # flag, never something that removes a flight from the pool.

            candidates.append({
                'scheduleRow': rowid, 'org': org, 'dest': dest, 'car': carrier,
                'dep': dep_time, 'depEtDatetime': dep_dt,
                'flightDate': flight_date_str, 'dow': dow,
                'aircraftConfig': aircraft_config or 'TBD', 'flightNumber': flight_number or '',
                'hoursUntilDep': hours_until_dep,
                'verdict': verdict or '', 'verdictType': verdict_type or 'info',
            })

    if unconfirmed_codes:
        codes = ', '.join(sorted(unconfirmed_codes))
        raise UnconfirmedAirportError(
            f"Can't compute departure times - these airport code(s) aren't confirmed yet: "
            f"{codes}. Run `python confirm_airports.py`, then try again."
        )

    candidates.sort(key=lambda c: c['depEtDatetime'])

    # No cadence/eligibility engine anymore - every scheduled, non-departed
    # flight in the window is a candidate every time. What keeps "next"
    # moving forward through a session, instead of re-offering the same
    # route over and over, is purely this session-scoped marker: a route+
    # day lands in here the moment it's either logged (a real save) or
    # explicitly blank-submitted ("nothing to log here right now") -
    # either way, it's skipped for the rest of this session. Client
    # resets it on reload, which is the deliberate way back to the top of
    # the list. Keyed on (org, dest, flightDate) so skipping today's
    # dtw-pdx can never bleed into tomorrow's.
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
    if next_candidate is None:
        next_candidate = next(
            (c for c in candidates
             if (c['org'], c['dest'], c['flightDate']) not in skip_set),
            None,
        )

    if next_candidate is None:
        total_departed = sum(departed_by_route.values())
        summary_text = (
            f"{total_departed} flight{'s' if total_departed != 1 else ''} already left"
            if total_departed > 0 else ''
        )
        wait_message = "Nothing left to check this session - refresh to start over."
        return {
            'dowDisplay': now.strftime('%a'), 'dateDisplay': now.strftime('%b %-d'),
            'flightDate': now.date().isoformat(),
            'routeOrg': None, 'routeDest': None, 'summaryText': summary_text,
            'waitMinutes': None, 'waitMessage': wait_message, 'rows': [],
            'aircraftOptions': load_aircraft_options(conn), 'settings': settings,
            'openFullSettings': load_open_full_settings(conn),
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
        prev_readings, today_c1 = previous_readings_for(conn, c['car'], c['dep'], c['org'], c['dest'], c['flightDate'])
        route_rows.append({
            'scheduleRow': c['scheduleRow'], 'org': c['org'], 'dest': c['dest'], 'car': c['car'],
            'dep': c['dep'], 'depDisplay': minutes_to_12h(c['dep']),
            'flightNumber': c['flightNumber'], 'aircraftConfig': c['aircraftConfig'],
            'hasD1': d1_map.get(str(c['aircraftConfig']).lower(), False),
            'hoursUntilDep': round(c['hoursUntilDep'], 1),
            'isNext': c['scheduleRow'] == next_candidate['scheduleRow'],
            'departed': False,
            'previousReadings': prev_readings,
            'curveEstimate': curve_estimates_for_row(
                conn, c['org'], c['dest'], c['dow'], c['dep'], c['hoursUntilDep'], c['depEtDatetime'],
                night_start_hour, night_end_hour, today_c1,
            ),
            'coefficientsHint': resolved_coefficients_for_row(conn, c['org'], c['dest'], c['dow'], c['dep']),
            'flag': get_flight_day_flag(conn, c['car'], c['dep'], c['org'], c['dest'], c['flightDate']),
            'verdict': c['verdict'], 'verdictType': c['verdictType'],
            'openFull': format_open_full(
                get_open_full_counts(conn, c['org'], c['dest'], c['dow'], c['dep'])
            ),
        })

    if include_departed:
        for c in departed_candidates:
            if c['org'] != next_candidate['org'] or c['dest'] != next_candidate['dest'] \
                    or c['flightDate'] != next_candidate['flightDate']:
                continue
            prev_readings, today_c1 = previous_readings_for(conn, c['car'], c['dep'], c['org'], c['dest'], c['flightDate'])
            route_rows.append({
                'scheduleRow': c['scheduleRow'], 'org': c['org'], 'dest': c['dest'], 'car': c['car'],
                'dep': c['dep'], 'depDisplay': minutes_to_12h(c['dep']),
                'flightNumber': c['flightNumber'], 'aircraftConfig': c['aircraftConfig'],
                'hasD1': d1_map.get(str(c['aircraftConfig']).lower(), False),
                'hoursUntilDep': round(c['hoursUntilDep'], 1),
                'isNext': False,
                'departed': True,
                'previousReadings': prev_readings,
                'curveEstimate': curve_estimates_for_row(
                    conn, c['org'], c['dest'], c['dow'], c['dep'], c['hoursUntilDep'], c['depEtDatetime'],
                    night_start_hour, night_end_hour, today_c1,
                ),
                'coefficientsHint': resolved_coefficients_for_row(conn, c['org'], c['dest'], c['dow'], c['dep']),
                'flag': get_flight_day_flag(conn, c['car'], c['dep'], c['org'], c['dest'], c['flightDate']),
                'verdict': c['verdict'], 'verdictType': c['verdictType'],
                'openFull': format_open_full(
                    get_open_full_counts(conn, c['org'], c['dest'], c['dow'], c['dep'])
                ),
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
        'aircraftOptions': load_aircraft_options(conn), 'settings': settings,
        'openFullSettings': load_open_full_settings(conn),
        'recentObservations': recent_observations(conn),
    }


def save_entry_dialog(conn, payload):
    """
    payload: {
      flightDate: "YYYY-MM-DD",   # the flight's own schedule date - may
                                   # be "yesterday" per the cross-midnight
                                   # handling above, not always ET-today
      entries: [{ scheduleRow, org, dest, car, dep, aircraftConfig,
                  flightNumber,
                  y, cplus, onePS, d1 (each '' or a value as typed) }]
    }
    No cheap/glance keys anymore (retired 2026-09-14, "every whiff of it,
    gone" - his call) - the cheapY/cheapCplus/cheapOnePS/cheapD1 columns
    still exist on the observations table itself (historical data from
    before this date stays intact), but nothing writes to them anymore.
    """
    flight_date = payload['flightDate']
    flight_date_obj = datetime.strptime(flight_date, '%Y-%m-%d').date()

    SEAT_KEYS = ('y', 'cplus', 'onePS', 'd1')
    to_write = [
        e for e in payload['entries']
        if any(e.get(f, '') not in ('', None) for f in SEAT_KEYS)
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
               (carrier, carriersFltNum_notStable_DO_NOT_USE, org, dest, flightDate, checkTimestamp,
                hoursBeforeDep, depTime, readingType, y, cPlus, firstOrPS, d1)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (entry['car'], entry['flightNumber'], entry['org'], entry['dest'],
             flight_date, check_timestamp, hours_before_dep, entry['dep'], 'avail',
             num('y'), num('cplus'), num('onePS'), num('d1')),
        )

    for org, dest in {(e['org'], e['dest']) for e in to_write}:
        converge_flight_date(conn, org, dest, flight_date)

    conn.commit()
    return {'logged': len(to_write)}


def save_and_get_next_batch(conn, payload, include_departed=False, forced_route=None):
    save_result = save_entry_dialog(conn, payload)
    next_result = get_next_batch(conn, None, include_departed, forced_route)
    return {'logged': save_result['logged'], 'next': next_result}
