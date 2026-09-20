"""
Same-day depTime convergence: within one route and flight date, readings
whose depTimes fall in the same clustering.cluster_services cluster are one
flight, and the cluster's last reading (by checkTimestamp) is truth - every
earlier reading in it gets that depTime, with hoursBeforeDep recomputed to
match. Never looks at flightSchedule or flightNumber, and never touches
another flight date.

converge_flight_date runs right after a reading is logged.
backfill_deptime_convergence.py runs the same correction over the whole table.
"""

from datetime import datetime

from clustering import cluster_services
from timezones import UnconfirmedAirportError, et_equivalent_datetime

CHECK_TS_FORMAT = '%Y-%m-%d %H:%M'


def corrections_for_flight_date(conn, org, dest, flight_date, readings):
    """readings: [(observationId, checkTimestamp, depTime)] for one route on
    one flight date. Returns [(depTime, hoursBeforeDep, observationId)] for
    the readings that need correcting. Raises UnconfirmedAirportError when
    org's timezone isn't known."""
    corrections = []
    flight_date_obj = datetime.strptime(flight_date, '%Y-%m-%d').date()

    for cluster in cluster_services(r[2] for r in readings):
        if len(set(cluster)) <= 1:
            continue

        members = [r for r in readings if r[2] in cluster]
        members.sort(key=lambda r: r[1])
        anchor_dep = members[-1][2]
        anchor_dt = et_equivalent_datetime(conn, anchor_dep, org, flight_date_obj)

        for obs_id, check_ts, dep in members:
            if dep == anchor_dep:
                continue
            check_dt = datetime.strptime(check_ts, CHECK_TS_FORMAT).replace(tzinfo=anchor_dt.tzinfo)
            new_hbd = (anchor_dt - check_dt).total_seconds() / 3600
            corrections.append((anchor_dep, round(new_hbd, 2), obs_id))

    return corrections


def apply_corrections(conn, corrections):
    conn.executemany(
        "UPDATE observations SET depTime=?, hoursBeforeDep=? WHERE observationId=?",
        corrections,
    )


def converge_flight_date(conn, org, dest, flight_date):
    """Corrects every reading of one route on one flight date. Returns how
    many readings changed. Leaves the readings alone if org's timezone isn't
    confirmed. Does not commit."""
    readings = conn.execute(
        """SELECT observationId, checkTimestamp, depTime FROM observations
           WHERE org = ? AND dest = ? AND flightDate = ? AND depTime IS NOT NULL
           ORDER BY observationId""",
        (org, dest, flight_date),
    ).fetchall()
    try:
        corrections = corrections_for_flight_date(conn, org, dest, flight_date, readings)
    except UnconfirmedAirportError:
        return 0
    apply_corrections(conn, corrections)
    return len(corrections)
