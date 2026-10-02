"""
Pools: weekly services grouped because they behave alike, recomputed from
the logged data rather than stored. One flight is one dated departure whose
live T1 comes from a reading within LAST_READING_MAX_HOURS of departure.

The pool tree splits on whichever of weekday, daily service or weekly
service separates its flights most, while that clears SPLIT_BAR after
allowing for the attributes tried, and repeats inside each side.

Everything the logging dialog needs from the pools comes from one
PoolSnapshot, built once per day and per open/full setting.
"""
from collections import defaultdict
from datetime import date

from bisect import bisect_right

from scipy.optimize import isotonic_regression
from scipy.stats import binom, kruskal, mannwhitneyu

from DeclineCurveFit import build_service_map, gather_instances, load_observations, nearest_reading
from PoolingSettingsDialog import excluded_date_where_clause
from ServiceGrouping import load_open_full_settings
from T1GridReport import (
    DAYS_OF_WEEK, format_minutes, last_t1_instances, routes_with_history, scheduled_service_finder,
)
from clustering import cluster_services
from observation_filters import not_seat_map_only_where_clause

LAST_READING_MAX_HOURS = 6.0
SPLIT_BAR = 0.01
TRUTH_WINDOW_HOURS = 1.55
TRUST_TOLERANCE = 1 / 50
TRUST_BAR = 0.01
GOLD_MAX_FULL_RATE = 0.05
TRUST_CABINS = ['y', 'cPlus', 'firstOrPS']
HISTORY_CABIN_COLUMNS = {'y': 'y', 'cplus': 'cPlus', 'onePS': 'firstOrPS', 'd1': 'd1'}
HISTORY_EDGE_TOLERANCE_HOURS = 1.0
WEEKDAY_NAMES = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
DAY_ABBREVIATIONS = {'Sun': 'Su', 'Mon': 'M', 'Tue': 'Tu', 'Wed': 'W', 'Thu': 'Th', 'Fri': 'F', 'Sat': 'Sa'}

ATTRIBUTES = {
    'weekday': lambda f: f['day'],
    'daily service': lambda f: f"{f['org'].upper()}-{f['dest'].upper()} {format_minutes(f['serviceMinutes'])}",
    'weekly service': lambda f: (f"{f['org'].upper()}-{f['dest'].upper()} {f['day']} "
                                 f"{format_minutes(f['serviceMinutes'])}"),
}


def logged_flights(conn, open_threshold):
    flights = []
    for route in routes_with_history(conn):
        org, dest = route['org'], route['dest']
        for day in DAYS_OF_WEEK:
            find_scheduled_service = scheduled_service_finder(conn, org, dest, day)
            for instance in last_t1_instances(conn, org, dest, day):
                if instance['t1'] is None or instance['lastReadingHours'] > LAST_READING_MAX_HOURS:
                    continue
                service_minutes = find_scheduled_service(instance['depTime'])
                if service_minutes is None:
                    service_minutes = instance['ownServiceMinutes']
                flights.append({
                    'org': org, 'dest': dest, 'day': day, 'serviceMinutes': service_minutes,
                    'flightDate': instance['flightDate'],
                    'cappedT1': min(instance['t1'], open_threshold),
                })
    return flights


def capped_t1s(flights):
    return [flight['cappedT1'] for flight in flights]


def by_level(flights, attribute):
    levels = defaultdict(list)
    for flight in flights:
        levels[ATTRIBUTES[attribute](flight)].append(flight)
    return levels


def attribute_association(flights, attribute):
    levels = by_level(flights, attribute)
    if len(levels) < 2:
        return None
    samples = [capped_t1s(members) for members in levels.values()]
    if len({value for sample in samples for value in sample}) < 2:
        return None
    return kruskal(*samples).pvalue


