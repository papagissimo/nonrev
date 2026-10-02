"""
Cross-day service identity and the open/full settings.

A "service" (his term) is a real flight - org+dest+time-of-day - as
distinct from Delta's flightNumber, which changes unpredictably and is
never used for matching (see flightSchedule's naming convention on that
column - not touched here, but same rule applies). Within one route,
service identity is found by simple gap-based clustering on depTime:
sort every depTime for the route, split wherever the gap to the next
one exceeds SERVICE_GAP_MINUTES. His call: exact cluster boundaries barely matter (a flight landing in the
"wrong" cluster still has roughly the right time of day, which is what
actually matters) - real schedule data already showed well-separated
clusters (hours apart) with small intra-cluster spread, so a simple gap
split is enough, no confidence score or fixed-K clustering needed.
get_route_services applies the same clustering to flightSchedule rows,
to group the current schedule itself.
"""

from collections import defaultdict

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
