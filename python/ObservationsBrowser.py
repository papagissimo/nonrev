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

from SeatLoggingDialog import eastern_now, minutes_to_12h

REAL_COLUMNS = [
    'observationId', 'checkTimestamp', 'flightDate', 'carrier', 'flightNumber',
    'org', 'dest', 'y', 'cPlus', 'firstOrPS', 'd1',
    'soloY', 'soloCPlus', 'soloFirstOrPS', 'soloD1',
    'pairY', 'pairCPlus', 'pairFirstOrPS', 'pairD1', 'blockedTotal',
    'hoursBeforeDep', 'nextDesiredLog', 'depTime',
]

# REAL_COLUMNS above are display/dict-key names (what this module's
# callers and ObservationsBrowser.html already expect) - 'flightNumber'
# is kept as that display name for continuity, but the actual column in
# the observations table is carriersFltNum_notStable_DO_NOT_USE (decorative
# only, never matched/joined on - see domainKnowledge.md). This maps a
# display name to its real SQL column wherever one gets interpolated
# into a query (SELECT list, WHERE, ORDER BY) - identity for every other
# column, which never had a name mismatch.
SQL_COLUMN_FOR = {'flightNumber': 'carriersFltNum_notStable_DO_NOT_USE'}

SORTABLE_COLUMNS = set(REAL_COLUMNS)


def _sql_col(display_col):
    return SQL_COLUMN_FOR.get(display_col, display_col)


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
    sort_dir_sql = 'ASC' if str(sort_dir).lower() == 'asc' else 'DESC'
    reverse = str(sort_dir).lower() != 'asc'

    where_clauses = []
    params = []
    dep_time_filter = None
    for col, val in filters.items():
        if col not in SORTABLE_COLUMNS or val in (None, ''):
            continue
        if col == 'depTime':
            dep_time_filter = str(val).strip()
            continue
        # Comma-separated values ("slc,lax") mean "match any of these" -
        # useful for a pair of origins/destinations, or any other column
        # where more than one specific value is wanted at once.
        parts = [p.strip() for p in str(val).split(',') if p.strip()]
        if not parts:
            continue
        or_clause = ' OR '.join(f"CAST({_sql_col(col)} AS TEXT) LIKE ?" for _ in parts)
        where_clauses.append(f"({or_clause})")
        params.extend(f"%{p}%" for p in parts)
    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    needs_python_pass = dep_time_filter is not None

    if not needs_python_pass:
        total = conn.execute(
            f"SELECT COUNT(*) FROM observations {where_sql}", params
        ).fetchone()[0]

        limit_sql = "" if limit is None else "LIMIT ?"
        query_params = list(params) + ([] if limit is None else [limit])
        rows = conn.execute(
            f"""SELECT {', '.join(_sql_col(c) for c in REAL_COLUMNS)} FROM observations {where_sql}
                ORDER BY {_sql_col(sort_col)} {sort_dir_sql}, observationId {sort_dir_sql}
                {limit_sql}""",
            query_params,
        ).fetchall()
        return {'rows': [dict(zip(REAL_COLUMNS, r)) for r in rows], 'total': total}

    # The depTime filter matches the 12-hour text a person types ("7:27 pm"),
    # which SQL can't do against the stored integer, so the LIMIT can't be
    # pushed down here - every matching row is fetched. Fine at a few
    # thousand rows; a table that outgrows this would need a different approach.
    rows = conn.execute(
        f"SELECT {', '.join(_sql_col(c) for c in REAL_COLUMNS)} FROM observations {where_sql}",
        params,
    ).fetchall()
    all_rows = [dict(zip(REAL_COLUMNS, r)) for r in rows]

    if dep_time_filter:
        needle = dep_time_filter.lower()
        all_rows = [
            row for row in all_rows
            if row['depTime'] is not None and needle in minutes_to_12h(row['depTime']).lower()
        ]

    total = len(all_rows)

    if sort_col == 'depTime':
        all_rows.sort(key=lambda row: (row['depTime'] is None, row['depTime'] or 0), reverse=reverse)
    else:
        # A handful of legacy rows carry a stray non-numeric value in
        # otherwise-numeric columns (e.g. hoursBeforeDep as a stray
        # space) - str() keeps the sort from raising on those instead
        # of assuming every value is comparable to every other.
        all_rows.sort(key=lambda row: (row[sort_col] is None, str(row[sort_col])), reverse=reverse)

    if limit is not None:
        all_rows = all_rows[:limit]

    return {'rows': all_rows, 'total': total}


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