def best_cut(flights, attribute):
    levels = by_level(flights, attribute)
    ordered = sorted(levels, key=lambda level: sum(capped_t1s(levels[level])) / len(levels[level]))
    best = None
    for cut in range(1, len(ordered)):
        low_levels, high_levels = ordered[:cut], ordered[cut:]
        low = [flight for level in low_levels for flight in levels[level]]
        high = [flight for level in high_levels for flight in levels[level]]
        p = mannwhitneyu(capped_t1s(low), capped_t1s(high), alternative='two-sided').pvalue
        if best is None or p < best['p']:
            best = {'p': p, 'lowLevels': low_levels, 'highLevels': high_levels, 'low': low, 'high': high}
    return best


def choose_split(flights):
    associations = {attribute: attribute_association(flights, attribute) for attribute in ATTRIBUTES}
    tested = {attribute: p for attribute, p in associations.items() if p is not None}
    if not tested:
        return None
    attribute = min(tested, key=tested.get)
    adjusted = min(1.0, tested[attribute] * len(tested))
    if adjusted >= SPLIT_BAR:
        return None
    cut = best_cut(flights, attribute)
    return {'attribute': attribute, 'adjustedP': adjusted, **cut}


def grow(flights, path):
    split = choose_split(flights)
    if split is None:
        return {'path': path, 'flights': flights}
    low_step = (split['attribute'], split['lowLevels'], split['highLevels'], split['adjustedP'])
    high_step = (split['attribute'], split['highLevels'], split['lowLevels'], split['adjustedP'])
    return {
        'path': path, 'flights': flights, 'split': split,
        'children': [grow(split['low'], path + [low_step]), grow(split['high'], path + [high_step])],
    }


def leaves(node):
    if 'children' not in node:
        return [node]
    return [leaf for child in node['children'] for leaf in leaves(child)]


def rate(values, test):
    return sum(1 for value in values if test(value)) / len(values)


def box(seats, thresholds):
    if seats <= thresholds['fullThreshold']:
        return 'full'
    if seats >= thresholds['openThreshold']:
        return 'open'
    return 'between'


def leaf_days(leaf):
    weekday_steps = [levels for attribute, levels, _sibling, _p in leaf['path'] if attribute == 'weekday']
    if not weekday_steps:
        return ''
    days = set(weekday_steps[-1])
    return ''.join(DAY_ABBREVIATIONS[day] for day in DAY_ABBREVIATIONS if day in days)


def pool_roles(pool_leaves, thresholds):
    full_rates = [rate(capped_t1s(leaf['flights']), lambda v: box(v, thresholds) == 'full') for leaf in pool_leaves]
    fullest = max(range(len(pool_leaves)), key=full_rates.__getitem__)
    return ['BadBoy' if number == fullest else 'Golden' if full_rate <= GOLD_MAX_FULL_RATE else 'Never Know'
            for number, full_rate in enumerate(full_rates)]


def pool_names(pool_leaves, roles):
    names = []
    for leaf, role in zip(pool_leaves, roles):
        days = leaf_days(leaf) if roles.count(role) > 1 else ''
        names.append(f'{role} {days}' if days else role)
    return names


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


def open_full_counts(flights, thresholds):
    boxes = [box(value, thresholds) for value in capped_t1s(flights)]
    return {'measured': len(boxes), 'open': boxes.count('open'), 'full': boxes.count('full')}


def format_open_full(counts):
    measured = counts['measured']
    return {
        'measured': measured,
        'openDisplay': f"{counts['open']}/{measured}" if measured else '\u2014',
        'fullDisplay': f"{counts['full']}/{measured}" if measured else '\u2014',
    }


def latest_known(readings, hours):
    earlier = [reading for reading in readings if reading[0] >= hours]
    return min(earlier, key=lambda reading: reading[0])[1] if earlier else None


