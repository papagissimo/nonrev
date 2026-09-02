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
from settings import load_settings, save_settings

# Wherever the gap between two sorted depTimes (minutes) exceeds this,
# they're treated as different services. Real schedule data showed
# distinct services hours apart with intra-cluster spread usually
# 5-20 min (a few known routes wider, ~60 min) - this comfortably
# splits real distinct services without splintering one service's own
# day-to-day wobble.
SERVICE_GAP_MINUTES = 60

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


def cluster_services(dep_times):
    """
    dep_times: iterable of int minutes-since-midnight. Returns a list of
    clusters (each a sorted list of the original values), split wherever
    the gap to the next value exceeds SERVICE_GAP_MINUTES.
    """
    times = sorted(dep_times)
    if not times:
        return []
    clusters = [[times[0]]]
    for t in times[1:]:
        if t - clusters[-1][-1] > SERVICE_GAP_MINUTES:
            clusters.append([t])
        else:
            clusters[-1].append(t)
    return clusters


def service_representative(cluster):
    """Median depTime of a cluster, rounded to the nearest 15 minutes."""
    times = sorted(cluster)
    n = len(times)
    if n % 2:
        median = times[n // 2]
    else:
        median = (times[n // 2 - 1] + times[n // 2]) / 2
    return round(median / 15) * 15


def get_route_services(conn, org, dest):
    """
    One entry per distinct service on this route, across all 7 days
    combined. Returns [{'repMinutes': int, 'rows': [{'carrier',
    'flightNumber', 'dayOfWeek', 'depTime'}, ...]}, ...] - rows is every
    flightSchedule row (any day of week) belonging to that service.
    """
    rows = conn.execute(
        """SELECT carrier, flightNumber, dayOfWeek, depTime
           FROM flightSchedule WHERE org = ? AND dest = ?""",
        (org, dest),
    ).fetchall()
    if not rows:
        return []

    clusters = cluster_services(r[3] for r in rows)

    rep_by_time = {}
    for cluster in clusters:
        rep = service_representative(cluster)
        for t in cluster:
            rep_by_time[t] = rep

    services = defaultdict(list)
    for carrier, flight_number, dow, dep_time in rows:
        rep = rep_by_time[dep_time]
        services[rep].append({
            'carrier': carrier, 'flightNumber': flight_number,
            'dayOfWeek': dow, 'depTime': dep_time,
        })

    return [{'repMinutes': rep, 'rows': members}
            for rep, members in sorted(services.items())]


def find_service_for_row(services, day_of_week, carrier, flight_number):
    """Which service (from get_route_services) a specific flightSchedule
    row belongs to, matched by (dayOfWeek, carrier, flightNumber)."""
    for service in services:
        for row in service['rows']:
            if (row['dayOfWeek'], row['carrier'], row['flightNumber']) == \
               (day_of_week, carrier, flight_number):
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


def get_open_full_counts(conn, org, dest, day_of_week, carrier, flight_number):
    """
    For the service that (org, dest, day_of_week, carrier, flight_number)
    belongs to, pooled across every day sharing that day's dayGrouping
    label, within the openFullSettings date range. Returns real counts,
    never a percentage:

        {'measured': n, 'open': n, 'full': n}

    measured = qualifying flight-date instances with a computable t1Old
    for this service on a grouped day, within the date range. open/full
    are classified against the settings' openThreshold/fullThreshold -
    a t1Old between them counts toward measured but neither bucket.
    """
    settings = load_open_full_settings(conn)

    services = get_route_services(conn, org, dest)
    target_service = find_service_for_row(services, day_of_week, carrier, flight_number)
    if target_service is None:
        return {'measured': 0, 'open': 0, 'full': 0}

    grouped_days = get_grouped_days(conn, org, dest, day_of_week)
    flight_keys = {
        (r['carrier'], r['flightNumber'])
        for r in target_service['rows'] if r['dayOfWeek'] in grouped_days
    }

    points = get_flight_points(
        conn, org, dest,
        days_of_week=list(grouped_days),
        date_from=settings['dateFrom'], date_to=None,
    )

    measured = open_count = full_count = 0
    for p in points:
        if (p['carrier'], p['flightNumber']) not in flight_keys:
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
