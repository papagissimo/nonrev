"""
FloorEstimates.py

Coefficients for converting a cheap-side glance (a confirmed 9 ceiling, a
confirmed 0, or a genuine "at least N" floor for N up to ~5) into a
decimal-valued estimate of what the actual (binary-search-confirmed) value
would turn out to be, for use wherever a real actual value isn't available
yet.

The math, per cabin: pull every historical row where BOTH a cheap glance
and a real actual value are present for that cabin - a same-row, same-
session pair (he glanced it, then later in that same reading actually
pinned it down) - group by the cheap value, and take the mean of the
actual values in each group. No minimum-sample threshold: a group's
sample size speaks for itself through its own stored sampleCount, rather
than an arbitrary cutoff hiding thin data behind a fallback. Mean (not
median) is deliberate: the T1 estimate sums all four cabins together, and
mean is linear (mean of a sum = sum of means), so summing four per-cabin
mean estimates matches estimating the combined total directly - median
doesn't have that property and would introduce a small bias once summed.

Recomputed from scratch every refresh, never maintained as an incremental
running average - a deliberate choice even though a running average would
be cheaper, because the planned future upgrade (an asymmetric Beta fit
over [floor, 8], tabled for now - see the decline-curve work) can't be
maintained incrementally, and building incremental-update machinery now
would just have to be thrown away then. A plain from-scratch recompute
costs milliseconds against his real data volume, so there's no
performance reason to do otherwise.

Refresh triggers (his call, deliberately NOT tied to cadence logic or
server lifecycle - see SeatLoggingDialog.html/Launcher.html): once when
the logging dialogue's page loads, and on demand via a manual button on
the launcher. Nothing here is ever called from get_next_batch or any
other cadence code.

The estimate itself is never stored on an observation row - always
computed live from these cached coefficients, at whatever moment a
consumer (Previous readings, the live T1 column, GraphObservations'
t1New) needs it.
"""

CABIN_COLUMNS = {
    'y': ('y', 'cheapY'),
    'cPlus': ('cPlus', 'cheapCPlus'),
    'firstOrPS': ('firstOrPS', 'cheapFirstOrPS'),
    'd1': ('d1', 'cheapD1'),
}


def refresh_floor_estimates(conn):
    """
    Recomputes floorEstimateCoefficients from scratch against every
    'avail'-type observation row currently in the db - deletes and
    rebuilds the whole table rather than updating it incrementally (see
    module docstring). Returns {cabin: number of (floor, mean) rows
    written} for logging/display.
    """
    conn.execute("DELETE FROM floorEstimateCoefficients")

    summary = {}
    for cabin, (actual_col, cheap_col) in CABIN_COLUMNS.items():
        rows = conn.execute(
            f"""SELECT {cheap_col}, AVG({actual_col}), COUNT(*)
                FROM observations
                WHERE readingType = 'avail'
                  AND {cheap_col} IS NOT NULL
                  AND {actual_col} IS NOT NULL
                GROUP BY {cheap_col}"""
        ).fetchall()
        for floor_value, mean_actual, sample_count in rows:
            conn.execute(
                """INSERT INTO floorEstimateCoefficients (cabin, floorValue, meanActual, sampleCount)
                   VALUES (?, ?, ?, ?)""",
                (cabin, int(floor_value), float(mean_actual), int(sample_count)),
            )
        summary[cabin] = len(rows)

    conn.commit()
    return summary


def load_floor_estimates(conn):
    """
    Returns {(cabin, floorValue): meanActual} for every cached
    coefficient. Doesn't compute anything - reads whatever
    refresh_floor_estimates last wrote. Call once per request and pass
    the result around rather than re-querying per row.
    """
    return {
        (cabin, floor_value): mean_actual
        for cabin, floor_value, mean_actual in conn.execute(
            "SELECT cabin, floorValue, meanActual FROM floorEstimateCoefficients"
        ).fetchall()
    }


def estimate_for_floor(coefficients, cabin, floor_value):
    """
    coefficients: the dict returned by load_floor_estimates.

    Falls back to the raw floor value itself (as a float, so it still
    carries the decimal-point self-distinguishing marker once a caller
    rounds/displays it) when no coefficient exists yet for this exact
    (cabin, floor) pair - happens before the first-ever refresh, or for a
    floor value with zero historical actual-resolved pairs so far.
    """
    return coefficients.get((cabin, floor_value), float(floor_value))