def open_readings_by_leaf(conn, tree, thresholds):
    rows, _dropped = load_observations(conn)
    dep_time_to_service, service_info = build_service_map(rows)
    instances_by_cabin = [gather_instances(rows, dep_time_to_service, cabin) for cabin in TRUST_CABINS]
    finders = {}
    readings_by_leaf = defaultdict(list)
    for key in set.intersection(*(set(instances) for instances in instances_by_cabin)):
        service_id, _flight_date = key
        org, dest, day, rep_minutes, _size = service_info[service_id]
        if (org, dest, day) not in finders:
            finders[(org, dest, day)] = scheduled_service_finder(conn, org, dest, day)
        service_minutes = finders[(org, dest, day)](rep_minutes)
        flight = {'org': org, 'dest': dest, 'day': day,
                  'serviceMinutes': rep_minutes if service_minutes is None else service_minutes}
        leaf = classify(tree, flight)
        if leaf is None:
            continue
        per_cabin = [instances[key]['readings'] for instances in instances_by_cabin]
        truths = [nearest_reading(readings, 1.0) for readings in per_cabin]
        if any(truth is None or truth[0] > TRUTH_WINDOW_HOURS for truth in truths):
            continue
        ended_full = box(sum(truth[1] for truth in truths), thresholds) == 'full'
        early = [[reading for reading in readings if reading[0] > TRUTH_WINDOW_HOURS] for readings in per_cabin]
        for hours in sorted({reading[0] for readings in early for reading in readings}):
            held = [latest_known(readings, hours) for readings in early]
            if None not in held and box(sum(held), thresholds) == 'open':
                readings_by_leaf[id(leaf)].append((hours, ended_full))
    return readings_by_leaf


def risk_curve(open_readings):
    outcomes_by_hours = defaultdict(list)
    for hours, full in open_readings:
        outcomes_by_hours[hours].append(full)
    hours = sorted(outcomes_by_hours)
    rates = [sum(outcomes_by_hours[h]) / len(outcomes_by_hours[h]) for h in hours]
    counts = [len(outcomes_by_hours[h]) for h in hours]
    fitted = isotonic_regression(rates, weights=counts, increasing=True).x

    def risk_at(at_hours):
        position = min(max(bisect_right(hours, at_hours) - 1, 0), len(hours) - 1)
        return float(fitted[position])

    return risk_at


def trust_horizon(open_readings, tolerance):
    risk_at = risk_curve(open_readings)
    safe = [(hours, full) for hours, full in open_readings if risk_at(hours) <= tolerance]
    if not safe:
        return None
    horizon = max(hours for hours, _ in safe)
    if horizon == max(hours for hours, _ in open_readings):
        return horizon
    pool_rate = sum(full for _, full in open_readings) / len(open_readings)
    chance_of_so_few_by_luck = binom.cdf(sum(full for _, full in safe), len(safe), pool_rate)
    return horizon if chance_of_so_few_by_luck < TRUST_BAR else None


def logged_service_containing(dep_time, logged_times):
    clusters = cluster_services(sorted(set(logged_times) | {dep_time}))
    return set(next(cluster for cluster in clusters if dep_time in cluster))


def value_at_hours(points, hours):
    for (h_near, v_near), (h_far, v_far) in zip(points, points[1:]):
        if h_near <= hours <= h_far:
            if h_far == h_near:
                return v_near
            return v_near + (v_far - v_near) * (hours - h_near) / (h_far - h_near)
    nearest_hours, nearest_value = min(points, key=lambda p: abs(p[0] - hours))
    if abs(nearest_hours - hours) <= HISTORY_EDGE_TOLERANCE_HOURS:
        return nearest_value
    return None


