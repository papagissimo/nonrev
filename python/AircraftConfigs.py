"""
Aircraft configurations (the aircraftConfigs table) and the two things the
seat-map panel does with them: correct which aircraft a scheduled flight
uses, and add/edit an aircraft's cabin sizes.

A cabin size of NULL means unknown; 0 means the aircraft has no such cabin.
Callers may only pre-fill "none of that cabin" from a size that is 0.

Correcting a flight's aircraft rewrites flightSchedule.aircraftConfig for that
one schedule row in place, the same as every other schedule edit - no history
kept, and the value carries forward to that weekly slot's next occurrence.
"""

import re

CABIN_SIZE_COLUMNS = ('d1', 'first', 'comfortPlus', 'main')
KEY_PATTERN = re.compile(r'^[a-z0-9][a-z0-9-]*$')
MAX_CABIN_SIZE = 999


class AircraftError(Exception):
    pass


def load_aircraft_list(conn):
    rows = conn.execute(
        "SELECT configKey, aircraft, d1, first, comfortPlus, main, total, confirmed "
        "FROM aircraftConfigs ORDER BY rowid"
    ).fetchall()
    return [
        {'key': key, 'name': name, 'd1': d1, 'first': first,
         'comfortPlus': comfort_plus, 'main': main, 'total': total, 'confirmed': bool(confirmed)}
        for key, name, d1, first, comfort_plus, main, total, confirmed in rows
    ]


def find_config_key(conn, key):
    """The stored configKey matching key case-insensitively, or None."""
    row = conn.execute(
        "SELECT configKey FROM aircraftConfigs WHERE lower(configKey) = lower(?)", (key,)
    ).fetchone()
    return row[0] if row else None


def parse_size(value, label):
    text = str(value).strip() if value is not None else ''
    if not text.isdigit() or int(text) > MAX_CABIN_SIZE:
        raise AircraftError(f"{label} seats must be a whole number from 0 to {MAX_CABIN_SIZE}")
    return int(text)


def set_schedule_aircraft(conn, schedule_row, config_key):
    stored_key = find_config_key(conn, config_key)
    if stored_key is None:
        raise AircraftError(f"No aircraft called '{config_key}' - add it first")
    updated = conn.execute(
        "UPDATE flightSchedule SET aircraftConfig = ? WHERE rowid = ?", (stored_key, schedule_row)
    ).rowcount
    if updated != 1:
        raise AircraftError(f"Schedule row {schedule_row} not found")
    conn.commit()
    return {'aircraftConfig': stored_key}


def save_aircraft(conn, payload):
    """payload: {mode: 'new'|'edit', configKey, aircraft, d1, first,
    comfortPlus, main, scheduleRow (optional)}.

    'new' refuses a key that already exists (any casing). 'edit' updates the
    named aircraft, or creates it when the schedule already references a key
    that has no row yet. All four cabin sizes are required - an aircraft
    whose sizes aren't known doesn't belong in the table. With scheduleRow,
    that flight is also switched to this aircraft in the same transaction."""
    mode = payload.get('mode')
    key = str(payload.get('configKey', '')).strip().lower()
    if not KEY_PATTERN.match(key):
        raise AircraftError("Key must be lowercase letters, digits and dashes, e.g. a321neo or crj-900")
    name = str(payload.get('aircraft', '')).strip() or key

    sizes = {
        column: parse_size(payload.get(column), label)
        for column, label in (('d1', 'D1'), ('first', 'First'), ('comfortPlus', 'Comfort+'), ('main', 'Main'))
    }
    total = sum(sizes.values())

    existing_key = find_config_key(conn, key)
    if mode == 'new':
        if existing_key is not None:
            raise AircraftError(f"'{existing_key}' already exists")
    elif mode != 'edit':
        raise AircraftError(f"Unknown mode '{mode}'")

    if existing_key is None:
        conn.execute(
            """INSERT INTO aircraftConfigs (configKey, aircraft, d1, first, comfortPlus, main, total, status, confirmed)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'from seat map', 1)""",
            (key, name, sizes['d1'], sizes['first'], sizes['comfortPlus'], sizes['main'], total),
        )
        stored_key = key
    else:
        conn.execute(
            """UPDATE aircraftConfigs
               SET aircraft = ?, d1 = ?, first = ?, comfortPlus = ?, main = ?, total = ?, status = 'from seat map', confirmed = 1
               WHERE configKey = ?""",
            (name, sizes['d1'], sizes['first'], sizes['comfortPlus'], sizes['main'], total, existing_key),
        )
        stored_key = existing_key

    if payload.get('scheduleRow') is not None:
        updated = conn.execute(
            "UPDATE flightSchedule SET aircraftConfig = ? WHERE rowid = ?",
            (stored_key, payload['scheduleRow']),
        ).rowcount
        if updated != 1:
            conn.rollback()
            raise AircraftError(f"Schedule row {payload['scheduleRow']} not found")

    conn.commit()
    return {'configKey': stored_key, 'aircraftList': load_aircraft_list(conn)}
