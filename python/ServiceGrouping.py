"""
Cross-day service identity and open/full counts.

A "service" (his term) is a real flight - org+dest+time-of-day - as
distinct from Delta's flightNumber, which changes unpredictably and is
never used for matching (see flightSchedule's naming convention on that
column - not touched here, but same rule applies). Within one route,
service identity is found by simple gap-based clustering on depTime:
sort every depTime for the route, split wherever the gap to the next
one exceeds SERVICE_GAP_MINUTES. For open/full counts the times
clustered are the logged flights', never flightSchedule's, which only
describes this week. His
call: exact cluster boundaries barely matter (a flight landing in the
"wrong" cluster still has roughly the right time of day, which is what
actually matters) - real schedule data already showed well-separated
clusters (hours apart) with small intra-cluster spread, so a simple gap
split is enough, no confidence score or fixed-K clustering needed.
get_route_services applies the same clustering to flightSchedule rows,
to group the current schedule itself.

Open/full counts are always a real fraction (e.g. "5/7"), never a bare
percentage - a percentage alone hides the sample size, which matters as
much as the rate itself here (his explicit correction after building
this once already).
"""

from collections import defaultdict
from datetime import date

from GraphObservations import get_flight_points
from PoolingSettingsDialog import excluded_date_where_clause
from observation_filters import not_seat_map_only_where_clause
from clustering import cluster_services, service_representative
from settings import load_settings, save_settings

WEEKDAY_NAMES = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
HISTORY_CABIN_COLUMNS = {'y': 'y', 'cplus': 'cPlus', 'onePS': 'firstOrPS', 'd1': 'd1'}
HISTORY_EDGE_TOLERANCE_HOURS = 1.0

OPEN_FULL_SETTINGS_KEY = 'openFullSettings'
DEFAULT_OPEN_FULL_SETTINGS = {
    # Matches the anchor already used for the manually-written gold-star
    # verdicts ("Open every reading since 8/14") - the known-bad stretch
    # before that date is excluded by default.
    'dateFrom': '2026-08-14',
    'fullThreshold': 2,
    'openThreshold': 8,
}


def load_open_full_settings(conn):
    return load_settings(conn, key=OPEN_FULL_SETTINGS_KEY, defaults=DEFAULT_OPEN_FULL_SETTINGS)


def save_open_full_settings(conn, new_settings):
    save_settings(conn, new_settings, key=OPEN_FULL_SETTINGS_KEY)


def get_route_services(conn, org, dest):
    """
    One entry per distinct service on this route, across all 7 days
    combined. Returns [{'repMinutes': int, 'rows': [{'dayOfWeek',
    'depTime'}, ...]}, ...] - rows is every flightSchedule row (any day
    of week) belonging to that service.
    """
    rows = conn.execute(
        """SELECT dayOfWeek, depTime
           FROM flightSchedule WHERE org = ? AND dest = ?""",
        (org, dest),
    ).fetchall()
    if not rows:
        return []

    clusters = cluster_services(r[1] for r in rows)

    rep_by_time = {}
    for cluster in clusters:
        rep = service_representative(cluster)
        for t in cluster:
            rep_by_time[t] = rep

    services = defaultdict(list)
    for dow, dep_time in rows:
        rep = rep_by_time[dep_time]
        services[rep].append({'dayOfWeek': dow, 'depTime': dep_time})

    return [{'repMinutes': rep, 'rows': members}
            for rep, members in sorted(services.items())]


def find_service_for_row(services, day_of_week, dep_time):
    """Which service (from get_route_services) a specific flightSchedule
    row belongs to, matched by (dayOfWeek, depTime) - never flightNumber
    (see domainKnowledge.md)."""
    for service in services:
        for row in service['rows']:
            if (row['dayOfWeek'], row['depTime']) == (day_of_week, dep_time):
                return service
    return None


def get_known_day_groupings(conn):
    """
    Every dayGrouping label currently in use, across all routes - sorted,
    deduplicated. No confirmed filter (that column is gone) - every row
    always holds a real, meaningful label now (the migration seeded
    real midweek/weekend-shoulders/weekend defaults, not placeholders),
    so there's no "was this a real choice" distinction left to make.
    Purpose is unchanged: a live-derived "what have I already typed"
    list, not a hand-authored preset - it grows only from labels that
    actually exist, never from anything invented in advance.
    """
    rows = conn.execute(
        "SELECT DISTINCT dayGrouping FROM dayGroupings ORDER BY dayGrouping"
    ).fetchall()
    return [r[0] for r in rows]


def get_day_grouping_row(conn, org, dest, day_of_week):
    """Current dayGrouping label for one (org, dest, dayOfWeek). Falls
    back to the day standing alone if no row exists yet (e.g. a route
    added after the migration seeded everyone else)."""
    row = conn.execute(
        "SELECT dayGrouping FROM dayGroupings WHERE org=? AND dest=? AND dayOfWeek=?",
        (org, dest, day_of_week),
    ).fetchone()
    return {'dayGrouping': row[0] if row else day_of_week}


def save_day_grouping(conn, org, dest, day_of_week, grouping_label):
    conn.execute(
        """INSERT INTO dayGroupings (org, dest, dayOfWeek, dayGrouping)
           VALUES (?,?,?,?)
           ON CONFLICT(org, dest, dayOfWeek) DO UPDATE SET dayGrouping=excluded.dayGrouping""",
        (org, dest, day_of_week, grouping_label),
    )


