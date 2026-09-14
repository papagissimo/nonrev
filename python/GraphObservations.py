"""
GraphObservations backend: the fourth launcher tool. Three linked views
over one route - a heat map, overlaid per-date T1-estimate curves, and a
per-service-instance actual-seats-vs-hours-to-departure view - sharing one
filter set (route, day-of-week multi-select, flight-date range) and
independently toggleable on the frontend.

Each plotted point is one FLIGHT INSTANCE on one calendar date (not one raw
observation) - e.g. "the 11:37am SLC-LAX flight on 2026-08-19" - positioned
at its own local departure time-of-day, colored/valued by its T1 estimate.
Readings are grouped into one flight instance by depTime clustering WITHIN
that single date (see clustering.py) - never by flightNumber, which isn't a
stable identifier at all (see domainKnowledge.md). Exact depTime equality
isn't used either: a same-day depTime correction on the schedule row (a typo
fix mid-session) would otherwise split one real flight's readings into two
points, so nearby depTimes on the same date are pooled the same gap-based
way ServiceGrouping pools services across different days. Nothing here ever
needs to decide whether "flight 3 last Wednesday" is "flight 3 this
Wednesday" - each date's points stand on their own, which is exactly what
sidesteps that whole problem for this graph (see project notes on the
route-level view's design).

T1 ESTIMATE: this used to be a standalone two-point trajectory
extrapolation, kept in its own compute_trajectory/compute_t1_estimate
functions, with a second floor-substituted variant (t1New) shown
alongside it (t1Old) for comparison. Both are gone now (his call - he
never actually used the side-by-side comparison, and didn't want two
estimator implementations existing at all, in Python or otherwise). This
module now calls T1Estimator.compute_t1_replay_column directly - the same
curve-slide estimator SeatLoggingDialog already uses - and takes the
LAST entry in its replay output (the estimate as of the most recent
reading of the day) as this flight-instance's single t1 value. One
estimator, two consumers, no separate copy of the math here.

Two real consequences of switching estimators, both deliberate:
- No more golden-ticket gate. The old method refused to show an estimate
  built purely from distant readings, since a two-point extrapolation
  from far out is genuinely untrustworthy. The curve-slide estimator
  doesn't have that weakness (it leans on the pooled historical curve
  when live data is thin), so the gate is gone - a point now shows
  whenever at least one cabin resolves to something, however far the
  underlying readings are from departure.
- No more trajectory line. The old chart drew a literal straight line
  from the anchor reading(s) to the target point, because that line WAS
  the two-point math, faithfully plotted. The curve-slide estimator
  doesn't have an equivalent "two points and a line" shape - the honest
  visualization would be the actual piecewise curve, which is a real
  future graphing feature, not part of this wiring. This module no
  longer emits a trajectory/anchors payload at all; the frontend shows
  the point value only, same as it always could.

Per-cabin reading resolution (feeding the estimator, and the raw
seats-vs-hours-to-departure view) is T1Estimator.resolved_actual_or_raw_cheap
- actual value if present, else the raw cheap-glance floor value itself,
else None - the SAME rule used everywhere else now, not a separate
old/new pair. See that function's docstring for why a cheap glance is no
longer laundered through FloorEstimates before being used.
"""

from collections import defaultdict
from datetime import datetime

from clustering import cluster_services, service_representative
from T1Estimator import compute_t1_replay_column, resolved_actual_or_raw_cheap, T1_TARGET_HOURS

# Wild-ass-guess starting constant for the confidence bar's half-height, in
# T1-estimate units (seats) per hour of gap between the nearest real reading
# and the target. Deliberately not derived from anything (a jackknife
# analysis could tell us this properly later, explicitly tabled for now) -
# meant to be eyeballed against a real chart and edited here. Independent
# of which estimator produces the point value itself - this measures
# reading-to-target proximity, nothing about the estimator's own math.
CONFIDENCE_HOURS_TO_SEATS = 0.75

DOW_ABBREV = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']


def _dow_abbrev(flight_date_str):
    d = datetime.strptime(flight_date_str, '%Y-%m-%d').date()
    return DOW_ABBREV[d.weekday()]


