"""
GraphObservations backend: the fourth launcher tool. Three linked views
over one route - a heat map, overlaid per-date T1-estimate curves, and a
per-service-instance actual-seats-vs-hours-to-departure view - sharing one
filter set (route, day-of-week multi-select, flight-date range) and
independently toggleable on the frontend.

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

T1 estimate math (compute_t1_estimate, target T-60min) started as a
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

Two variants are computed per flight-instance, t1Old and t1New:
- t1Old: actual (binary-search) cabin values only, missing = 0 - this is
  the estimate exactly as it's always worked, unchanged.
- t1New: same, but where an actual value is missing and a cheap-side
  floor glance (cheapY/cheapCPlus/cheapFirstOrPS/cheapD1) is present and
  nonzero, substitutes an expected resolved value (see
  substitute_for_floor below) instead of treating the cabin as 0. A
  cheap 9 or confirmed cheap 0 never reaches this path in well-formed
  data, since the dialogue auto-fills and locks the matching actual
  column in both those cases - the substitution only ever fires for a
  genuine unresolved nonzero floor.
Both are shown side by side rather than replacing one with the other -
his call, to watch how far they diverge as real paired
cheap-floor/same-day-resolve data accumulates. Same estimate feeds both
the displayed value and any future full/open verdict logic - no separate
treatment for the verdict path (his explicit call: a substituted cabin
value carries real information and shouldn't be suppressed just because
it makes a borderline case less comfortable).
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

# Expected resolved value for a cheap-side floor reading of N, used only
# when the actual (binary-search) value for that cabin is still missing.
# His gut-feel seed, not yet fit to real data: floor + 1.7. Meant to be
# overwritten once enough same-day cheap-floor/actual-resolve pairs pile
# up to fit a real distribution (Beta over [floor, 8], mean/concentration
# parameterized - tabled until there's data to fit against; this
# constant is the mean only, and is all today's math needs).
FLOOR_MEAN_OFFSET = 1.7

DOW_ABBREV = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']


def substitute_for_floor(cheap_floor):
    """
    Expected resolved value for a genuine nonzero cheap-side floor
    reading, missing its actual value. Deliberately not called for a
    cheap 9 or cheap 0 - those cases already have a real actual value
    in well-formed data (auto-filled/locked by the dialogue), so they
    never need a substitute; this is purely the "we glanced a floor but
    haven't binary-searched it yet" case.
    """
    return cheap_floor + FLOOR_MEAN_OFFSET


def _dow_abbrev(flight_date_str):
    d = datetime.strptime(flight_date_str, '%Y-%m-%d').date()
    return DOW_ABBREV[d.weekday()]


def compute_trajectory(readings, target_hours, golden_ticket_hours):
    """
    The single source of truth for "where do we think this is headed."
    Returns None (no estimate - same conditions as the old
    compute_t1_estimate) or a dict:
        {'anchors': [{'hrs':.., 'ttl':..}, ...],  # 1 or 2 real readings
         'target':  {'hrs': target_hours, 'ttl': <estimate>}}

    anchors is whichever real reading(s) the estimate was actually
    built from. A straight line through anchors, extended to target,
    is BY CONSTRUCTION the same math this function used internally -
    that's deliberate, so the graph can draw exactly what the
    estimator did rather than a separately-computed decoration. If the
    estimator ever goes non-linear, this is the one place that
    changes: anchors/target stays the contract, only what's plotted
    between them (still this function's job, not the graph's) would
    stop being a straight line.

    Bracket-nearest-to-target logic, shared with the log dialogue's JS
    version (see SeatLoggingDialog.html's computeT1EstimateForPool -
    keep the two in lockstep by hand, since there's no shared module
    between Python and the browser here).

    readings: list of dicts with 'hrs' (hoursBeforeDep) and 'ttl'
    (summed seats). No ceiling exclusion - every reading is eligible.

    When readings straddle the target, uses the nearest reading on
    each side (true interpolation). When every reading is on the same
    side of the target - the common case, since the T-45min-to-T-60min
    window is rarely actually hit - uses the two NEAREST readings on
    that side rather than nearest+farthest: extrapolating off the full
    span pulled the estimate toward a stale, distant reading even
    after the trend had since flattened (confirmed against a real bug
    where two flat readings both summing to 5 got dragged down to 2.4
    by a lone 8-seat reading several hours further out).

    Special case: exactly one reading total only counts as an estimate
    if it's within the golden-ticket threshold (his call) - otherwise a
    single distant reading implies no trend at all and isn't shown as
    one.
    """
    if not readings:
        return None

    if len(readings) == 1:
        r = readings[0]
        if r['hrs'] > golden_ticket_hours:
            return None
        return {'anchors': [r], 'target': {'hrs': target_hours, 'ttl': r['ttl']}}

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

    def two_nearest(lst):
        # Two readings closest to the target, by absolute distance.
        # Same-side list, so this sorts the same as sorting by hrs
        # itself - naturally the two readings adjacent in time.
        ordered = sorted(lst, key=lambda r: abs(r['hrs'] - target_hours))
        return ordered[0], ordered[1]

    def result(anchors, ttl):
        return {'anchors': anchors, 'target': {'hrs': target_hours, 'ttl': ttl}}

    if before and after:
        b = nearest_of(before, True)   # smallest hrs among before-side
        a = nearest_of(after, False)   # largest hrs among after-side
        if b['hrs'] == a['hrs']:
            return result([b], b['ttl'])
        ttl = b['ttl'] + (a['ttl'] - b['ttl']) * (b['hrs'] - target_hours) / (b['hrs'] - a['hrs'])
        return result([b, a], ttl)

    if before:
        # Reaching here guarantees len(readings) >= 2 (top check above)
        # and after == [] (or we'd have taken the bracket branch), so
        # before necessarily holds >= 2 readings - two_nearest always
        # has something to return.
        nearer, second = two_nearest(before)
        if second['hrs'] == nearer['hrs']:
            return result([nearer], nearer['ttl'])
        slope = (second['ttl'] - nearer['ttl']) / (second['hrs'] - nearer['hrs'])
        ttl = nearer['ttl'] + slope * (target_hours - nearer['hrs'])
        return result([second, nearer], ttl)

    if after:
        nearer2, second2 = two_nearest(after)
        if second2['hrs'] == nearer2['hrs']:
            return result([nearer2], nearer2['ttl'])
        slope2 = (second2['ttl'] - nearer2['ttl']) / (second2['hrs'] - nearer2['hrs'])
        ttl = nearer2['ttl'] + slope2 * (target_hours - nearer2['hrs'])
        return result([second2, nearer2], ttl)

    return None


def compute_t1_estimate(readings, target_hours, golden_ticket_hours):
    """
    Thin wrapper kept for callers that only want the number, not the
    anchors - just the target's ttl from compute_trajectory, or None.
    Not re-implementing the math here on purpose: two copies of this
    logic drifting apart is exactly the bug class it already caused
    once (see compute_trajectory's docstring).
    """
    traj = compute_trajectory(readings, target_hours, golden_ticket_hours)
    return traj['target']['ttl'] if traj else None


def compute_confidence(readings, target_hours):
    """
    Confidence half-range in seats, at the target hour, given the full
    reading set for a flight-instance (not just the trajectory's
    anchors - deliberately the closest ANY real reading got to the
    target, matching how this has always been computed here).
    Wild-ass-guess linear model for now (CONFIDENCE_HOURS_TO_SEATS);
    swap the body of this function for something real later without
    touching any caller.
    """
    if not readings:
        return 0.0
    nearest_gap = min(abs(r['hrs'] - target_hours) for r in readings)
    return CONFIDENCE_HOURS_TO_SEATS * nearest_gap


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
                 t1Old, t1New, confidenceHalfRange, numReadings}
    t1Old is the estimate exactly as it's always worked (actual cabin
    values only, missing = 0). t1New substitutes an expected resolved
    value for a cabin whose actual value is missing but which has a
    genuine nonzero cheap-side floor glance - see substitute_for_floor.
    The two are shown side by side rather than one replacing the other.
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
                   hoursBeforeDep, y, cPlus, firstOrPS, d1,
                   cheapY, cheapCPlus, cheapFirstOrPS, cheapD1
            FROM observations
            WHERE {where_sql}""",
        params,
    ).fetchall()

    groups = defaultdict(list)
    for (carrier, flight_number, flight_date, check_ts, hrs, y, c_plus, first_ps, d1,
         cheap_y, cheap_c_plus, cheap_first_ps, cheap_d1) in rows:
        groups[(carrier, flight_number, flight_date)].append({
            'checkTimestamp': check_ts,
            'hrs': hrs,
            'y': y, 'cPlus': c_plus, 'firstOrPS': first_ps, 'd1': d1,
            'cheapY': cheap_y, 'cheapCPlus': cheap_c_plus,
            'cheapFirstOrPS': cheap_first_ps, 'cheapD1': cheap_d1,
        })

    dow_filter = set(days_of_week) if days_of_week else None

    # A handful of legacy rows carry a stray non-numeric value in what
    # should be an integer seat column - same tolerance ObservationsBrowser
    # already applies elsewhere - skip just that value rather than
    # dropping the whole reading.
    def as_int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    def resolve_cabin(actual, cheap):
        """
        One cabin's contribution to a reading's ttl, old and new.
        old: actual value if present, else 0 (unchanged behavior).
        new: actual value if present, else a floor-derived substitute if
        a genuine nonzero cheap floor is present, else 0. A cheap 9 or
        cheap 0 with no actual value shouldn't occur in well-formed data
        (the dialogue auto-fills/locks actual in both cases) - if it
        ever does, it's treated the same as any other missing actual
        value here (falls through to the nonzero-floor check, which a 9
        or 0 fails, so old and new agree and it's just 0/9 respectively
        via whichever value IS present).
        """
        if actual is not None:
            return actual, actual
        if cheap is not None and cheap != 0 and cheap != 9:
            return 0, substitute_for_floor(cheap)
        return 0, (cheap if cheap is not None else 0)

    points = []
    for (carrier, flight_number, flight_date), obs_list in groups.items():
        dow = _dow_abbrev(flight_date)
        if dow_filter and dow not in dow_filter:
            continue

        readings_old = []
        readings_new = []
        dep_minutes = None
        for obs in obs_list:
            if obs['hrs'] is None:
                continue
            try:
                hrs = float(obs['hrs'])
            except (TypeError, ValueError):
                continue

            y_old, y_new = resolve_cabin(as_int(obs['y']), as_int(obs['cheapY']))
            cp_old, cp_new = resolve_cabin(as_int(obs['cPlus']), as_int(obs['cheapCPlus']))
            fp_old, fp_new = resolve_cabin(as_int(obs['firstOrPS']), as_int(obs['cheapFirstOrPS']))
            d1_old, d1_new = resolve_cabin(as_int(obs['d1']), as_int(obs['cheapD1']))

            readings_old.append({'hrs': hrs, 'ttl': y_old + cp_old + fp_old + d1_old})
            readings_new.append({'hrs': hrs, 'ttl': y_new + cp_new + fp_new + d1_new})
            if dep_minutes is None:
                dep_minutes = dep_time_minutes(conn, org, obs['checkTimestamp'], obs['hrs'])

        if not readings_old or dep_minutes is None:
            continue

        trajectory = compute_trajectory(readings_old, T1_TARGET_HOURS, golden_ticket_hours)
        if trajectory is None:
            continue
        t1_old = trajectory['target']['ttl']
        t1_new = compute_t1_estimate(readings_new, T1_TARGET_HOURS, golden_ticket_hours)
        confidence_half_range = compute_confidence(readings_old, T1_TARGET_HOURS)

        points.append({
            'carrier': carrier,
            'flightNumber': flight_number,
            'flightDate': flight_date,
            'dow': dow,
            'depTimeMinutes': dep_minutes,
            't1Old': round(t1_old, 2),
            't1New': round(t1_new, 2) if t1_new is not None else None,
            'confidenceHalfRange': round(confidence_half_range, 2),
            'numReadings': len(readings_old),
            # Raw actual readings (precise, no uncertainty) for the
            # seats-vs-hours-to-departure view - readings_old only
            # (actual binary-search values, missing = 0), matching
            # what t1Old itself was computed from. Sorted furthest-out
            # first so the frontend can draw left-to-right as time
            # actually passes (large hrs -> 0).
            'readings': sorted(
                [{'hrs': round(r['hrs'], 2), 'ttl': r['ttl']} for r in readings_old],
                key=lambda r: -r['hrs'],
            ),
            # The trajectory - anchor reading(s) plus the target point
            # at T1_TARGET_HOURS - is a straight line today by
            # construction (see compute_trajectory). Rounded here so
            # the frontend never needs to know about float noise.
            'trajectory': {
                'anchors': [{'hrs': round(a['hrs'], 2), 'ttl': a['ttl']} for a in trajectory['anchors']],
                'targetHrs': T1_TARGET_HOURS,
                'targetTtl': round(t1_old, 2),
                'confidenceHalfRange': round(confidence_half_range, 2),
            },
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
