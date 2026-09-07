"""
Shared depTime clustering - identifies "distinct real flights" by
grouping nearby departure times, never by flightNumber (see
domainKnowledge.md). Used two ways:
  - ServiceGrouping.py: across all 7 days of a route, to find recurring
    "services" for open/full pooling.
  - GraphObservations.py: within a single flightDate, to group same-day
    readings into one flight instance for the trend graph - needed
    because a mid-day depTime correction on a schedule row would
    otherwise split one real flight's readings into two if grouped by
    exact depTime equality.

Split into its own module (rather than living in ServiceGrouping, where
it started) so GraphObservations can import it too - ServiceGrouping
already imports FROM GraphObservations (get_flight_points), so the
reverse import would be circular.
"""

# Wherever the gap between two sorted depTimes (minutes) exceeds this,
# they're treated as different flights/services. Real schedule data
# showed distinct services hours apart with intra-cluster spread usually
# 5-20 min (a few known routes wider, ~60 min) - this comfortably splits
# real distinct flights without splintering one flight's own wobble
# (day-to-day schedule drift, or a same-day depTime correction).
SERVICE_GAP_MINUTES = 60


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
