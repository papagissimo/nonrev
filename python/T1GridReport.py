from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from clustering import SERVICE_GAP_MINUTES, cluster_services, service_representative
from PoolingSettingsDialog import excluded_date_where_clause
from ServiceGrouping import get_route_services
from T1Estimator import compute_t1_replay_column
from timezones import UnconfirmedAirportError, et_equivalent_datetime, get_confirmed_timezone

DAYS_OF_WEEK = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
NO_ESTIMATE_DISPLAY = '\u2014'


def studied_routes(conn):
    rows = conn.execute(
        """SELECT DISTINCT o.org, o.dest
           FROM observations o
           LEFT JOIN routeSettings rs ON rs.org = o.org AND rs.dest = o.dest
           WHERE COALESCE(rs.studyThisRoute, 1) = 1
           ORDER BY o.org, o.dest"""
    ).fetchall()
    return [{'org': org, 'dest': dest} for org, dest in rows]


def format_t1(value):
    if value is None:
        return NO_ESTIMATE_DISPLAY
    if round(value, 1) < 10:
        return f'{value:.1f}'
    return str(int(value + 0.5))


def format_minutes(minutes):
    return f'{minutes // 60:02d}:{minutes % 60:02d}'


def format_date_header(flight_date):
    parsed = datetime.strptime(flight_date, '%Y-%m-%d')
    return f'{parsed.month}/{parsed.day}'


