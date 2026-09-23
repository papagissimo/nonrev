"""
Scenarios: named sets of route-and-weekday pairs, each switched on or off.
What is being studied is every pair covered by at least one switched-on
scenario. studied_cells is the one lookup the rest of the app asks.

A scenario can also hold hand grades for its services, one per service per
day, stored under the service's representative time. Nothing reads them
any more: the logging dialog's label is computed by Grading.py.
"""

import sqlite3

from ServiceGrouping import get_route_services

DAYS_OF_WEEK = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
GRADES = ['A', 'B', 'C', 'D', 'F']


def studied_cells(conn):
    rows = conn.execute(
        """SELECT c.org, c.dest, c.dayOfWeek
           FROM scenarioCells c
           JOIN scenarios s ON s.id = c.scenarioId
           WHERE s.active = 1"""
    ).fetchall()
    return {(org, dest, day) for org, dest, day in rows}


def studied_routes(conn):
    return {(org, dest) for org, dest, _ in studied_cells(conn)}


def scenario_cells(conn, scenario_id):
    rows = conn.execute(
        "SELECT org, dest, dayOfWeek FROM scenarioCells WHERE scenarioId = ?", (scenario_id,)
    ).fetchall()
    days_by_route = {}
    for org, dest, day in rows:
        days_by_route.setdefault((org, dest), set()).add(day)
    return [
        {'org': org, 'dest': dest, 'days': [day for day in DAYS_OF_WEEK if day in days]}
        for (org, dest), days in sorted(days_by_route.items())
    ]


def route_service_days(conn, org, dest):
    """
    The route's services, each with the flights it has on each day. Ignored
    flights are left out, since the logging dialog never offers them.
    [{'serviceMinutes': int, 'days': {'Mon': [depTime, ...], ...}}, ...]
    """
    ignored = set(conn.execute(
        "SELECT dayOfWeek, depTime FROM flightSchedule WHERE org = ? AND dest = ? AND ignore = 1",
        (org, dest),
    ).fetchall())
    services = []
    for service in get_route_services(conn, org, dest):
        days = {}
        for row in service['rows']:
            if (row['dayOfWeek'], row['depTime']) in ignored:
                continue
            days.setdefault(row['dayOfWeek'], []).append(row['depTime'])
        if days:
            services.append({
                'serviceMinutes': service['repMinutes'],
                'days': {day: sorted(times) for day, times in days.items()},
            })
    return services


def scenario_grades(conn, scenario_id):
    rows = conn.execute(
        """SELECT org, dest, dayOfWeek, serviceMinutes, grade
           FROM scenarioServiceGrades WHERE scenarioId = ?
           ORDER BY org, dest, serviceMinutes, dayOfWeek""",
        (scenario_id,),
    ).fetchall()
    return [
        {'org': org, 'dest': dest, 'day': day, 'serviceMinutes': minutes, 'grade': grade}
        for org, dest, day, minutes, grade in rows
    ]


def route_options(conn):
    routes = set(conn.execute("SELECT DISTINCT org, dest FROM flightSchedule").fetchall())
    routes |= set(conn.execute("SELECT DISTINCT org, dest FROM scenarioCells").fetchall())
    flight_days = {}
    for org, dest, day in conn.execute(
        "SELECT DISTINCT org, dest, dayOfWeek FROM flightSchedule WHERE ignore = 0"
    ):
        flight_days.setdefault((org, dest), set()).add(day)
    return [
        {'org': org, 'dest': dest,
         'flightDays': [day for day in DAYS_OF_WEEK if day in flight_days.get((org, dest), set())],
         'services': route_service_days(conn, org, dest)}
        for org, dest in sorted(routes)
    ]


def get_screen_data(conn):
    scenarios = [
        {'id': scenario_id, 'name': name, 'active': bool(active),
         'cells': scenario_cells(conn, scenario_id), 'grades': scenario_grades(conn, scenario_id)}
        for scenario_id, name, active in conn.execute("SELECT id, name, active FROM scenarios ORDER BY id")
    ]
    return {'scenarios': scenarios, 'routes': route_options(conn)}


def clean_scenario(entry):
    name = str(entry.get('name') or '').strip()
    if not name:
        raise ValueError('Every scenario needs a name.')
    cells = []
    for cell in entry.get('cells') or []:
        org = str(cell['org']).strip().lower()
        dest = str(cell['dest']).strip().lower()
        for day in cell['days']:
            if day not in DAYS_OF_WEEK:
                raise ValueError(f'Unknown weekday: {day}')
            cells.append((org, dest, day))
    grades = {}
    for item in entry.get('grades') or []:
        org = str(item['org']).strip().lower()
        dest = str(item['dest']).strip().lower()
        day = item['day']
        grade = str(item.get('grade') or '').strip().upper()
        if grade == '':
            continue
        if grade not in GRADES:
            raise ValueError(f'Unknown grade: {grade}')
        if (org, dest, day) not in cells:
            continue
        grades[(org, dest, day, int(item['serviceMinutes']))] = grade
    return {'id': entry.get('id'), 'name': name, 'active': bool(entry.get('active')),
            'cells': cells, 'grades': [(*key, grade) for key, grade in grades.items()]}


def save_scenarios(conn, payload):
    scenarios = [clean_scenario(entry) for entry in payload.get('scenarios') or []]
    deleted_ids = [int(scenario_id) for scenario_id in payload.get('deletedIds') or []]

    names = [scenario['name'].lower() for scenario in scenarios]
    if len(names) != len(set(names)):
        raise ValueError('Two scenarios have the same name.')

    try:
        write_scenarios(conn, scenarios, deleted_ids)
    except sqlite3.IntegrityError:
        raise ValueError('That name is already used by another scenario.')

    return get_screen_data(conn)


def write_scenarios(conn, scenarios, deleted_ids):
    with conn:
        existing_ids = {row[0] for row in conn.execute("SELECT id FROM scenarios")}
        missing = [s['name'] for s in scenarios if s['id'] is not None and s['id'] not in existing_ids]
        if missing:
            raise ValueError(f"No longer exists, reload the page: {', '.join(missing)}")

        for scenario_id in deleted_ids:
            conn.execute("DELETE FROM scenarios WHERE id = ?", (scenario_id,))

        for scenario in scenarios:
            if scenario['id'] is None:
                scenario['id'] = conn.execute(
                    "INSERT INTO scenarios (name, active) VALUES (?, ?)",
                    (scenario['name'], int(scenario['active'])),
                ).lastrowid
            else:
                conn.execute(
                    "UPDATE scenarios SET name = ?, active = ? WHERE id = ?",
                    (scenario['name'], int(scenario['active']), scenario['id']),
                )
                conn.execute("DELETE FROM scenarioCells WHERE scenarioId = ?", (scenario['id'],))
                conn.execute("DELETE FROM scenarioServiceGrades WHERE scenarioId = ?", (scenario['id'],))
            conn.executemany(
                "INSERT INTO scenarioCells (scenarioId, org, dest, dayOfWeek) VALUES (?, ?, ?, ?)",
                [(scenario['id'], org, dest, day) for org, dest, day in scenario['cells']],
            )
            conn.executemany(
                """INSERT INTO scenarioServiceGrades (scenarioId, org, dest, dayOfWeek, serviceMinutes, grade)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                [(scenario['id'], *grade) for grade in scenario['grades']],
            )
