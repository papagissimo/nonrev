"""
Shared WHERE-clause fragment for readers that treat an observations row as
an availability (can-buy) reading.

An observation can carry seat-map counts with no can-buy counts at all (a
seat-map-only sitting): y/cPlus/firstOrPS/d1 all NULL, some seat-map column
set. A reader that counts a missing cabin as 0 - GraphObservations does -
would turn such a row into a phantom "flight was full" point, so every
can-buy reader filters through this one clause. Every other row passes
untouched, including older rows with no can-buy values and no seat-map
values either.
"""

CAN_BUY_COLUMNS = ('y', 'cPlus', 'firstOrPS', 'd1')
SEAT_MAP_COLUMNS = (
    'soloY', 'soloCPlus', 'soloFirstOrPS', 'soloD1',
    'pairY', 'pairCPlus', 'pairFirstOrPS', 'pairD1',
    'blockedTotal',
)


def not_seat_map_only_where_clause(alias_prefix=''):
    any_can_buy = ' OR '.join(f"{alias_prefix}{column} IS NOT NULL" for column in CAN_BUY_COLUMNS)
    no_seat_map = ' AND '.join(f"{alias_prefix}{column} IS NULL" for column in SEAT_MAP_COLUMNS)
    return f"(({any_can_buy}) OR ({no_seat_map}))"
