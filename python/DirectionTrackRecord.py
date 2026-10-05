"""
Does a service's own track record say which way its next flight moves?
Each flight read at MIN_SEATS or more near a horizon is labeled by the mean
move to T1 of its service's OTHER flights from that horizon: decliner at
-TRACK_CUT or below, riser at +TRACK_CUT or above, else steady. Reports how
each group's held-out flights actually moved, and how often shuffled
service labels open as wide a riser-minus-decliner gap. Report only.
"""
import os
import sqlite3
import sys
from collections import Counter, defaultdict

import numpy as np

from DriftCurve import HORIZON_BANDS, reading_near
from TrustPools import TrustSnapshot

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'nonrev.db')
MIN_SEATS = 3
TRACK_CUT = 2.0
MOVE_THAT_COUNTS = 3
SHUFFLES = 1000
USABLE_FLIGHTS = 5
SERVICE_KEYS = {
    'weekly service': lambda f: (f['org'], f['dest'], f['day'], f['serviceMinutes']),
    'daily service': lambda f: (f['org'], f['dest'], f['serviceMinutes']),
}


def groups_by_record(moves_by_service):
    groups = defaultdict(list)
    for moves in moves_by_service.values():
        for index, move in enumerate(moves):
            others = moves[:index] + moves[index + 1:]
            if not others:
                continue
            record = np.mean(others)
            label = 'decliner' if record <= -TRACK_CUT else 'riser' if record >= TRACK_CUT else 'steady'
            groups[label].append(move)
    return groups


def gap(groups):
    if not groups['decliner'] or not groups['riser']:
        return np.nan
    return np.mean(groups['riser']) - np.mean(groups['decliner'])


def regroup(keys, moves):
    moves_by_service = defaultdict(list)
    for key, move in zip(keys, moves):
        moves_by_service[key].append(move)
    return moves_by_service


def main():
    if not os.path.exists(DB_PATH):
        sys.exit(f'nonrev.db not found at {DB_PATH}')
    flights = TrustSnapshot(sqlite3.connect(DB_PATH)).flights
    rng = np.random.default_rng(0)
    for hours, band in sorted(HORIZON_BANDS.items(), reverse=True):
        for name, key_of in SERVICE_KEYS.items():
            keys, moves = [], []
            for flight in flights:
                held = reading_near(flight, hours, band)
                if held is not None and held >= MIN_SEATS:
                    keys.append(key_of(flight))
                    moves.append(flight['t1'] - held)
            usable = sum(count >= USABLE_FLIGHTS for count in Counter(keys).values())
            groups = groups_by_record(regroup(keys, moves))
            observed = gap(groups)
            shuffled = np.array([gap(groups_by_record(regroup([keys[i] for i in rng.permutation(len(keys))], moves)))
                                 for _ in range(SHUFFLES)])
            luck = np.nanmean(shuffled >= observed) * 100 if not np.isnan(observed) else float('nan')
            print(f'T-{hours}, by {name}: {usable} services with {USABLE_FLIGHTS}+ usable flights; '
                  f'riser minus decliner {observed:+.1f} seats, shuffles match it {luck:.0f}% of the time')
            for label in ('decliner', 'steady', 'riser'):
                group = np.array(groups[label])
                if len(group):
                    print(f'   {label:9} {len(group):4d} flights  avg {group.mean():+.1f}  '
                          f'drop {MOVE_THAT_COUNTS}+ {np.mean(group <= -MOVE_THAT_COUNTS) * 100:3.0f}%  '
                          f'rise {MOVE_THAT_COUNTS}+ {np.mean(group >= MOVE_THAT_COUNTS) * 100:3.0f}%')


if __name__ == '__main__':
    main()
