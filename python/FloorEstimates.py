"""
What a glance floor is worth as a seat count.

A glance is two checks per cabin: can 9 be bought, can 1 be bought. A 9 or
a 0 is exact. Any other glance value is a floor - at least that many at the
price shown - and its estimate is the mean actual count on past readings
where that cabin was glanced at that floor and then binary-searched in the
same sitting. Mean rather than median because cabin estimates get summed,
and a sum of means is the mean of the sum.

A cabin with no pairs at a floor borrows the pairs of every cabin at that
floor; a floor with no pairs anywhere is its own estimate.

Computed from scratch on every call - one grouped query over the whole
table - so it can never be stale.
"""

GLANCE_COLUMN_FOR = {
    'y': 'cheapY',
    'cPlus': 'cheapCPlus',
    'firstOrPS': 'cheapFirstOrPS',
    'd1': 'cheapD1',
}
EXACT_GLANCES = (0, 9)


def load_floor_estimates(conn):
    """{'byCabin': {(actual_column, floor): mean}, 'pooled': {floor: mean}}"""
    by_cabin = {}
    pooled_sums = {}
    for actual_col, glance_col in GLANCE_COLUMN_FOR.items():
        rows = conn.execute(
            f"""SELECT {glance_col}, SUM({actual_col}), COUNT(*)
                FROM observations
                WHERE {glance_col} BETWEEN 1 AND 8 AND {actual_col} IS NOT NULL
                GROUP BY {glance_col}"""
        ).fetchall()
        for floor, total, count in rows:
            by_cabin[(actual_col, int(floor))] = total / count
            pooled_total, pooled_count = pooled_sums.get(int(floor), (0, 0))
            pooled_sums[int(floor)] = (pooled_total + total, pooled_count + count)
    pooled = {floor: total / count for floor, (total, count) in pooled_sums.items()}
    return {'byCabin': by_cabin, 'pooled': pooled}


def seats_from_glance(estimates, actual_col, glance):
    """The glance's seat count: exact for 9 or 0, else the floor's estimate."""
    if glance in EXACT_GLANCES:
        return glance
    return estimates['byCabin'].get((actual_col, glance),
                                    estimates['pooled'].get(glance, float(glance)))