def get_grouped_days(conn, org, dest, day_of_week):
    """Every dayOfWeek on this route sharing (org, dest, day_of_week)'s
    current dayGrouping label, including the day itself."""
    grouping = get_day_grouping_row(conn, org, dest, day_of_week)['dayGrouping']
    rows = conn.execute(
        "SELECT dayOfWeek FROM dayGroupings WHERE org=? AND dest=? AND dayGrouping=?",
        (org, dest, grouping),
    ).fetchall()
    days = {r[0] for r in rows}
    days.add(day_of_week)
    return days


def logged_service_containing(dep_time, logged_times):
    clusters = cluster_services(sorted(set(logged_times) | {dep_time}))
    return set(next(cluster for cluster in clusters if dep_time in cluster))


def get_open_full_counts(conn, org, dest, day_of_week, dep_time):
    """
    For the service that (org, dest, day_of_week, dep_time) belongs to,
    pooled across every day sharing that day's dayGrouping label, within
    the openFullSettings date range. Returns real counts, never a
    percentage:

        {'measured': n, 'open': n, 'full': n}

    measured = qualifying flight-date instances with a computable t1
    for this service on a grouped day, within the date range. open/full
    are classified against the settings' openThreshold/fullThreshold -
    a t1 between them counts toward measured but is neither open nor
    full. (t1
    is now the curve-slide estimate via T1Estimator, not the old
    two-point method - see GraphObservations.py's module docstring;
    this function didn't need to change beyond the field's name, since
    it only ever consumed whatever get_flight_points called its single
    point estimate.)

    The service is found from the logged flights alone: the depTimes of
    every flight logged on the grouped days are clustered together with
    dep_time itself (the live flightSchedule time is only the lookup key
    for today's flight, never what defines the service), and the flights
    in dep_time's cluster are that service's history.
    """
    settings = load_open_full_settings(conn)

    grouped_days = get_grouped_days(conn, org, dest, day_of_week)

    points = get_flight_points(
        conn, org, dest,
        days_of_week=list(grouped_days),
        date_from=settings['dateFrom'], date_to=None,
    )

    service_times = logged_service_containing(dep_time, (p['depTimeMinutes'] for p in points))

    measured = open_count = full_count = 0
    for p in points:
        if p['depTimeMinutes'] not in service_times:
            continue
        measured += 1
        if p['t1Old'] >= settings['openThreshold']:
            open_count += 1
        elif p['t1Old'] <= settings['fullThreshold']:
            full_count += 1

    return {'measured': measured, 'open': open_count, 'full': full_count}


def format_open_full(counts):
    """{'measured', 'open', 'full'} -> display-ready fraction strings,
    e.g. {'openDisplay': '5/7', 'fullDisplay': '1/7'}. Real counts, no
    percentage - the denominator matters as much as the rate."""
    m = counts['measured']
    return {
        'measured': m,
        'openDisplay': f"{counts['open']}/{m}" if m else "—",
        'fullDisplay': f"{counts['full']}/{m}" if m else "—",
    }


def value_at_hours(points, hours):
    """points: [(hoursBeforeDep, value)] sorted by hours. Linear between the
    two readings bracketing hours; outside the flight's readings, the
    nearest one only if it is within HISTORY_EDGE_TOLERANCE_HOURS."""
    for (h_near, v_near), (h_far, v_far) in zip(points, points[1:]):
        if h_near <= hours <= h_far:
            if h_far == h_near:
                return v_near
            return v_near + (v_far - v_near) * (hours - h_near) / (h_far - h_near)
    nearest_hours, nearest_value = min(points, key=lambda p: abs(p[0] - hours))
    if abs(nearest_hours - hours) <= HISTORY_EDGE_TOLERANCE_HOURS:
        return nearest_value
    return None


def history_ranges_for_row(conn, org, dest, day_of_week, dep_time, hours_until_dep, flight_date):
    """What prior flights of this service read at hours_until_dep, per
    cabin: {'low', 'high', 'count'}, or None where no prior flight has
    readings around that hour. Same service matching, grouped days, date
    range and excluded ranges as get_open_full_counts."""
    settings = load_open_full_settings(conn)
    grouped_days = get_grouped_days(conn, org, dest, day_of_week)
    rows = conn.execute(
        f"""SELECT flightDate, depTime, hoursBeforeDep, y, cPlus, firstOrPS, d1
            FROM observations
            WHERE org = ? AND dest = ? AND flightDate >= ? AND flightDate < ?
              AND depTime IS NOT NULL AND hoursBeforeDep IS NOT NULL
              AND {excluded_date_where_clause()} AND {not_seat_map_only_where_clause()}""",
        (org, dest, settings['dateFrom'], flight_date),
    ).fetchall()
    rows = [r for r in rows if WEEKDAY_NAMES[date.fromisoformat(r[0]).weekday()] in grouped_days]
    service_times = logged_service_containing(dep_time, (r[1] for r in rows))

    readings_by_date = defaultdict(list)
    for flight_date_seen, logged_dep, hours, y, c_plus, first_or_ps, d1 in rows:
        if logged_dep in service_times:
            readings_by_date[flight_date_seen].append(
                {'hours': hours, 'y': y, 'cPlus': c_plus, 'firstOrPS': first_or_ps, 'd1': d1})

    ranges = {}
    for cabin_key, column in HISTORY_CABIN_COLUMNS.items():
        values = []
        for readings in readings_by_date.values():
            points = sorted((r['hours'], r[column]) for r in readings if r[column] is not None)
            value = value_at_hours(points, hours_until_dep) if points else None
            if value is not None:
                values.append(value)
        ranges[cabin_key] = (
            {'low': round(min(values)), 'high': round(max(values)), 'count': len(values)}
            if values else None
        )
    return ranges
