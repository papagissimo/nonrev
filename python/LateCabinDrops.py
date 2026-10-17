import os
import sqlite3
import sys
from collections import defaultdict

from observation_filters import not_seat_map_only_where_clause

DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'nonrev.db')
CABINS = ('y', 'cPlus', 'firstOrPS')
COLLAPSE_SEATS = 3


def flights_with_readings(conn):
    flights = defaultdict(list)
    for row in conn.execute(f"""
            SELECT org, dest, flightDate, depTime, hoursBeforeDep, y, cPlus, firstOrPS
            FROM observations
            WHERE hoursBeforeDep IS NOT NULL AND {not_seat_map_only_where_clause()}"""):
        flights[row[:4]].append(row)
    return flights


def compare(flights, early_window, late_window):
    tally = {cabin: defaultdict(int) for cabin in CABINS}
    collapses = []
    paired = 0
    for flight, readings in flights.items():
        early = [r for r in readings if early_window[0] < r['hoursBeforeDep'] <= early_window[1]]
        late = [r for r in readings if late_window[0] < r['hoursBeforeDep'] <= late_window[1]]
        if not early or not late:
            continue
        paired += 1
        start = max(early, key=lambda r: r['hoursBeforeDep'])
        end = min(late, key=lambda r: r['hoursBeforeDep'])
        seats_lost = 0
        for cabin in CABINS:
            if start[cabin] is None or end[cabin] is None:
                continue
            counts = tally[cabin]
            counts['read'] += 1
            change = end[cabin] - start[cabin]
            if change < 0:
                counts['dropped'] += 1
                counts['seatsLost'] -= change
                counts['toZero'] += end[cabin] == 0
                seats_lost -= change
            elif change > 0:
                counts['rose'] += 1
        if seats_lost >= COLLAPSE_SEATS:
            collapses.append((seats_lost, flight, start, end))
    return paired, tally, collapses


def report(label, flights, early_window, late_window):
    paired, tally, collapses = compare(flights, early_window, late_window)
    print(f'{label}: {paired} flights')
    for cabin in CABINS:
        counts = tally[cabin]
        share = 100 * counts['dropped'] / counts['read'] if counts['read'] else 0
        print(f"  {cabin:10s} read {counts['read']:3d}  dropped {counts['dropped']:3d} ({share:.0f}%)"
              f"  to 0 {counts['toZero']:3d}  rose {counts['rose']:3d}  seats lost {counts['seatsLost']:3d}")
    return collapses


def main():
    if not os.path.exists(DB_PATH):
        sys.exit(f'No database at {DB_PATH}')
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    flights = flights_with_readings(conn)
    collapses = report('Late: T-1.5..1.0 -> last reading under T-1', flights, (1.0, 1.55), (0.0, 1.0))
    report('Baseline: T-3..2.5 -> T-2..1.5', flights, (2.5, 3.05), (1.5, 2.0))
    print(f'Under T-0.75: {sum(r["hoursBeforeDep"] <= 0.75 for rs in flights.values() for r in rs)} readings')
    print(f'\nLate collapses of {COLLAPSE_SEATS}+ seats summed:')
    for seats_lost, (org, dest, flight_date, dep_time), start, end in sorted(collapses, key=lambda c: -c[0]):
        cabins = '  '.join(f'{cabin} {start[cabin]}->{end[cabin]}' for cabin in CABINS)
        print(f'  {seats_lost:2d}  {org}-{dest} {flight_date} {dep_time // 60}:{dep_time % 60:02d}'
              f'  T-{start["hoursBeforeDep"]:.2f} -> T-{end["hoursBeforeDep"]:.2f}  {cabins}')


if __name__ == '__main__':
    main()
