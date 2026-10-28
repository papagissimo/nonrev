"""
What a floor reading is worth as a seat count.

A floor reading is a set of route-wide searches ("can N be bought?") plus,
per cabin, the floor: the most that could be bought. The searches are stored
per observation as `searched` ("1,3,6,9"); a cabin's ceiling follows from
them - the next searched number above its floor, less one, or 9 when nothing
above it was searched. A floor of 0 or 9, or a floor that meets its ceiling,
is exact. A floor with no searched list predates the list and searched 1 and
9; an empty list means nothing was searched.

Within an open range the estimate is a weighted average of past exact counts
that fell inside the range. A floor says a little more than its range does -
whether Delta showed "N left at this price" alongside a search - so the
estimate draws first on past readings with the same floor, reached the same
way (searched for, or shown free), whose own searches left at least this
much of the range open, and that were then counted exactly. With too few of
those it falls back to every exact count in the range. Past counts whose
flight's previous exact count sits near this flight's add weight on top.

Rebuilt from the whole table on every load, so it can never be stale.
"""

from collections import Counter, defaultdict

from clustering import cluster_services

FLOOR_COLUMN_FOR = {
    'y': 'floorY',
    'cPlus': 'floorCPlus',
    'firstOrPS': 'floorFirstOrPS',
    'd1': 'floorD1',
}
LEGACY_SEARCHED = frozenset({1, 9})
EXACT_FLOORS = (0, 9)
MAX_SEATS = 9

# Chosen by backtest on the readings through 2026-10-27; results were flat
# within a few hundredths of a seat across nearby values.
MIN_SAME_FLOOR_COUNTS = 10
PREVIOUS_WINDOW = 1
PREVIOUS_WEIGHT = 5


def parse_searched(text):
    """The searched numbers as a frozenset; LEGACY_SEARCHED for NULL."""
    if text is None:
        return LEGACY_SEARCHED
    return frozenset(int(part) for part in str(text).split(',') if part.strip())


def format_searched(numbers):
    return ','.join(str(n) for n in sorted(numbers))


def ceiling_for(floor, searched):
    if floor in EXACT_FLOORS:
        return floor
    above = [n for n in searched if n > floor]
    return min(above) - 1 if above else MAX_SEATS


def exact_value(actual, floor, searched):
    """The cabin's exact count on this reading, or None."""
    if actual is not None:
        return int(actual)
    if floor is not None and ceiling_for(floor, searched) == floor:
        return int(floor)
    return None


def floor_kind(floor, searched):
    return 'searched' if floor in searched else 'free'


class FloorModel:
    """records: (cabin, floor, searched, exact, previous) per cabin per
    reading - floor None when not taken, exact None when not known,
    previous the flight's latest earlier exact count or None."""

    def __init__(self, records):
        self.counted = defaultdict(Counter)
        self.same_floor_counted = defaultdict(Counter)
        for cabin, floor, searched, exact, previous in records:
            if exact is None:
                continue
            self.counted[cabin][(exact, previous)] += 1
            if floor is not None and floor not in EXACT_FLOORS:
                key = (cabin, floor, floor_kind(floor, searched))
                self.same_floor_counted[key][(exact, previous, ceiling_for(floor, searched))] += 1

    def _pool(self, cabin, floor, searched, high):
        """{(seats, previous): count} within floor..high, from the right pool."""
        same = Counter()
        for (seats, previous, own_ceiling), count in \
                self.same_floor_counted[(cabin, floor, floor_kind(floor, searched))].items():
            if own_ceiling >= high and floor <= seats <= high:
                same[(seats, previous)] += count
        if sum(same.values()) >= MIN_SAME_FLOOR_COUNTS:
            return same
        return {key: count for key, count in self.counted[cabin].items() if floor <= key[0] <= high}

    def distribution(self, cabin, floor, searched, previous=None):
        """{seats: probability} for the cabin's true count."""
        high = ceiling_for(floor, searched)
        if high == floor:
            return {floor: 1.0}
        pool = self._pool(cabin, floor, searched, high)
        seats = range(floor, high + 1)
        totals = {s: 0 for s in seats}
        near = {s: 0 for s in seats}
        for (s, prev), count in pool.items():
            totals[s] += count
            if previous is not None and prev is not None and abs(prev - previous) <= PREVIOUS_WINDOW:
                near[s] += count
        grand = sum(totals.values())
        base = ({s: totals[s] / grand for s in seats} if grand
                else {s: 1.0 / len(seats) for s in seats})
        if previous is None:
            return base
        weight = sum(near.values()) + PREVIOUS_WEIGHT
        return {s: (near[s] + PREVIOUS_WEIGHT * base[s]) / weight for s in seats}

    def estimate(self, cabin, floor, searched, previous=None):
        return sum(s * p for s, p in self.distribution(cabin, floor, searched, previous).items())

    def page_table(self):
        """Every open distribution the logging dialog can need, for the
        browser to read its estimates and hint from: cabin -> floor ->
        ceiling -> 'free'/'searched' -> previous ('none' or 0-9) -> the
        probabilities of floor..ceiling in order."""
        table = {}
        for cabin in FLOOR_COLUMN_FOR:
            for floor in range(1, MAX_SEATS):
                for high in range(floor + 1, MAX_SEATS + 1):
                    for kind in ('free', 'searched'):
                        searched = (frozenset({high + 1}) if kind == 'free'
                                    else frozenset({floor, high + 1}))
                        by_previous = (table.setdefault(cabin, {}).setdefault(str(floor), {})
                                       .setdefault(str(high), {}).setdefault(kind, {}))
                        for previous in [None] + list(range(MAX_SEATS + 1)):
                            dist = self.distribution(cabin, floor, searched, previous)
                            by_previous['none' if previous is None else str(previous)] = [
                                round(dist[s], 4) for s in range(floor, high + 1)]
        return table


