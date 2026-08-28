"""
GraphObservations backend: the fourth launcher tool. Two linked views over
one route - a heat map and overlaid per-date curves - sharing one filter
set (route, day-of-week multi-select, flight-date range).

Each plotted point is one FLIGHT INSTANCE on one calendar date (not one raw
observation) - e.g. "the 11:37am SLC-LAX flight on 2026-08-19" - positioned
at its own local departure time-of-day, colored/valued by its T1 estimate.
That grouping is safe without any cross-day flight-identity matching: within
a single flightDate, (carrier, flightNumber, org, dest) already uniquely
tags one real flight instance (see SeatLoggingDialog's docstring - every
observation carries a real flightNumber assigned at logging time). Nothing
here ever needs to decide whether "flight 3 last Wednesday" is "flight 3
this Wednesday" - each date's points stand on their own, placed by real
clock time, which is exactly what sidesteps that whole problem for this
graph (see project notes on the route-level view's design).

T1 estimate math (compute_bucket_estimate, target T-60min) started as a
port of the old Apps Script computeBucketEstimate_, then was revised with
him directly: dropped the T-61min oddity in favor of a clean T-60min, and
dropped "ceiling" (a displayed 9) exclusion entirely - the two readings
actually used are always the ones nearest the target, and a reading that
close to departure is very unlikely to still be sitting at a real ceiling.
Same bracket-nearest-target selection logic as the log dialogue's own JS
version (SeatLoggingDialog.html's computeT1EstimateForPool) - kept in
lockstep by hand since there's no shared module between Python and the
browser here. Computed here on the fly per flight-instance rather than
stored.
"""

from collections import defaultdict
from datetime import datetime

from ObservationsBrowser import dep_time_minutes
from settings import load_settings

# Locked design (agreed with him directly, not just a port default anymore):
# T-60min exactly - the old T-61min was a leftover distinction from an
# earlier conversation and never mattered on its own merits. No more
# "ceiling" (a displayed 9) exclusion either - the two readings actually
# used are always the ones closest to the target, and a reading that
# close to departure is very unlikely to still be sitting at a real
# ceiling, so the extra complexity wasn't earning its keep.
T1_TARGET_HOURS = 1.0

# Wild-ass-guess starting constant for the confidence bar's half-height, in
# T1-estimate units (seats) per hour of gap between the nearest real reading
# and the T-61min target. Deliberately not derived from anything (a
# jackknife analysis could tell us this properly later, explicitly tabled
# for now) - meant to be eyeballed against a real chart and edited here.
CONFIDENCE_HOURS_TO_SEATS = 0.75

DOW_ABBREV = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']


def _dow_abbrev(flight_date_str):
    d = datetime.strptime(flight_date_str, '%Y-%m-%d').date()
    return DOW_ABBREV[d.weekday()]


def compute_bucket_estimate(readings, target_hours, golden_ticket_hours):
    """
    Bracket-nearest-to-target estimate, shared logic with the log
    dialogue's JS version (see SeatLoggingDialog.html's
    computeT1EstimateForPool - keep the two in lockstep by hand, since
    there's no shared module between Python and the browser here).

    readings: list of dicts with 'hrs' (hoursBeforeDep) and 'ttl'
    (summed seats). No ceiling exclusion - every reading is eligible.

    Special case: exactly one reading total only counts as an estimate
    if it's within the golden-ticket threshold (his call) - otherwise a
    single distant reading implies no trend at all and isn't shown as
    one. Returns None (no estimate) or a float.
    """
    if not readings:
        return None

    if len(readings) == 1:
        return readings[0]['ttl'] if readings[0]['hrs'] <= golden_ticket_hours else None

    before = [r for r in readings if r['hrs'] >= target_hours]
    after = [r for r in readings if r['hrs'] <= target_hours]

    def nearest_of(lst, want_min):
        best = None
        for cur in lst:
            if best is None:
                best = cur
            elif want_min and cur['hrs'] < best['hrs']:
                best = cur
            elif not want_min and cur['hrs'] > best['hrs']:
                best = cur
        return best

    if before and after:
        b = nearest_of(before, True)   # smallest hrs among before-side
        a = nearest_of(after, False)   # largest hrs among after-side
        if b['hrs'] == a['hrs']:
            return b['ttl']
        return b['ttl'] + (a['ttl'] - b['ttl']) * (b['hrs'] - target_hours) / (b['hrs'] - a['hrs'])

    if before:
        # Reaching here guarantees len(readings) >= 2 (top check above)
        # and after == [] (or we'd have taken the bracket branch), so
        # before necessarily holds >= 2 readings - no separate single-
        # item fallback needed here, unlike the port this replaced.
        b2 = nearest_of(before, True)
        far = nearest_of(before, False)
        if far['hrs'] == b2['hrs']:
            return b2['ttl']
        slope = (far['ttl'] - b2['ttl']) / (far['hrs'] - b2['hrs'])
        return b2['ttl'] + slope * (target_hours - b2['hrs'])

    if after:
        a2 = nearest_of(after, False)
        far2 = nearest_of(after, True)
        if far2['hrs'] == a2['hrs']:
            return a2['ttl']
        slope2 = (far2['ttl'] - a2['ttl']) / (far2['hrs'] - a2['hrs'])
        return a2['ttl'] + slope2 * (target_hours - a2['hrs'])

    return None


