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

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from SeatLoggingDialog import eastern_now, ET_ZONE, minutes_to_12h
from timezones import get_confirmed_timezone, UnconfirmedAirportError

REAL_COLUMNS = [
    'observationId', 'checkTimestamp', 'flightDate', 'carrier', 'flightNumber',
    'org', 'dest', 'readingType', 'y', 'cPlus', 'firstOrPS', 'd1',
    'hoursBeforeDep', 'nextDesiredLog',
]

# depTime isn't a real observations column, and deliberately isn't joined
# in from flightSchedule's current row either - a flightNumber can be
# renamed or dropped from flightSchedule entirely (the known bogus-number
# fragmentation), which would leave the join with nothing to match and
# the column blank for exactly the rows most worth troubleshooting.
# Instead it's derived per-row from data the observation already owns:
# checkTimestamp + hoursBeforeDep together fix the exact instant the
# flight departed (that's the whole point of correcting hoursBeforeDep
# in place rather than freezing it), converted to the origin airport's
# own local clock. This is the flight's own frozen departure time for
# that specific day, independent of whatever flightSchedule says right
# now - never blank just because a flightNumber was later reassigned.
COLUMNS = REAL_COLUMNS + ['depTime']
SORTABLE_COLUMNS = set(COLUMNS)


def dep_time_minutes(conn, org, check_timestamp, hours_before_dep):
    """
    Origin-local minutes-since-midnight for this observation's departure,
    reconstructed from its own checkTimestamp + hoursBeforeDep - not a
    flightSchedule lookup. Returns None if hoursBeforeDep was never
    computed (an unconfirmed-airport gap at logging time, or a handful
    of legacy rows carrying a stray non-numeric value) or if the origin
    isn't confirmed right now.
    """
    if hours_before_dep is None:
        return None
    try:
        hours_before_dep = float(hours_before_dep)
    except (TypeError, ValueError):
        return None
    try:
        tz_name = get_confirmed_timezone(conn, org)
    except UnconfirmedAirportError:
        return None
    try:
        check_dt = datetime.strptime(check_timestamp, '%Y-%m-%d %H:%M').replace(tzinfo=ET_ZONE)
    except (TypeError, ValueError):
        return None  # a handful of legacy rows carry a malformed checkTimestamp (e.g. "24:03")
    dep_dt_et = check_dt + timedelta(hours=hours_before_dep)
    local_dt = dep_dt_et.astimezone(ZoneInfo(tz_name))
    return local_dt.hour * 60 + local_dt.minute


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

    # Only the real columns can be pushed down into SQL. depTime is
    # computed in Python below, so it's excluded from the WHERE clause
    # here and handled separately once every candidate row has a value.
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
        or_clause = ' OR '.join(f"CAST({col} AS TEXT) LIKE ?" for _ in parts)
        where_clauses.append(f"({or_clause})")
        params.extend(f"%{p}%" for p in parts)
    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    needs_python_pass = dep_time_filter is not None or sort_col == 'depTime'

    if not needs_python_pass:
        # Common case (no depTime filter/sort involved): SQL does the
        # sorting, filtering, and paging exactly as before - fast, and
        # depTime only needs computing for the one page actually shown.
        total = conn.execute(
            f"SELECT COUNT(*) FROM observations {where_sql}", params
        ).fetchone()[0]

        limit_sql = "" if limit is None else "LIMIT ?"
        query_params = list(params) + ([] if limit is None else [limit])
        rows = conn.execute(
            f"""SELECT {', '.join(REAL_COLUMNS)} FROM observations {where_sql}
                ORDER BY {sort_col} {sort_dir_sql}, observationId {sort_dir_sql}
                {limit_sql}""",
            query_params,
        ).fetchall()

        result_rows = []
        for r in rows:
            row = dict(zip(REAL_COLUMNS, r))
            row['depTime'] = dep_time_minutes(conn, row['org'], row['checkTimestamp'], row['hoursBeforeDep'])
            result_rows.append(row)
        return {'rows': result_rows, 'total': total}

    # depTime is involved: every row matching the other filters needs its
    # depTime computed before the depTime filter/sort/limit can be applied,
    # so this pass can't push the LIMIT down to SQL - it fetches every
    # matching row instead. Observations is a few thousand rows, still
    # cheap; a table that outgrows this would need a different approach.
    rows = conn.execute(
        f"SELECT {', '.join(REAL_COLUMNS)} FROM observations {where_sql}",
        params,
    ).fetchall()

    all_rows = []
    for r in rows:
        row = dict(zip(REAL_COLUMNS, r))
        row['depTime'] = dep_time_minutes(conn, row['org'], row['checkTimestamp'], row['hoursBeforeDep'])
        all_rows.append(row)

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
