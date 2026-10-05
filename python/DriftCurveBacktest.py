"""
Leave-one-out: does adding the typical move (DriftCurve) to the held reading
call T1 better than plain hold, and is a per-trust-pool move better than one
fleet-wide move? Report only.
"""
import os
import sqlite3
import sys

import numpy as np

from DriftCurve import HORIZON_BANDS, drift_points, reading_near, shifted
from ServiceGrouping import load_open_full_settings
from TrustPools import TrustSnapshot, classify

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'nonrev.db')


def box(seats, full, open_):
    return 'full' if seats <= full else 'open' if seats >= open_ else 'between'


def main():
    if not os.path.exists(DB_PATH):
        sys.exit(f'nonrev.db not found at {DB_PATH}')
    conn = sqlite3.connect(DB_PATH)
    thresholds = load_open_full_settings(conn)
    full, open_ = thresholds['fullThreshold'], thresholds['openThreshold']
    snapshot = TrustSnapshot(conn)
    flights = snapshot.flights
    pool_of = {}
    for flight in flights:
        leaf = classify(snapshot.tree, flight)
        pool_of[id(flight)] = None if leaf is None else snapshot.letters[id(leaf)]

    print(f'{"From":5} {"Days":>5}  {"Hold miss":>9} {"Fleet miss":>10} {"Pool miss":>9}  '
          f'{"Hold right":>10} {"Fleet right":>11} {"Pool right":>10}  open->full: hold/fleet/pool')
    for hours, band in sorted(HORIZON_BANDS.items(), reverse=True):
        rows = []
        for flight in flights:
            held = reading_near(flight, hours, band)
            if held is None:
                continue
            others = [f for f in flights if f is not flight]
            same_pool = [f for f in others if pool_of[id(f)] == pool_of[id(flight)]]
            rows.append((flight['t1'], held,
                         shifted(held, drift_points(others), hours),
                         shifted(held, drift_points(same_pool), hours)))
        truth = np.array([r[0] for r in rows])
        stats = []
        for column in (1, 2, 3):
            guess = np.array([r[column] for r in rows])
            right = np.mean([box(g, full, open_) == box(t, full, open_) for g, t in zip(guess, truth)]) * 100
            misses = sum(box(g, full, open_) == 'open' and box(t, full, open_) == 'full' for g, t in zip(guess, truth))
            stats.append((np.mean(np.abs(guess - truth)), right, misses))
        print(f'T-{hours:<3} {len(rows):5d}  {stats[0][0]:9.2f} {stats[1][0]:10.2f} {stats[2][0]:9.2f}  '
              f'{stats[0][1]:9.1f}% {stats[1][1]:10.1f}% {stats[2][1]:9.1f}%  '
              f'{stats[0][2]}/{stats[1][2]}/{stats[2][2]}')
    print('\nFleet-wide move by distance:', [(h, round(m, 2)) for h, m in drift_points(flights)])


if __name__ == '__main__':
    main()
