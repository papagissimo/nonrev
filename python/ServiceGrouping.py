"""
Cross-day service identity and open/full counts.

A "service" (his term) is a real flight - org+dest+time-of-day - as
distinct from Delta's flightNumber, which changes unpredictably and is
never used for matching (see flightSchedule's naming convention on that
column - not touched here, but same rule applies). Within one route,
service identity is found by simple gap-based clustering on depTime
across all 7 flightSchedule rows: sort every depTime for the route,
split wherever the gap to the next one exceeds SERVICE_GAP_MINUTES. His
call: exact cluster boundaries barely matter (a flight landing in the
"wrong" cluster still has roughly the right time of day, which is what
actually matters) - real schedule data already showed well-separated
clusters (hours apart) with small intra-cluster spread, so a simple gap
split is enough, no confidence score or fixed-K clustering needed.

Open/full counts are always a real fraction (e.g. "5/7"), never a bare
percentage - a percentage alone hides the sample size, which matters as
much as the rate itself here (his explicit correction after building
this once already).
"""

from collections import defaultdict

from GraphObservations import get_flight_points
from clustering import cluster_services, service_representative
from settings import load_settings, save_settings

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


def get_open_full_counts(conn, org, dest, day_of_week, dep_time):
    """
    For the service that (org, dest, day_of_week, dep_time) belongs to,
    pooled across every day sharing that day's dayGrouping label, within
    the openFullSettings date range. Returns real counts, never a
    percentage:

        {'measured': n, 'open': n, 'full': n}

    measured = qualifying flight-date instances with a computable t1Old
    for this service on a grouped day, within the date range. open/full
    are classified against the settings' openThreshold/fullThreshold -
    a t1Old between them counts toward measured but neither bucket.

    Matched against get_flight_points' own depTimeMinutes - which is
    ITSELF a cluster representative (rounded to the nearest 15 min from
    real observations), not a raw depTime - by the service's own
    repMinutes (the same kind of representative, computed from
    flightSchedule's raw depTimes). Comparing representative-to-
    representative is deliberate: comparing a raw schedule depTime
    (rarely a clean multiple of 15) against a rounded graph point would
    essentially never match.
    """
    settings = load_open_full_settings(conn)

    services = get_route_services(conn, org, dest)
    target_service = find_service_for_row(services, day_of_week, dep_time)
    if target_service is None:
        return {'measured': 0, 'open': 0, 'full': 0}

    grouped_days = get_grouped_days(conn, org, dest, day_of_week)

    points = get_flight_points(
        conn, org, dest,
        days_of_week=list(grouped_days),
        date_from=settings['dateFrom'], date_to=None,
    )

    measured = open_count = full_count = 0
    for p in points:
        if p['depTimeMinutes'] != target_service['repMinutes']:
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