def as_seat_count(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def observation_rows_for_day(conn, org, dest, day_of_week):
    rows = conn.execute(
        f"""SELECT flightDate, depTime, hoursBeforeDep, y, cPlus, firstOrPS, d1
            FROM observations
            WHERE readingType = 'avail' AND org = ? AND dest = ?
              AND depTime IS NOT NULL AND hoursBeforeDep IS NOT NULL
              AND {excluded_date_where_clause()}""",
        (org, dest),
    ).fetchall()
    kept = []
    for row in rows:
        try:
            row_day = datetime.strptime(row[0], '%Y-%m-%d').strftime('%a')
        except ValueError:
            continue
        if row_day == day_of_week:
            kept.append(row)
    return kept


def scheduled_service_finder(conn, org, dest, day_of_week):
    scheduled = []
    for service in get_route_services(conn, org, dest):
        for member in service['rows']:
            if member['dayOfWeek'] == day_of_week:
                scheduled.append((member['depTime'], service['repMinutes']))

    def find(dep_time):
        if not scheduled:
            return None
        nearest_dep_time, service_minutes = min(scheduled, key=lambda s: abs(s[0] - dep_time))
        if abs(nearest_dep_time - dep_time) <= SERVICE_GAP_MINUTES:
            return service_minutes
        return None

    return find


def last_t1_instances(conn, org, dest, day_of_week):
    rows_by_date = defaultdict(list)
    for row in observation_rows_for_day(conn, org, dest, day_of_week):
        rows_by_date[row[0]].append(row)

    instances = []
    for flight_date, date_rows in rows_by_date.items():
        for cluster in cluster_services(row[1] for row in date_rows):
            members = [row for row in date_rows if row[1] in cluster]
            latest = min(members, key=lambda row: float(row[2]))
            readings = sorted(
                ({'hrs': float(row[2]), 'y': as_seat_count(row[3]), 'cplus': as_seat_count(row[4]),
                  'onePS': as_seat_count(row[5]), 'd1': as_seat_count(row[6])} for row in members),
                key=lambda reading: reading['hrs'],
            )
            t1 = compute_t1_replay_column(conn, org, dest, flight_date, latest[1], readings)[0]
            instances.append({
                'flightDate': flight_date,
                'depTime': latest[1],
                'ownServiceMinutes': service_representative(cluster),
                'numReadings': len(readings),
                't1': t1,
            })
    return instances


def get_t1_grid(conn, org, dest, day_of_week):
    find_scheduled_service = scheduled_service_finder(conn, org, dest, day_of_week)
    cells_by_service = defaultdict(dict)
    warnings = []

    for instance in last_t1_instances(conn, org, dest, day_of_week):
        service_minutes = find_scheduled_service(instance['depTime'])
        if service_minutes is None:
            service_minutes = instance['ownServiceMinutes']
        cells = cells_by_service[service_minutes]
        date = instance['flightDate']
        if date in cells:
            warnings.append(
                f"{format_date_header(date)}: two flights landed in the {format_minutes(service_minutes)} row - "
                f"showing the one with more readings"
            )
            if cells[date]['numReadings'] >= instance['numReadings']:
                continue
        cells[date] = instance

    dates = sorted({date for cells in cells_by_service.values() for date in cells})
    rows = []
    for service_minutes in sorted(cells_by_service):
        cells = cells_by_service[service_minutes]
        rows.append({
            'label': format_minutes(service_minutes),
            'cells': [
                {'t1': cells[date]['t1'], 'display': format_t1(cells[date]['t1'])} if date in cells else None
                for date in dates
            ],
        })

    return {
        'dates': dates,
        'dateHeaders': [format_date_header(date) for date in dates],
        'rows': rows,
        'warnings': warnings,
    }


def next_date_on(day_of_week):
    today = date.today()
    for offset in range(7):
        candidate = today + timedelta(days=offset)
        if candidate.strftime('%a') == day_of_week:
            return candidate


def scheduled_dep_times(conn, org, dest, day_of_week):
    rows = conn.execute(
        """SELECT DISTINCT depTime FROM flightSchedule
           WHERE org = ? AND dest = ? AND dayOfWeek = ? AND ignore = 0
           ORDER BY depTime""",
        (org, dest, day_of_week),
    ).fetchall()
    return [row[0] for row in rows]


def route_duration_minutes(conn, org, dest):
    row = conn.execute(
        "SELECT durationMinutes FROM routeSettings WHERE org = ? AND dest = ?", (org, dest)
    ).fetchone()
    return row[0] if row else None


def minutes_on_clock(instant, clock_zone, on_date):
    wall_time = instant.astimezone(clock_zone).replace(tzinfo=None)
    return int((wall_time - datetime.combine(on_date, time.min)).total_seconds() // 60)


def leg_bars(conn, org, dest, day_of_week, clock_airport, on_date):
    route_name = f'{org.upper()}\u2192{dest.upper()}'
    duration = route_duration_minutes(conn, org, dest)
    if duration is None:
        return {'route': route_name, 'bars': [], 'problem': f'no duration on file for {route_name}'}
    dep_times = scheduled_dep_times(conn, org, dest, day_of_week)
    if not dep_times:
        return {'route': route_name, 'bars': [], 'problem': f'no {day_of_week} flights scheduled for {route_name}'}

    find_scheduled_service = scheduled_service_finder(conn, org, dest, day_of_week)
    clock_zone = ZoneInfo(get_confirmed_timezone(conn, clock_airport))
    bars = []
    for dep_time in dep_times:
        departure = et_equivalent_datetime(conn, dep_time, org, on_date)
        arrival = departure.astimezone(timezone.utc) + timedelta(minutes=duration)
        end_minutes = minutes_on_clock(arrival, clock_zone, on_date)
        service_minutes = find_scheduled_service(dep_time)
        if service_minutes is None:
            service_minutes = service_representative([dep_time])
        bars.append({
            'label': format_minutes(service_minutes),
            'depExact': format_minutes(dep_time),
            'arrExact': format_minutes(end_minutes % 1440),
            'startMinutes': minutes_on_clock(departure, clock_zone, on_date),
            'endMinutes': end_minutes,
        })
    return {'route': route_name, 'bars': bars, 'problem': None}


def get_connection_chart(conn, first_org, first_dest, second_org, second_dest, day_of_week):
    if first_dest != second_org:
        return {
            'problem': f'{first_org.upper()}\u2192{first_dest.upper()} lands at {first_dest.upper()} '
                       f'but {second_org.upper()}\u2192{second_dest.upper()} leaves from {second_org.upper()}'
        }
    on_date = next_date_on(day_of_week)
    try:
        legs = [
            leg_bars(conn, first_org, first_dest, day_of_week, first_dest, on_date),
            leg_bars(conn, second_org, second_dest, day_of_week, first_dest, on_date),
        ]
    except UnconfirmedAirportError as error:
        return {'problem': str(error)}
    return {'problem': None, 'clockLabel': f'{first_dest.upper()} local time', 'legs': legs}