def flight_groups(rows):
    """rows: dicts with carrier/org/dest/flightDate/depTime/checkTimestamp.
    Yields each flight's rows oldest first, same-day depTimes clustered so
    a schedule correction doesn't split one flight."""
    by_route_day = defaultdict(list)
    for row in rows:
        if row['depTime'] is not None:
            by_route_day[(row['carrier'], row['org'], row['dest'], row['flightDate'])].append(row)
    for day_rows in by_route_day.values():
        for cluster in cluster_services({r['depTime'] for r in day_rows}):
            members = set(cluster)
            yield sorted((r for r in day_rows if r['depTime'] in members), key=lambda r: r['checkTimestamp'])


def reading_values(rows):
    """For one flight's rows oldest first: per row, per cabin,
    (floor, searched, exact, previous) - previous is the latest exact
    count from an earlier sitting."""
    latest = {}
    this_sitting = {}
    sitting = None
    result = []
    for row in rows:
        if row['checkTimestamp'] != sitting:
            latest.update(this_sitting)
            this_sitting = {}
            sitting = row['checkTimestamp']
        searched = parse_searched(row['searched'])
        values = {}
        for cabin, floor_col in FLOOR_COLUMN_FOR.items():
            floor = None if row[floor_col] is None else int(row[floor_col])
            values[cabin] = (floor, searched, exact_value(row[cabin], floor, searched), latest.get(cabin))
        for cabin, (_, _, exact, _) in values.items():
            if exact is not None:
                this_sitting[cabin] = exact
        result.append(values)
    return result


def cabin_seats(model, cabin, values):
    """(seats, estimated) for one cabin of one reading - the exact count,
    else the floor's estimate, else (None, False) when nothing was taken."""
    floor, searched, exact, previous = values
    if exact is not None:
        return exact, False
    if floor is None:
        return None, False
    return model.estimate(cabin, floor, searched, previous), True


def load_reading_rows(conn):
    cursor = conn.execute(
        f"""SELECT carrier, org, dest, flightDate, depTime, checkTimestamp, searched,
                   {', '.join(FLOOR_COLUMN_FOR)}, {', '.join(FLOOR_COLUMN_FOR.values())}
            FROM observations"""
    )
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, values)) for values in cursor.fetchall()]


def records_from_rows(rows):
    for flight_rows in flight_groups(rows):
        for values in reading_values(flight_rows):
            for cabin, (floor, searched, exact, previous) in values.items():
                yield cabin, floor, searched, exact, previous


def load_floor_model(conn):
    return FloorModel(records_from_rows(load_reading_rows(conn)))
