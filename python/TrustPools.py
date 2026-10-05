"""
Trust pools: weekly services grouped by how their readings settle, never by
how their flights end.

A reading has a move that matters still to come when it sits farther from
the flight's golden-ticket total than the cadence yardstick allows at the
reading's own total, in either direction. A flight's settling distance is
its closest-in such reading; a flight with none is known only to have
settled at or beyond its farthest reading.

Each flight in a node gets a log-rank score: whether it was seen unsettled,
less the node's Nelson-Aalen cumulative hazard at its settling distance.
The tree splits on weekday, daily service or weekly service when the scores
differ across that attribute's levels by more than shuffled level labels
manage, at SHUFFLES shuffles, clearing SPLIT_BAR after allowing for the
attributes tried. The levels are then cut in two where the scores separate
most, keeping at least MIN_POOL_FLIGHTS flights on each side. Each
pool keeps its curve of the share of flights with a move that matters still
to come, by distance, and pools are lettered A onward from the steadiest.

A flight's cadence state comes from its pool's curve: the points a reading
now would buy are the curve at the last reading's distance less the curve
now. The leadoff and the golden ticket are always called for, since the
study needs them whatever the curve says. While he's away, a flight
departing before he's back (plus a margin) is Last: the reading now is the
last one it will get.
"""
from bisect import bisect_left
from collections import defaultdict
from datetime import date, datetime

import numpy as np
from lifelines import NelsonAalenFitter

from DeclineCurveFit import build_service_map
from FloorEstimates import load_floor_estimates, seats_from_glance
from PoolingSettingsDialog import excluded_date_where_clause
from T1GridReport import format_minutes, scheduled_service_finder
from observation_filters import not_seat_map_only_where_clause
from settings import load_settings

SPLIT_BAR = 0.01
SHUFFLES = 9999
MIN_POOL_FLIGHTS = 30
CURVE_HOURS = [48, 24, 12, 8, 6, 4, 3, 2]
GLANCE_COLUMNS = {'y': 'cheapY', 'cPlus': 'cheapCPlus', 'firstOrPS': 'cheapFirstOrPS'}
T1_RANGE_QUANTILES = (0.1, 0.9)
T1_RANGE_LEVEL_SPREAD = 3.0
T1_RANGE_MIN_FLIGHTS = 10

ATTRIBUTES = {
    'weekday': lambda f: f['day'],
    'daily service': lambda f: f"{f['org'].upper()}-{f['dest'].upper()} {format_minutes(f['serviceMinutes'])}",
    'weekly service': lambda f: (f"{f['org'].upper()}-{f['dest'].upper()} {f['day']} "
                                 f"{format_minutes(f['serviceMinutes'])}"),
}