def compute_confidence(readings, target_hours):
    """
    Confidence half-range in seats, at the target hour, given the full
    reading set for a flight-instance. Wild-ass-guess linear model for
    now (CONFIDENCE_HOURS_TO_SEATS); swap the body of this function for
    something real later without touching any caller. Unchanged by the
    estimator switch - this has never depended on which method produces
    the point value, only on how far the nearest real reading sat from
    target.
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
    Returns one point per distinct flight instance for the given route -
    grouped by depTime clustering WITHIN each flightDate (see module
    docstring and clustering.py), never by flightNumber - filtered by
    day-of-week (list of 'Mon'/'Tue'/..., empty/None means all) and
    flightDate range (either end optional).

    Each point: {flightDate, dow, depTimeMinutes, flightNumber, t1,
                 confidenceHalfRange, numReadings, readings}
    t1 is the curve-slide estimate (T1Estimator.compute_t1_replay_column,
    last entry - i.e. as of the most recent reading logged that day) -
    see module docstring. flightNumber is carried through purely for
    display (a tooltip label) - the most recently logged reading's value
    in the group, never used to decide the grouping itself.
    Points with no depTime at all (never captured - a handful of legacy
    rows predating this column, or an unconfirmed-airport gap at logging
    time) are dropped - nothing to place them on the x-axis with. Points
    where every cabin's t1 contribution is unresolvable (see
    T1Estimator's own docstring for when that happens - practically only
    a route/service with no coefficients anywhere, not even the global
    default) are dropped too, same as before - nothing meaningful to show.
    """
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
        f"""SELECT flightDate, checkTimestamp, depTime,
                   carriersFltNum_notStable_DO_NOT_USE,
                   hoursBeforeDep, y, cPlus, firstOrPS, d1,
                   cheapY, cheapCPlus, cheapFirstOrPS, cheapD1
            FROM observations
            WHERE {where_sql}""",
        params,
    ).fetchall()

    # Group by flightDate first (each date's own set of distinct flights,
    # never blended across dates - see module docstring), then split each
    # date's own readings into distinct flight instances by depTime
    # clustering - not exact equality, so a same-day depTime correction
    # can't silently split one real flight's readings into two points.
    by_date = defaultdict(list)
    for row in rows:
        by_date[row[0]].append(row)

    groups = defaultdict(list)  # (flightDate, clusterRepMinutes) -> [obs dicts]
    for flight_date, date_rows in by_date.items():
        dep_times = [r[2] for r in date_rows if r[2] is not None]
        clusters = cluster_services(dep_times)
        rep_by_time = {}
        for cluster in clusters:
            rep = service_representative(cluster)
            for t in cluster:
                rep_by_time[t] = rep

        for (f_date, check_ts, dep_time, flight_number, hrs, y, c_plus, first_ps, d1,
             cheap_y, cheap_c_plus, cheap_first_ps, cheap_d1) in date_rows:
            if dep_time is None:
                continue  # nothing to cluster this reading into - dropped
            rep = rep_by_time[dep_time]
            groups[(flight_date, rep)].append({
                'checkTimestamp': check_ts,
                'depTime': dep_time,
                'flightNumber': flight_number,
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

    points = []
    for (flight_date, dep_minutes), obs_list in groups.items():
        dow = _dow_abbrev(flight_date)
        if dow_filter and dow not in dow_filter:
            continue

        # One resolved entry per raw reading - each carries its own real
        # depTime (needed to look up this instance's coefficients below)
        # alongside the per-cabin values the estimator and the raw
        # display both need. Sorted ascending by hrs (smallest first -
        # most recent check first, same convention T1Estimator's replay
        # already assumes) so index 0 is always "as of the most recent
        # reading logged today."
        resolved = []
        flight_number_display = ''
        for obs in obs_list:
            if obs['hrs'] is None:
                continue
            try:
                hrs = float(obs['hrs'])
            except (TypeError, ValueError):
                continue

            y = resolved_actual_or_raw_cheap(as_int(obs['y']), as_int(obs['cheapY']))
            cplus = resolved_actual_or_raw_cheap(as_int(obs['cPlus']), as_int(obs['cheapCPlus']))
            one_ps = resolved_actual_or_raw_cheap(as_int(obs['firstOrPS']), as_int(obs['cheapFirstOrPS']))
            d1 = resolved_actual_or_raw_cheap(as_int(obs['d1']), as_int(obs['cheapD1']))

            resolved.append({
                'hrs': hrs, 'depTime': obs['depTime'],
                'y': y, 'cplus': cplus, 'onePS': one_ps, 'd1': d1,
            })
            # Decoration only (see module docstring) - whichever reading's
            # value happens to be seen last in iteration order, no real
            # significance to the choice beyond having something to show.
            if obs['flightNumber']:
                flight_number_display = obs['flightNumber']

        if not resolved:
            continue

        resolved.sort(key=lambda r: r['hrs'])  # ascending hrs = most-recent-first

        # Any one of this instance's own raw depTimes resolves to the
        # same service (and therefore the same coefficients) as any
        # other - DeclineCurveFit.py writes a declineCurveCoefficients
        # row for every raw depTime a service spans, precisely so an
        # exact-match lookup never needs to know which one to pick (see
        # that module's persistence docstring). Using the most recent
        # reading's own depTime is as good a choice as any.
        dep_time_for_lookup = resolved[0]['depTime']
        t1_series = compute_t1_replay_column(
            conn, org, dest, flight_date, dep_time_for_lookup, resolved,
        )
        t1 = t1_series[0] if t1_series else None
        if t1 is None:
            continue

        confidence_half_range = compute_confidence(resolved, T1_TARGET_HOURS)

        # Raw display readings for the seats-vs-hours-to-departure view -
        # same resolution rule as the estimator itself, missing cabin
        # treated as 0 for a real total-seats-at-a-glance number (not fed
        # to the estimator, which needs None-vs-0 to stay distinct).
        # Sorted furthest-out first so the frontend draws left-to-right
        # as time actually passes (large hrs -> 0).
        display_readings = sorted(
            [
                {
                    'hrs': round(r['hrs'], 2),
                    'ttl': sum(v for v in (r['y'], r['cplus'], r['onePS'], r['d1']) if v is not None),
                }
                for r in resolved
            ],
            key=lambda r: -r['hrs'],
        )

        points.append({
            'flightNumber': flight_number_display,
            'flightDate': flight_date,
            'dow': dow,
            'depTimeMinutes': dep_minutes,
            't1': round(t1, 2),
            'confidenceHalfRange': round(confidence_half_range, 2),
            'numReadings': len(resolved),
            'readings': display_readings,
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
