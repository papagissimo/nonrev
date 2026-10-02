import os
import sqlite3
from collections import defaultdict

from scipy.stats import kruskal, mannwhitneyu

from ServiceGrouping import load_open_full_settings
from T1GridReport import (
    DAYS_OF_WEEK, format_minutes, last_t1_instances, routes_with_history, scheduled_service_finder,
)

DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'nonrev.db')
LAST_READING_MAX_HOURS = 6.0
SPLIT_BAR = 0.01
LEVELS_PER_LINE = 5

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
                    'flightDate': instance['flightDate'], 'cappedT1': min(instance['t1'], open_threshold),
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


def rate(values, test):
    return sum(1 for value in values if test(value)) / len(values)


def describe_levels(levels, sibling_levels, pad):
    if len(levels) > len(sibling_levels) and len(levels) > 6:
        return f'the other {len(levels)}'
    names = sorted(levels)
    if len(levels) <= 6:
        return ', '.join(names)
    lines = [names[start:start + LEVELS_PER_LINE] for start in range(0, len(names), LEVELS_PER_LINE)]
    listed = '\n'.join(pad + '      ' + ', '.join(line) for line in lines)
    return f'these {len(levels)}:\n{listed}'


def print_node(node, thresholds, indent=0):
    values = capped_t1s(node['flights'])
    weekly_services = len(by_level(node['flights'], 'weekly service'))
    open_rate = rate(values, lambda value: value >= thresholds['openThreshold'])
    full_rate = rate(values, lambda value: value <= thresholds['fullThreshold'])
    pad = '    ' * indent
    if node['path']:
        attribute, levels, sibling_levels, adjusted = node['path'][-1]
        print(f'{pad}{attribute} (split p={adjusted:.1e}): {describe_levels(levels, sibling_levels, pad)}')
    else:
        print('Everything')
    print(f'{pad}  {len(values)} flights, {weekly_services} weekly services, '
          f'open {open_rate:.0%}, full {full_rate:.0%}')
    if 'children' not in node and len(node['path']) and weekly_services <= 8:
        for label, members in sorted(by_level(node['flights'], 'weekly service').items()):
            letters = ''.join(verdict_letter(value, thresholds) for value in
                              capped_t1s(sorted(members, key=lambda f: f['flightDate'])))
            print(f'{pad}    {label:<30} {letters}')
    for child in node.get('children', []):
        print_node(child, thresholds, indent + 1)


def verdict_letter(t1, thresholds):
    if t1 <= thresholds['fullThreshold']:
        return 'F'
    if t1 >= thresholds['openThreshold']:
        return 'O'
    return 'i'


def main():
    if not os.path.exists(DB_PATH):
        raise SystemExit(f'{DB_PATH} not found - refusing to run rather than create an empty database.')
    conn = sqlite3.connect(DB_PATH)
    thresholds = load_open_full_settings(conn)
    flights = logged_flights(conn, thresholds['openThreshold'])

    print(f'{len(flights)} flights (live T1, last reading within {LAST_READING_MAX_HOURS:g}h, '
          f'capped at {thresholds["openThreshold"]}, excluded ranges left out).')
    print(f'Each pool splits on whichever of {", ".join(ATTRIBUTES)} separates it most, '
          f'if that clears p < {SPLIT_BAR:g} after allowing for how many were tried.')
    print('Letters are flights in date order: O open, i in between, F full.')
    print()
    print_node(grow(flights, []), thresholds)


if __name__ == '__main__':
    main()