class PoolSnapshot:
    def __init__(self, conn):
        self.thresholds = load_open_full_settings(conn)
        self.flights = logged_flights(conn, self.thresholds['openThreshold'])
        self.tree = grow(self.flights, [])
        self.pool_leaves = leaves(self.tree)
        roles = pool_roles(self.pool_leaves, self.thresholds)
        self.roles = dict(zip(map(id, self.pool_leaves), roles))
        self.names = dict(zip(map(id, self.pool_leaves), pool_names(self.pool_leaves, roles)))
        open_readings = open_readings_by_leaf(conn, self.tree, self.thresholds)
        self.horizons = {}
        for leaf in self.pool_leaves:
            readings = open_readings.get(id(leaf), [])
            earliest = max((hours for hours, _ in readings), default=None)
            horizon = trust_horizon(readings, TRUST_TOLERANCE) if readings else None
            self.horizons[id(leaf)] = None if horizon == earliest else horizon
        self.finders = {}

    def weekly_service(self, conn, org, dest, day, dep_time):
        if (org, dest, day) not in self.finders:
            self.finders[(org, dest, day)] = scheduled_service_finder(conn, org, dest, day)
        service_minutes = self.finders[(org, dest, day)](dep_time)
        return {'org': org, 'dest': dest, 'day': day,
                'serviceMinutes': dep_time if service_minutes is None else service_minutes}

    def pool_of(self, conn, org, dest, day, dep_time):
        return classify(self.tree, self.weekly_service(conn, org, dest, day, dep_time))

    def own_record(self, conn, org, dest, day, dep_time):
        weekly = self.weekly_service(conn, org, dest, day, dep_time)
        own = [flight for flight in self.flights
               if (flight['org'], flight['dest'], flight['day'], flight['serviceMinutes'])
               == (org, dest, day, weekly['serviceMinutes'])]
        return format_open_full(open_full_counts(own, self.thresholds))

    def pool_record(self, conn, org, dest, day, dep_time, hours_until_dep):
        leaf = self.pool_of(conn, org, dest, day, dep_time)
        if leaf is None:
            return None
        horizon = self.horizons[id(leaf)]
        role = self.roles[id(leaf)]
        return {
            'label': role if horizon is None else f'{role} {horizon:.1f} hr',
            'beforeTrustHorizon': horizon is not None and hours_until_dep > horizon,
            **format_open_full(open_full_counts(leaf['flights'], self.thresholds)),
        }

    def pool_days(self, conn, org, dest, day, dep_time):
        leaf = self.pool_of(conn, org, dest, day, dep_time)
        if leaf is None:
            return {day}
        weekly = self.weekly_service(conn, org, dest, day, dep_time)
        return {other_day for other_day in WEEKDAY_NAMES
                if classify(self.tree, {**weekly, 'day': other_day}) is leaf} | {day}

    def history_ranges(self, conn, org, dest, day, dep_time, hours_until_dep, flight_date):
        days = self.pool_days(conn, org, dest, day, dep_time)
        rows = conn.execute(
            f"""SELECT flightDate, depTime, hoursBeforeDep, y, cPlus, firstOrPS, d1
                FROM observations
                WHERE org = ? AND dest = ? AND flightDate >= ? AND flightDate < ?
                  AND depTime IS NOT NULL AND hoursBeforeDep IS NOT NULL
                  AND {excluded_date_where_clause()} AND {not_seat_map_only_where_clause()}""",
            (org, dest, self.thresholds['dateFrom'], flight_date),
        ).fetchall()
        rows = [r for r in rows if WEEKDAY_NAMES[date.fromisoformat(r[0]).weekday()] in days]
        service_times = logged_service_containing(dep_time, (r[1] for r in rows))

        readings_by_date = defaultdict(list)
        for flight_date_seen, logged_dep, hours, y, c_plus, first_or_ps, d1 in rows:
            if logged_dep in service_times:
                readings_by_date[flight_date_seen].append(
                    {'hours': hours, 'y': y, 'cPlus': c_plus, 'firstOrPS': first_or_ps, 'd1': d1})

        ranges = {}
        for cabin_key, column in HISTORY_CABIN_COLUMNS.items():
            values = []
            for readings in readings_by_date.values():
                points = sorted((r['hours'], r[column]) for r in readings if r[column] is not None)
                value = value_at_hours(points, hours_until_dep) if points else None
                if value is not None:
                    values.append(value)
            ranges[cabin_key] = (
                {'low': round(min(values)), 'high': round(max(values)), 'count': len(values)}
                if values else None
            )
        return ranges


_cached = {}


def snapshot(conn):
    thresholds = load_open_full_settings(conn)
    key = (date.today(), tuple(sorted(thresholds.items())))
    if key not in _cached:
        _cached.clear()
        _cached[key] = PoolSnapshot(conn)
    return _cached[key]
