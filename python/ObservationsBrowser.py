"""
ObservationsBrowser backend: a spreadsheet-style browse/sort/filter/delete
view directly over the observations table. Built for the "I logged five bad
readings, cleaned them up, now need to go delete the originals" case - no
route/flight-picking flow, just the raw table with Excel-autofilter-ish
column filters and click-to-sort headers.

Filtering is intentionally uniform across every column (substring match on
the text form of the value) rather than type-aware (numeric ranges, date
pickers, etc). That covers the real use case - eyeballing recent rows and
narrowing by whatever's visible - without needing a UI per column type.
"""

COLUMNS = [
    'observationId', 'checkTimestamp', 'flightDate', 'carrier', 'flightNumber',
    'org', 'dest', 'readingType', 'y', 'cPlus', 'firstOrPS', 'd1',
    'hoursBeforeDep', 'nextDesiredLog',
]

# observationId is real but not user-facing sort/filter surface - it's
# still selectable/orderable defensively, so no harm leaving it in.
SORTABLE_COLUMNS = set(COLUMNS)


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
        where_clauses.append(f"CAST({col} AS TEXT) LIKE ?")
        params.append(f"%{val}%")
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