def seat_value(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def reading_total(row, floor_estimates):
    total = 0.0
    for actual_col, glance_col in GLANCE_COLUMNS.items():
        actual = seat_value(row[actual_col])
        glance = seat_value(row[glance_col])
        if actual is not None:
            total += actual
        elif glance is not None:
            total += seats_from_glance(floor_estimates, actual_col, int(glance))
        else:
            return None
    return total


def load_rows(conn):
    cursor = conn.execute(
        f"""SELECT org, dest, flightDate, depTime, hoursBeforeDep,
                   y, cPlus, firstOrPS, cheapY, cheapCPlus, cheapFirstOrPS
            FROM observations
            WHERE depTime IS NOT NULL AND hoursBeforeDep IS NOT NULL
              AND {excluded_date_where_clause()} AND {not_seat_map_only_where_clause()}"""
    )
    names = [column[0] for column in cursor.description]
    rows = []
    for values in cursor.fetchall():
        row = dict(zip(names, values))
        try:
            row['dayOfWeek'] = datetime.strptime(row['flightDate'], '%Y-%m-%d').strftime('%a')
        except (TypeError, ValueError):
            continue
        rows.append(row)
    return rows


def parse_yardstick(text):
    points = sorted(tuple(float(part) for part in pair.split()) for pair in text.split(',') if pair.strip())
    totals, moves = zip(*points)
    return lambda total: float(np.interp(total, totals, moves))


def matters(reading_total, t1, yardstick):
    return abs(reading_total - t1) > yardstick(reading_total)


def settling(readings, golden_ticket_hours, yardstick):
    truth = [reading for reading in readings if reading[0] <= golden_ticket_hours]
    early = sorted(reading for reading in readings if reading[0] > golden_ticket_hours)
    if not truth or not early:
        return None
    t1 = min(truth)[1]
    outside = [hours for hours, seats in early if matters(seats, t1, yardstick)]
    settle = min(outside) if outside else max(hours for hours, _ in early)
    return {'settle': settle, 'seenUnsettled': bool(outside), 'early': early, 't1': t1}


def settled_flights(conn, golden_ticket_hours, yardstick):
    rows = load_rows(conn)
    floor_estimates = load_floor_estimates(conn)
    dep_time_to_service, service_info = build_service_map(rows)
    readings_by_flight = defaultdict(list)
    for row in rows:
        total = reading_total(row, floor_estimates)
        service_id = dep_time_to_service.get((row['org'], row['dest'], row['dayOfWeek'], row['depTime']))
        if total is None or service_id is None:
            continue
        readings_by_flight[(service_id, row['flightDate'])].append((float(row['hoursBeforeDep']), total))

    finders = {}
    flights = []
    for (service_id, flight_date), readings in readings_by_flight.items():
        result = settling(readings, golden_ticket_hours, yardstick)
        if result is None:
            continue
        org, dest, day, rep_minutes, _size = service_info[service_id]
        if (org, dest, day) not in finders:
            finders[(org, dest, day)] = scheduled_service_finder(conn, org, dest, day)
        service_minutes = finders[(org, dest, day)](rep_minutes)
        flights.append({'org': org, 'dest': dest, 'day': day, 'flightDate': flight_date,
                        'serviceMinutes': rep_minutes if service_minutes is None else service_minutes,
                        **result})
    return flights


def any_unsettled(flights):
    return any(f['seenUnsettled'] for f in flights)


def logrank_scores(flights):
    settles = np.array([f['settle'] for f in flights])
    seen = np.array([f['seenUnsettled'] for f in flights], dtype=float)
    hazard = NelsonAalenFitter().fit(settles, event_observed=seen)
    return seen - hazard.cumulative_hazard_at_times(settles).to_numpy()


def separation(scores, labels, group_count):
    sums = np.bincount(labels, weights=scores, minlength=group_count)
    sizes = np.bincount(labels, minlength=group_count)
    return float(np.sum(sums ** 2 / sizes))


def shuffled_p(scores, labels, group_count, rng):
    observed = separation(scores, labels, group_count)
    beaten = sum(separation(rng.permutation(scores), labels, group_count) >= observed for _ in range(SHUFFLES))
    return (1 + beaten) / (1 + SHUFFLES)


def by_level(flights, attribute):
    levels = defaultdict(list)
    for flight in flights:
        levels[ATTRIBUTES[attribute](flight)].append(flight)
    return levels


def level_labels(flights, attribute):
    names = sorted({ATTRIBUTES[attribute](f) for f in flights})
    index = {name: number for number, name in enumerate(names)}
    return np.array([index[ATTRIBUTES[attribute](f)] for f in flights]), names


def best_cut(flights, scores, attribute):
    labels, names = level_labels(flights, attribute)
    sizes = np.bincount(labels, minlength=len(names))
    means = np.bincount(labels, weights=scores, minlength=len(names)) / sizes
    ordered = list(np.argsort(means))
    best = None
    for cut in range(1, len(ordered)):
        steady = set(ordered[:cut])
        side = np.array([label in steady for label in labels], dtype=int)
        if min(side.sum(), len(side) - side.sum()) < MIN_POOL_FLIGHTS:
            continue
        strength = separation(scores, side, 2)
        if best is None or strength > best['strength']:
            best = {'strength': strength,
                    'lowLevels': [names[i] for i in ordered[:cut]],
                    'highLevels': [names[i] for i in ordered[cut:]]}
    if best is None:
        return None
    low = set(best['lowLevels'])
    best['low'] = [f for f in flights if ATTRIBUTES[attribute](f) in low]
    best['high'] = [f for f in flights if ATTRIBUTES[attribute](f) not in low]
    return best


def choose_split(flights, rng):
    if len(flights) < 2 * MIN_POOL_FLIGHTS or not any_unsettled(flights):
        return None
    scores = logrank_scores(flights)
    candidates = {}
    for attribute in ATTRIBUTES:
        labels, names = level_labels(flights, attribute)
        if len(names) < 2:
            continue
        cut = best_cut(flights, scores, attribute)
        if cut is not None:
            candidates[attribute] = (shuffled_p(scores, labels, len(names), rng), cut)
    if not candidates:
        return None
    attribute = min(candidates, key=lambda a: candidates[a][0])
    p, cut = candidates[attribute]
    adjusted = min(1.0, p * len(candidates))
    if adjusted >= SPLIT_BAR:
        return None
    return {'attribute': attribute, 'adjustedP': adjusted, **cut}


def grow(flights, path, rng):
    split = choose_split(flights, rng)
    if split is None:
        return {'path': path, 'flights': flights}
    return {
        'path': path, 'flights': flights, 'split': split,
        'children': [grow(split['low'], path + [(split['attribute'], split['lowLevels'])], rng),
                     grow(split['high'], path + [(split['attribute'], split['highLevels'])], rng)],
    }


def leaves(node):
    if 'children' not in node:
        return [node]
    return [leaf for child in node['children'] for leaf in leaves(child)]


def classify(node, flight):
    while 'children' in node:
        value = ATTRIBUTES[node['split']['attribute']](flight)
        if value in node['split']['lowLevels']:
            node = node['children'][0]
        elif value in node['split']['highLevels']:
            node = node['children'][1]
        else:
            return None
    return node


def still_to_come_curve(flights, yardstick):
    """Percent of flights whose latest reading at each distance in CURVE_HOURS
    has a move that matters still to come; None where no flight was read that
    far out."""
    curve = []
    for hours in CURVE_HOURS:
        held = [(min(r for r in f['early'] if r[0] >= hours), f['t1'])
                for f in flights if any(r[0] >= hours for r in f['early'])]
        curve.append(100 * sum(matters(reading[1], t1, yardstick) for reading, t1 in held) / len(held)
                     if held else None)
    return curve


def still_to_come(curve, hours):
    points = [(h, v) for h, v in zip(CURVE_HOURS, curve) if v is not None][::-1]
    return np.interp(hours, [h for h, _ in points], [v for _, v in points])


def still_to_come_at(curve, hours):
    return float(still_to_come(curve, hours))


def held_reading(flight, hours):
    early = flight['early']
    index = bisect_left(early, (hours, float('-inf')))
    return early[index][1] if index < len(early) else None


def t1_range(flights, hours, held):
    """Where flights like this one ended: the T1_RANGE_QUANTILES of past
    flights' moves from their reading held at this distance to their T1,
    each flight weighted by how close its held total sits to this one's,
    added to this held total. None when the weights add up to fewer than
    T1_RANGE_MIN_FLIGHTS flights' worth."""
    moves, weights = [], []
    for flight in flights:
        past_held = held_reading(flight, hours)
        if past_held is None:
            continue
        moves.append(flight['t1'] - past_held)
        weights.append(np.exp(-0.5 * ((past_held - held) / T1_RANGE_LEVEL_SPREAD) ** 2))
    if not weights:
        return None
    weights = np.array(weights)
    if weights.sum() ** 2 / np.sum(weights ** 2) < T1_RANGE_MIN_FLIGHTS:
        return None
    low, high = np.quantile(moves, T1_RANGE_QUANTILES, weights=weights, method='inverted_cdf')
    return {'lo': max(0.0, held + float(low)), 'hi': held + float(high)}


def cadence_reading(reading_hours, hours_until_dep, golden_ticket_hours, curve, now_points, skip_points,
                    last_chance_hours=None):
    """(state, urgency). Urgency sorts most pressing first: Last (never read,
    by soonest departure; then points; then no pool), then Now! by unread
    golden-ticket window (soonest departure), then points, then leadoff
    (soonest departure); Meh by points, then no pool; Skip by departure.
    Last is a flight departing within last_chance_hours, while he's away
    until then: the reading now is the last one he'll get."""
    if any(hours <= golden_ticket_hours for hours in reading_hours):
        return 'skip', (2, 0, hours_until_dep)
    if last_chance_hours is not None and hours_until_dep <= last_chance_hours:
        if not reading_hours:
            return 'last', (0, -1, 0, hours_until_dep)
        if curve is None:
            return 'last', (0, -1, 2, hours_until_dep)
        return 'last', (0, -1, 1, -points_now(reading_hours, hours_until_dep, curve))
    if not reading_hours:
        return 'now', (0, 2, hours_until_dep)
    if hours_until_dep <= golden_ticket_hours:
        return 'now', (0, 0, hours_until_dep)
    if curve is None:
        return 'meh', (1, 1, hours_until_dep)
    gain = points_now(reading_hours, hours_until_dep, curve)
    if gain >= now_points:
        return 'now', (0, 1, -gain)
    if gain >= skip_points:
        return 'meh', (1, 0, -gain)
    return 'skip', (2, 0, hours_until_dep)


def points_now(reading_hours, hours_until_dep, curve):
    return still_to_come_at(curve, min(reading_hours)) - still_to_come_at(curve, hours_until_dep)


def cadence_state(reading_hours, hours_until_dep, golden_ticket_hours, curve, now_points, skip_points):
    return cadence_reading(reading_hours, hours_until_dep, golden_ticket_hours, curve, now_points, skip_points)[0]


def next_now_hours(reading_hours, hours_until_dep, golden_ticket_hours, curve, now_points):
    """Hours before departure at which the flight turns Now!, to the minute:
    hours_until_dep when it already is, None once the golden ticket is in."""
    if any(hours <= golden_ticket_hours for hours in reading_hours):
        return None
    if not reading_hours or hours_until_dep <= golden_ticket_hours:
        return hours_until_dep
    if curve is None:
        return golden_ticket_hours
    target = still_to_come_at(curve, min(reading_hours)) - now_points
    hours = np.arange(hours_until_dep, golden_ticket_hours, -1 / 60)
    reached = hours[still_to_come(curve, hours) <= target]
    return float(reached[0]) if reached.size else golden_ticket_hours


class TrustSnapshot:
    def __init__(self, conn):
        settings = load_settings(conn)
        self.golden_ticket_hours = settings['goldenTicketHours']
        self.yardstick = parse_yardstick(settings['cadenceYardstick'])
        self.flights = settled_flights(conn, self.golden_ticket_hours, self.yardstick)
        self.tree = grow(self.flights, [], np.random.default_rng(0))
        self.pool_leaves = leaves(self.tree)
        self.curves = {id(leaf): still_to_come_curve(leaf['flights'], self.yardstick) for leaf in self.pool_leaves}
        steadiest_first = sorted(self.pool_leaves, key=lambda leaf: still_to_come_at(self.curves[id(leaf)], 6))
        self.letters = {id(leaf): chr(ord('A') + rank) for rank, leaf in enumerate(steadiest_first)}
        self.finders = {}

    def weekly_service(self, conn, org, dest, day, dep_time):
        if (org, dest, day) not in self.finders:
            self.finders[(org, dest, day)] = scheduled_service_finder(conn, org, dest, day)
        service_minutes = self.finders[(org, dest, day)](dep_time)
        return {'org': org, 'dest': dest, 'day': day,
                'serviceMinutes': dep_time if service_minutes is None else service_minutes}

    def pool_record(self, conn, org, dest, day, dep_time):
        leaf = classify(self.tree, self.weekly_service(conn, org, dest, day, dep_time))
        if leaf is None:
            return None
        return {'label': self.letters[id(leaf)], 'curve': self.curves[id(leaf)]}

    def t1_range(self, conn, org, dest, day, dep_time, flight_date, hours, held):
        leaf = classify(self.tree, self.weekly_service(conn, org, dest, day, dep_time))
        if leaf is None:
            return None
        others = [f for f in leaf['flights']
                  if (f['org'], f['dest'], f['flightDate']) != (org, dest, flight_date)]
        return t1_range(others, hours, held)


_cached = {}


def snapshot(conn):
    settings = load_settings(conn)
    key = (date.today(), settings['goldenTicketHours'], settings['cadenceYardstick'])
    if key not in _cached:
        _cached.clear()
        _cached[key] = TrustSnapshot(conn)
    return _cached[key]
