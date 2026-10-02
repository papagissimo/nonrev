import os
import sqlite3

from Pools import ATTRIBUTES, LAST_READING_MAX_HOURS, SPLIT_BAR, by_level, capped_t1s, grow, logged_flights, rate
from ServiceGrouping import load_open_full_settings

DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'nonrev.db')
LEVELS_PER_LINE = 5


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
