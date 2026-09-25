"""
Insider readings: standby availability, others listed, our list position
and a yes/no call, read off an airline's employee system (Delta's is
DeltaNet) by someone with access to it and relayed secondhand. Kept in its
own table, separate from observations, because it's a different observer
looking at data the public site never shows.

Entered from the logging dialog's insider panel, saved on the same Submit
as the rest of the row. The page behind the Launcher's "Insider Readings"
button only lists and deletes.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from timezones import et_equivalent_datetime, UnconfirmedAirportError

ET_ZONE = ZoneInfo('America/New_York')

TABLE_SQL = """
CREATE TABLE insiderReadings (
    readingId          INTEGER PRIMARY KEY AUTOINCREMENT,
    carrier            TEXT NOT NULL,
    org                TEXT NOT NULL,
    dest               TEXT NOT NULL,
    flightDate         TEXT NOT NULL,
    depTime            INTEGER NOT NULL,
    checkTimestamp     TEXT NOT NULL,
    standbyY           INTEGER,
    standbyCPlus       INTEGER,
    standbyFirstOrPS   INTEGER,
    standbyD1          INTEGER,
    standbyTotal       INTEGER,
    numListed          INTEGER,
    ourPosition        INTEGER,
    verdict            TEXT
)
"""

NUMBER_FIELDS = [
    'standbyY', 'standbyCPlus', 'standbyFirstOrPS', 'standbyD1',
    'standbyTotal', 'numListed', 'ourPosition',
]

VERDICTS = ['no', 'probably not', 'probably yes', 'yes']


def _hours_out(conn, org, dep_minutes, flight_date, check_timestamp):
    check_dt = datetime.strptime(check_timestamp, '%Y-%m-%d %H:%M').replace(tzinfo=ET_ZONE)
    flight_date_obj = datetime.strptime(flight_date, '%Y-%m-%d').date()
    try:
        dep_dt = et_equivalent_datetime(conn, dep_minutes, org, flight_date_obj)
    except UnconfirmedAirportError as e:
        print(f"Warning: couldn't compute hours out for insider reading ({org}): {e}")
        return None
    return (dep_dt - check_dt).total_seconds() / 3600


def insert_reading(conn, reading):
    conn.execute(
        f"""INSERT INTO insiderReadings
            (carrier, org, dest, flightDate, depTime, checkTimestamp,
             {', '.join(NUMBER_FIELDS)}, verdict)
            VALUES ({', '.join('?' * (7 + len(NUMBER_FIELDS)))})""",
        (reading['carrier'], reading['org'], reading['dest'], reading['flightDate'],
         reading['depTime'], reading['checkTimestamp'],
         *[reading.get(f) for f in NUMBER_FIELDS], reading.get('verdict')),
    )


def _check_timestamp(typed_time, now):
    """A typed HH:MM means its most recent occurrence at or before now, so a
    late-evening reading entered after midnight lands on the right day."""
    if not typed_time:
        return now.strftime('%Y-%m-%d %H:%M')
    hour, minute = (int(part) for part in typed_time.split(':'))
    checked = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if checked > now:
        checked -= timedelta(days=1)
    return checked.strftime('%Y-%m-%d %H:%M')


def save_from_logging(conn, flight_date, entries, now):
    """Writes the insider panel of every logging-dialog entry that has one
    filled in. The caller commits, together with its own observations."""
    saved = 0
    for entry in entries:
        insider = entry.get('insider') or {}
        numbers = {f: (None if insider.get(f, '') in ('', None) else int(insider[f]))
                   for f in NUMBER_FIELDS}
        verdict = insider.get('verdict') or None
        if all(v is None for v in numbers.values()) and verdict is None:
            continue
        insert_reading(conn, {
            'carrier': entry['car'], 'org': entry['org'], 'dest': entry['dest'],
            'flightDate': flight_date, 'depTime': int(entry['dep']),
            'checkTimestamp': _check_timestamp(insider.get('checkTime'), now),
            **numbers, 'verdict': verdict,
        })
        saved += 1
    return saved


def get_readings(conn, flight_date=None):
    flight_date = flight_date or datetime.now(ET_ZONE).date().isoformat()
    cols = ['readingId', 'carrier', 'org', 'dest', 'depTime', 'checkTimestamp',
            *NUMBER_FIELDS, 'verdict']
    rows = conn.execute(
        f"""SELECT {', '.join(cols)} FROM insiderReadings
            WHERE flightDate = ?
            ORDER BY checkTimestamp, org, dest, depTime""",
        (flight_date,),
    ).fetchall()
    readings = []
    for row in rows:
        reading = dict(zip(cols, row))
        reading['hoursOut'] = _hours_out(conn, reading['org'], reading['depTime'],
                                         flight_date, reading['checkTimestamp'])
        readings.append(reading)
    return {'flightDate': flight_date, 'readings': readings}


def delete_reading(conn, reading_id, flight_date):
    conn.execute("DELETE FROM insiderReadings WHERE readingId = ?", (int(reading_id),))
    conn.commit()
    return get_readings(conn, flight_date)
