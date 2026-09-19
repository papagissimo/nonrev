"""
One-time historical cleanup (2026-09-18), his call.

Within a same-day flight instance (identified by clustering.cluster_services
- see that module for why, same identity mechanism GraphObservations already
uses), each reading's depTime is whatever flightSchedule showed at the exact
moment it was logged. That value drifts a little through the day - not a
one-time correction, continuous convergence. Analysis that day (674 flight-day
groups, 520 with internal drift) showed deviation from the LAST reading in a
cluster shrinks steadily as departure approaches: avg 2.2 min at 8+ hrs out,
down to 0.3 min inside the final hour. That's Delta's own displayed time
sharpening as the operational picture firms up, not symmetric noise - so the
last reading is the best available estimate of a flight's true departure
time, not an arbitrary pick.

This backfill uses that last-reading depTime as truth and back-corrects every
earlier reading in the same cluster - both depTime and hoursBeforeDep, so the
two stay consistent with each other (ObservationsBrowser's own depTime
reconstruction assumes they agree). Explicitly not chasing precision far out
- his call: +/-15 min at 4+ hours from departure is fine, this is data
hygiene, not a push for minute-level accuracy where it doesn't matter.

Run once:
    python backfill_deptime_convergence.py            # dry run, prints a summary
    python backfill_deptime_convergence.py --apply     # writes the corrections

Safe to re-run - once applied, every reading in a cluster already matches its
last reading, so a second pass finds nothing left to correct. Rows whose org
isn't in confirmedAirports are skipped (can't compute their true departure
instant) and reported, not silently dropped.

Historical result (2026-09-18 run, full DB): 1458 rows corrected, avg |hbd|
change 0.043 hr (~2.6 min), max 0.6 hr (36 min). No unconfirmed airports hit.
"""

import argparse
import sqlite3
from collections import defaultdict
from datetime import datetime

from clustering import cluster_services
from timezones import et_equivalent_datetime, UnconfirmedAirportError

DB_PATH = '../nonrev.db'
CHECK_TS_FORMAT = '%Y-%m-%d %H:%M'


def compute_updates(conn):
    """
    Returns (updates, skipped_unconfirmed_orgs). updates is a list of
    (new_dep_time, new_hours_before_dep, observationId) ready for
    executemany - only for readings that actually need correcting (their
    stored depTime differs from their cluster's anchor).
    """
    rows = conn.execute(
        """SELECT observationId, org, dest, flightDate, checkTimestamp, depTime, hoursBeforeDep
           FROM observations WHERE depTime IS NOT NULL"""
    ).fetchall()

    by_key = defaultdict(list)
    for obs_id, org, dest, flight_date, check_ts, dep, hbd in rows:
        by_key[(org, dest, flight_date)].append((obs_id, check_ts, dep, hbd))

    updates = []
    skipped_unconfirmed = set()

    for (org, dest, flight_date), readings in by_key.items():
        dep_times = [r[2] for r in readings]
        clusters = cluster_services(dep_times)
        for cluster in clusters:
            if len(set(cluster)) <= 1:
                continue  # no drift in this instance, nothing to correct

            members = [r for r in readings if r[2] in cluster]
            members.sort(key=lambda r: r[1])  # chronological by checkTimestamp
            anchor_dep = members[-1][2]        # last reading's depTime = truth

            flight_date_obj = datetime.strptime(flight_date, '%Y-%m-%d').date()
            try:
                anchor_dt = et_equivalent_datetime(conn, anchor_dep, org, flight_date_obj)
            except UnconfirmedAirportError:
                skipped_unconfirmed.add(org)
                continue

            for obs_id, check_ts, dep, hbd in members:
                if dep == anchor_dep:
                    continue  # already matches the anchor
                check_dt = datetime.strptime(check_ts, CHECK_TS_FORMAT).replace(tzinfo=anchor_dt.tzinfo)
                new_hbd = (anchor_dt - check_dt).total_seconds() / 3600
                updates.append((anchor_dep, round(new_hbd, 2), obs_id))

    return updates, skipped_unconfirmed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true', help='Write the corrections (default is dry run)')
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    updates, skipped_unconfirmed = compute_updates(conn)

    print(f"{len(updates)} reading(s) to back-correct.")
    if skipped_unconfirmed:
        print(f"Skipped (unconfirmed airport code, couldn't resolve): {sorted(skipped_unconfirmed)}")

    if args.apply:
        conn.executemany(
            "UPDATE observations SET depTime=?, hoursBeforeDep=? WHERE observationId=?",
            updates,
        )
        conn.commit()
        print(f"Applied - {len(updates)} row(s) updated.")
    else:
        print("Dry run - nothing written. Re-run with --apply to commit.")
        for dep, hbd, obs_id in updates[:10]:
            print(f"  observationId={obs_id}  ->  depTime={dep}  hoursBeforeDep={hbd}")
        if len(updates) > 10:
            print(f"  ... and {len(updates) - 10} more")

    conn.close()


if __name__ == '__main__':
    main()
