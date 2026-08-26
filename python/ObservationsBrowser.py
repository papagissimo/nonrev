"""
ObservationsBrowser backend: a spreadsheet-style browse/sort/filter/delete
view directly over the observations table. Built for the "I logged five bad
readings, cleaned them up, now need to go delete the originals" case - no
route/flight-picking flow, just the raw table with Excel-autofilter-ish
column filters and click-to-sort headers.

Filtering is intentionally uniform across every column (substring match on
the text form of the value) rather than type-aware (numeric ranges, date
pickers, etc), except org/dest, which get a distinct-values dropdown since
those are naturally a short closed list and a click beats retyping an
airport code. The page defaults its flightDate filter to today (ET, same
"today" the rest of the app uses) precisely so a normal visit doesn't have
to pull the whole table before narrowing - "All rows, filtered from the
server side" beats "All rows, then filter in the browser" for a table
that only grows.
"""

from SeatLoggingDialog import eastern_now

COLUMNS = [
    'observationId', 'checkTimestamp', 'flightDate', 'carrier', 'flightNumber',
    'org', 'dest', 'readingType', 'y', 'cPlus', 'firstOrPS', 'd1',
    'hoursBeforeDep', 'nextDesiredLog',
]

# observationId is real but not user-facing sort/filter surface - it's
# still selectable/orderable defensively, so no harm leaving it in.
SORTABLE_COLUMNS = set(COLUMNS)


def get_filter_options(conn):
    origins = [r[0] for r in conn.execute(
        "SELECT DISTINCT org FROM observations ORDER BY org"
    ).fetchall()]
    destinations = [r[0] for r in conn.execute(
        "SELECT DISTINCT dest FROM observations ORDER BY dest"
    ).fetchall()]
    return {
        'origins': origins,
        'destinations': destinations,
        'today': eastern_now().date().isoformat(),
    }


def get_observations(conn, sort_col='checkTimestamp', sort_dir='desc', limit=20, filters=None):
    filters = filters or {}

    if sort_col not in SORTABLE_COLUMNS:
        sort_col = 'checkTimestamp'
    sort_dir = 'ASC' if str(sort_dir).lower() == 'asc' else 'DESC'

    where_clauses = []
    params = []
    for col, val in filters.items():
        if col not in SORTABLE_COLUMNS or val in (None, ''):
            continue
        # Comma-separated values ("slc,lax") mean "match any of these" -
        # useful for a pair of origins/destinations, or any other column
        # where more than one specific value is wanted at once.
        parts = [p.strip() for p in str(val).split(',') if p.strip()]
        if not parts:
            continue
        or_clause = ' OR '.join(f"CAST({col} AS TEXT) LIKE ?" for _ in parts)
        where_clauses.append(f"({or_clause})")
        params.extend(f"%{p}%" for p in parts)
    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    total = conn.execute(
        f"SELECT COUNT(*) FROM observations {where_sql}", params
    ).fetchone()[0]

    limit_sql = "" if limit is None else "LIMIT ?"
    query_params = list(params) + ([] if limit is None else [limit])

    rows = conn.execute(
        f"""SELECT {', '.join(COLUMNS)} FROM observations {where_sql}
            ORDER BY {sort_col} {sort_dir}, observationId {sort_dir}
            {limit_sql}""",
        query_params,
    ).fetchall()

    return {
        'rows': [dict(zip(COLUMNS, r)) for r in rows],
        'total': total,
    }


def delete_observations(conn, observation_ids):
    observation_ids = [int(i) for i in (observation_ids or [])]
    if not observation_ids:
        return {'deleted': 0}
    placeholders = ','.join('?' for _ in observation_ids)
    conn.execute(
        f"DELETE FROM observations WHERE observationId IN ({placeholders})",
        observation_ids,
    )
    conn.commit()
    return {'deleted': len(observation_ids)}
