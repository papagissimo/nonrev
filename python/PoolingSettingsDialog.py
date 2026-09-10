"""
PoolingSettingsDialog backend: manages excludedDateRanges, the global
list of flight-date ranges left out of every pooling consumer (verdict/
classification, decline-curve fitting, the weekday chart, dayGroupings
stats) - not tied to any one graphing tool, hence its own standalone
dialog rather than a panel inside GraphObservations. Expected to stay a
short list (a handful of rows), so this deliberately reuses the simple
whole-list update/delete/insert save pattern from FlightScheduleDialog
rather than ObservationsBrowser's sort/filter/paging machinery.

is_date_excluded / excluded_date_where_clause are the shared functions
other modules should call rather than querying excludedDateRanges
directly - keeps the "what counts as excluded" logic in one place as
more pooling consumers start checking it.

Also manages declineCurveSettings (the step-change correction loop's
RMSE threshold and iteration cap - see DeclineCurveFit.py's docstring for
what they control) - same settings key/value table as everything else in
settings.py, just surfaced on this page since decline-curve fitting is
already a named pooling consumer here.
"""

from settings import load_settings, save_settings, DECLINE_CURVE_SETTINGS_KEY, DEFAULT_DECLINE_CURVE_SETTINGS


def get_decline_curve_settings(conn):
    return load_settings(conn, key=DECLINE_CURVE_SETTINGS_KEY, defaults=DEFAULT_DECLINE_CURVE_SETTINGS)


def save_decline_curve_settings(conn, payload):
    """payload: {stepChangeRmseThreshold: float, stepChangeMaxIterations: int}"""
    settings = {
        'stepChangeRmseThreshold': float(payload['stepChangeRmseThreshold']),
        'stepChangeMaxIterations': int(payload['stepChangeMaxIterations']),
    }
    save_settings(conn, settings, key=DECLINE_CURVE_SETTINGS_KEY)
    return settings


def get_excluded_date_ranges(conn):
    rows = conn.execute(
        "SELECT id, startDate, endDate, reason FROM excludedDateRanges ORDER BY startDate"
    ).fetchall()
    return {
        'ranges': [
            {'id': rowid, 'startDate': start, 'endDate': end, 'reason': reason or ''}
            for rowid, start, end, reason in rows
        ],
    }


def save_excluded_date_ranges(conn, payload):
    """
    payload: { ranges: [{ id (existing int, or null for new),
                           startDate, endDate, reason, deleted (bool) }] }
    Dates are 'YYYY-MM-DD' strings throughout, matching flightDate
    elsewhere in the schema.
    """
    for entry in payload.get('ranges', []):
        if not entry.get('id') or entry.get('deleted'):
            continue
        conn.execute(
            "UPDATE excludedDateRanges SET startDate=?, endDate=?, reason=? WHERE id=?",
            (entry['startDate'], entry['endDate'], (entry.get('reason') or '').strip() or None,
             entry['id']),
        )

    to_delete = [e['id'] for e in payload.get('ranges', []) if e.get('id') and e.get('deleted')]
    for range_id in to_delete:
        conn.execute("DELETE FROM excludedDateRanges WHERE id=?", (range_id,))

    new_ranges = [e for e in payload.get('ranges', []) if not e.get('id') and not e.get('deleted')]
    for entry in new_ranges:
        conn.execute(
            "INSERT INTO excludedDateRanges (startDate, endDate, reason) VALUES (?,?,?)",
            (entry['startDate'], entry['endDate'], (entry.get('reason') or '').strip() or None),
        )

    conn.commit()
    return {'savedCount': len(payload.get('ranges', []))}


def is_date_excluded(conn, flight_date):
    """flight_date: 'YYYY-MM-DD' string. True if it falls in any saved
    range (inclusive both ends)."""
    row = conn.execute(
        "SELECT 1 FROM excludedDateRanges WHERE ? BETWEEN startDate AND endDate LIMIT 1",
        (flight_date,),
    ).fetchone()
    return row is not None


def excluded_date_where_clause(alias_prefix=''):
    """
    Returns (sql_fragment, needs_params_hint) for a pooling consumer's own
    WHERE clause: NOT EXISTS against excludedDateRanges, using that
    consumer's own flightDate column (qualified with alias_prefix,
    e.g. 'o.' for an observations alias 'o'). No params needed - the
    consumer's own flightDate column name is inlined directly since it's
    always a trusted internal identifier, never user input.
    """
    col = f"{alias_prefix}flightDate"
    return (
        f"NOT EXISTS (SELECT 1 FROM excludedDateRanges edr "
        f"WHERE {col} BETWEEN edr.startDate AND edr.endDate)"
    )