def get_route_options(conn):
    rows = conn.execute(
        "SELECT DISTINCT org, dest FROM observations ORDER BY org, dest"
    ).fetchall()
    return [{'org': r[0], 'dest': r[1]} for r in rows]


def get_flight_points(conn, org, dest, days_of_week, date_from, date_to):
    """
    Returns one point per (flightNumber, flightDate) flight instance for
    the given route, filtered by day-of-week (list of 'Mon'/'Tue'/...,
    empty/None means all) and flightDate range (either end optional).

    Each point: {flightDate, dow, depTimeMinutes, depTimeDisplay,
                 t1Estimate, confidenceHalfRange, numReadings}
    Points with no computable depTimeMinutes (unconfirmed airport, or
    every reading in the group has a malformed hoursBeforeDep) are
    dropped - nothing to place them on the x-axis with.
    """
    golden_ticket_hours = load_settings(conn).get('goldenTicketHours', 1.5)

    where = ["org = ?", "dest = ?"]
    params = [org, dest]
    if date_from:
        where.append("flightDate >= ?")
        params.append(date_from)
    if date_to:
        where.append("flightDate <= ?")
        params.append(date_to)
    where_sql = " AND ".join(where)

    rows = conn.execute(
        f"""SELECT carrier, flightNumber, flightDate, checkTimestamp,
                   hoursBeforeDep, y, cPlus, firstOrPS, d1
            FROM observations
            WHERE {where_sql}""",
        params,
    ).fetchall()

    groups = defaultdict(list)
    for carrier, flight_number, flight_date, check_ts, hrs, y, c_plus, first_ps, d1 in rows:
        groups[(carrier, flight_number, flight_date)].append({
            'checkTimestamp': check_ts,
            'hrs': hrs,
            'y': y, 'cPlus': c_plus, 'firstOrPS': first_ps, 'd1': d1,
        })

    dow_filter = set(days_of_week) if days_of_week else None

    points = []
    for (carrier, flight_number, flight_date), obs_list in groups.items():
        dow = _dow_abbrev(flight_date)
        if dow_filter and dow not in dow_filter:
            continue

        readings = []
        dep_minutes = None
        for obs in obs_list:
            if obs['hrs'] is None:
                continue
            try:
                hrs = float(obs['hrs'])
            except (TypeError, ValueError):
                continue
            # A handful of legacy rows carry a stray non-numeric value in
            # what should be an integer seat column - same tolerance
            # ObservationsBrowser already applies elsewhere - skip just
            # that value rather than dropping the whole reading.
            def as_int(v):
                try:
                    return int(v)
                except (TypeError, ValueError):
                    return None
            y_i, cp_i, fp_i, d1_i = (as_int(obs['y']), as_int(obs['cPlus']),
                                      as_int(obs['firstOrPS']), as_int(obs['d1']))
            parts = [y_i, cp_i, fp_i, d1_i]
            ttl = sum(p for p in parts if p is not None)
            readings.append({'hrs': hrs, 'ttl': ttl})
            if dep_minutes is None:
                dep_minutes = dep_time_minutes(conn, org, obs['checkTimestamp'], obs['hrs'])

        if not readings or dep_minutes is None:
            continue

        t1_estimate = compute_bucket_estimate(readings, T1_TARGET_HOURS, golden_ticket_hours)
        if t1_estimate is None:
            continue

        nearest_gap = min(abs(r['hrs'] - T1_TARGET_HOURS) for r in readings)
        confidence_half_range = CONFIDENCE_HOURS_TO_SEATS * nearest_gap

        points.append({
            'carrier': carrier,
            'flightNumber': flight_number,
            'flightDate': flight_date,
            'dow': dow,
            'depTimeMinutes': dep_minutes,
            't1Estimate': round(t1_estimate, 2),
            'confidenceHalfRange': round(confidence_half_range, 2),
            'numReadings': len(readings),
        })

    points.sort(key=lambda p: (p['flightDate'], p['depTimeMinutes']))
    return points


def get_graph_data(conn, org, dest, days_of_week, date_from, date_to):
    points = get_flight_points(conn, org, dest, days_of_week, date_from, date_to)
    dates = sorted(set(p['flightDate'] for p in points))
    return {
        'org': org,
        'dest': dest,
        'points': points,
        'dates': dates,
    }
