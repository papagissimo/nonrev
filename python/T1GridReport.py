from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from clustering import SERVICE_GAP_MINUTES, cluster_services, service_representative
from PoolingSettingsDialog import excluded_date_where_clause
from Scenarios import studied_routes
from ServiceGrouping import get_route_services, load_open_full_settings
from settings import load_settings
from T1Estimator import compute_t1_replay_column
from timezones import UnconfirmedAirportError, et_equivalent_datetime, get_confirmed_timezone

DAYS_OF_WEEK = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
NO_ESTIMATE_DISPLAY = '\u2014'

STRIKE_WEIGHTS = {'open': 0.0, 'iffy': 0.25, 'full': 1.0}
RED_END_STRIKE_RATE = 0.5
MIN_COUNTED_WEEKS_FOR_FULL_COLOR = 3
FEW_WEEKS_WHITE_BLEND = 0.5
COLOR_RAMPS = [
    {'id': 'stoplight', 'label': 'Stoplight, smoothed',
     'stops': [(0.0, '#2e9e4f'), (0.5, '#f4d03f'), (1.0, '#d63a2f')]},
    {'id': 'muted', 'label': 'Stoplight, muted',
     'stops': [(0.0, '#6bb98a'), (0.5, '#f3dc7a'), (1.0, '#d9705a')]},
    {'id': 'threeColors', 'label': 'Just three colors',
     'steps': ['#3aa655', '#f4d03f', '#d9432f']},
    {'id': 'heatMap', 'label': 'Green-red heat map',
     'stops': [(0.0, '#1a9850'), (0.25, '#a6d96a'), (0.5, '#fee08b'), (0.75, '#f46d43'), (1.0, '#a50026')]},
    {'id': 'blueOrange', 'label': 'Blue to orange (colorblind-safe)',
     'stops': [(0.0, '#2c7bb6'), (0.5, '#efe3b0'), (1.0, '#e66101')]},
    {'id': 'weatherMap', 'label': 'Weather map (blue to red)',
     'stops': [(0.0, '#2c7bb6'), (0.25, '#7fc4d8'), (0.5, '#f4d35e'), (0.75, '#f28e4b'), (1.0, '#c8302f')]},
]
DEFAULT_RAMP_ID = 'stoplight'
NO_HISTORY_FILL = '#c9ced6'
DARK_TEXT = '#222222'
LIGHT_TEXT = '#ffffff'
LEGEND_OPEN_LABEL = 'always open'
LEGEND_FULL_LABEL = 'full half the time or more'
DAY_NAMES = {'Mon': 'Monday', 'Tue': 'Tuesday', 'Wed': 'Wednesday', 'Thu': 'Thursday',
             'Fri': 'Friday', 'Sat': 'Saturday', 'Sun': 'Sunday'}


def routes_with_history(conn):
    rows = conn.execute(
        """SELECT DISTINCT o.org, o.dest, rs.durationMinutes
           FROM observations o
           LEFT JOIN routeSettings rs ON rs.org = o.org AND rs.dest = o.dest
           ORDER BY o.org, o.dest"""
    ).fetchall()
    routes = [{'org': org, 'dest': dest, 'durationMinutes': duration} for org, dest, duration in rows]
    being_studied = studied_routes(conn)
    return (
        [r for r in routes if (r['org'], r['dest']) in being_studied]
        + [r for r in routes if (r['org'], r['dest']) not in being_studied]
    )


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
                'lastReadingHours': readings[0]['hrs'],
                't1': t1,
            })
    return instances


def get_t1_grid(conn, org, dest, day_of_week):
    find_scheduled_service = scheduled_service_finder(conn, org, dest, day_of_week)
    golden_ticket_hours = load_settings(conn).get('goldenTicketHours', 1.5)
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
                {
                    't1': cells[date]['t1'],
                    'display': format_t1(cells[date]['t1']),
                    'countsForColor': cells[date]['lastReadingHours'] <= golden_ticket_hours,
                } if date in cells else None
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


def airport_zone(conn, airport):
    return ZoneInfo(get_confirmed_timezone(conn, airport))


def minutes_on_clock(instant, clock_zone, on_date):
    wall_time = instant.astimezone(clock_zone).replace(tzinfo=None)
    return int((wall_time - datetime.combine(on_date, time.min)).total_seconds() // 60)


def format_local_moment(moment, on_date):
    next_day_marker = f' +{(moment.date() - on_date).days}' if moment.date() > on_date else ''
    return f'{moment:%H:%M} {moment.tzname()}{next_day_marker}'


def hex_to_rgb(color):
    return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))


def rgb_to_hex(rgb):
    return '#' + ''.join(f'{round(channel):02x}' for channel in rgb)


def ramp_rgb(ramp, position):
    if 'steps' in ramp:
        step = min(int(position * len(ramp['steps'])), len(ramp['steps']) - 1)
        return hex_to_rgb(ramp['steps'][step])
    stops = ramp['stops']
    for (low_at, low_color), (high_at, high_color) in zip(stops, stops[1:]):
        if position <= high_at:
            fraction = (position - low_at) / (high_at - low_at)
            low, high = hex_to_rgb(low_color), hex_to_rgb(high_color)
            return tuple(low[i] + (high[i] - low[i]) * fraction for i in range(3))
    return hex_to_rgb(stops[-1][1])


def ramp_preview_stops(ramp):
    if 'stops' in ramp:
        return [{'offset': at, 'color': color} for at, color in ramp['stops']]
    count = len(ramp['steps'])
    preview = []
    for index, color in enumerate(ramp['steps']):
        preview.append({'offset': index / count, 'color': color})
        preview.append({'offset': (index + 1) / count, 'color': color})
    return preview


def blend_toward_white(rgb, amount):
    return tuple(channel + (255 - channel) * amount for channel in rgb)


def relative_luminance(rgb):
    def linear(channel):
        value = channel / 255
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4
    red, green, blue = (linear(channel) for channel in rgb)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def readable_text_color(rgb):
    luminance = relative_luminance(rgb)
    contrast_with_white = 1.05 / (luminance + 0.05)
    contrast_with_dark = (luminance + 0.05) / (relative_luminance(hex_to_rgb(DARK_TEXT)) + 0.05)
    return LIGHT_TEXT if contrast_with_white >= contrast_with_dark else DARK_TEXT


def classify_week(t1, thresholds):
    if t1 is None:
        return None
    if t1 <= thresholds['fullThreshold']:
        return 'full'
    if t1 >= thresholds['openThreshold']:
        return 'open'
    return 'iffy'


def service_appearance(weeks, thresholds):
    classes = [classify_week(week['t1'], thresholds) for week in weeks if week['countsForColor']]
    classes = [c for c in classes if c]
    if not classes:
        no_history = {'fill': NO_HISTORY_FILL, 'textColor': DARK_TEXT}
        return {'fills': {ramp['id']: no_history for ramp in COLOR_RAMPS}, 'tally': None}
    strike_rate = sum(STRIKE_WEIGHTS[c] for c in classes) / len(classes)
    position = min(strike_rate / RED_END_STRIKE_RATE, 1.0)
    fills = {}
    for ramp in COLOR_RAMPS:
        rgb = ramp_rgb(ramp, position)
        if len(classes) < MIN_COUNTED_WEEKS_FOR_FULL_COLOR:
            rgb = blend_toward_white(rgb, FEW_WEEKS_WHITE_BLEND)
        fills[ramp['id']] = {'fill': rgb_to_hex(rgb), 'textColor': readable_text_color(rgb)}
    return {
        'fills': fills,
        'tally': {'weeks': len(classes), 'open': classes.count('open'),
                  'iffy': classes.count('iffy'), 'full': classes.count('full')},
    }


def weekly_history(grid, service_label):
    row = next((r for r in grid['rows'] if r['label'] == service_label), None)
    if row is None:
        return []
    weeks = [
        {'date': header, 't1': cell['t1'], 'display': cell['display'], 'countsForColor': cell['countsForColor']}
        for header, cell in zip(grid['dateHeaders'], row['cells']) if cell
    ]
    return list(reversed(weeks))


def bar_tooltip(route_name, day_of_week, departure_text, arrival_text, weeks, tally):
    lines = [f'{route_name} \u00b7 {DAY_NAMES[day_of_week]}', f'Departs {departure_text}', f'Arrives {arrival_text}']
    if tally:
        lines.append(f"Counted {tally['weeks']} weeks: open {tally['open']} \u00b7 iffy {tally['iffy']} \u00b7 full {tally['full']}")
    for week in weeks:
        soft_note = '' if week['countsForColor'] else '  (no late reading, not counted)'
        lines.append(f"{week['date']}  {week['display']}{soft_note}")
    return '\n'.join(lines)


def leg_bars(conn, org, dest, day_of_week, clock_airport, on_date, thresholds):
    route_name = f'{org.upper()}\u2192{dest.upper()}'
    duration = route_duration_minutes(conn, org, dest)
    if duration is None:
        return {'route': route_name, 'bars': [],
                'problem': f'no flight time on file for {route_name} (set it in Edit Schedule)'}
    dep_times = scheduled_dep_times(conn, org, dest, day_of_week)
    if not dep_times:
        return {'route': route_name, 'bars': [], 'problem': f'no {day_of_week} flights scheduled for {route_name}'}

    find_scheduled_service = scheduled_service_finder(conn, org, dest, day_of_week)
    grid = get_t1_grid(conn, org, dest, day_of_week)
    clock_zone = airport_zone(conn, clock_airport)
    origin_zone = airport_zone(conn, org)
    dest_zone = airport_zone(conn, dest)
    bars = []
    for dep_time in dep_times:
        departure = et_equivalent_datetime(conn, dep_time, org, on_date)
        arrival = departure.astimezone(timezone.utc) + timedelta(minutes=duration)
        service_minutes = find_scheduled_service(dep_time)
        if service_minutes is None:
            service_minutes = service_representative([dep_time])
        label = format_minutes(service_minutes)
        weeks = weekly_history(grid, label)
        appearance = service_appearance(weeks, thresholds)
        bars.append({
            'label': label,
            'startMinutes': minutes_on_clock(departure, clock_zone, on_date),
            'endMinutes': minutes_on_clock(arrival, clock_zone, on_date),
            'weeks': [{'date': w['date'], 'display': w['display'], 'counted': w['countsForColor']} for w in weeks],
            'fills': appearance['fills'],
            'tooltip': bar_tooltip(
                route_name, day_of_week,
                format_local_moment(departure.astimezone(origin_zone), on_date),
                format_local_moment(arrival.astimezone(dest_zone), on_date),
                weeks, appearance['tally'],
            ),
        })
    return {'route': route_name, 'bars': bars, 'problem': None}


def axis_description(conn, airport, clock_airport, on_date):
    noon = datetime.combine(on_date, time(12))
    airport_moment = noon.replace(tzinfo=airport_zone(conn, airport))
    clock_moment = noon.replace(tzinfo=airport_zone(conn, clock_airport))
    offset = airport_moment.utcoffset() - clock_moment.utcoffset()
    return {
        'airport': airport.upper(),
        'zone': airport_moment.tzname(),
        'offsetMinutes': int(offset.total_seconds() // 60),
    }


def legend_description():
    return {
        'ramps': [{'id': ramp['id'], 'label': ramp['label'], 'previewStops': ramp_preview_stops(ramp)}
                  for ramp in COLOR_RAMPS],
        'defaultRampId': DEFAULT_RAMP_ID,
        'openLabel': LEGEND_OPEN_LABEL,
        'fullLabel': LEGEND_FULL_LABEL,
        'fewWeeksBelow': MIN_COUNTED_WEEKS_FOR_FULL_COLOR,
    }


def get_connection_chart(conn, first_org, first_dest, second_org, second_dest, day_of_week):
    if first_dest != second_org:
        return {
            'problem': f'{first_org.upper()}\u2192{first_dest.upper()} lands at {first_dest.upper()} '
                       f'but {second_org.upper()}\u2192{second_dest.upper()} leaves from {second_org.upper()}'
        }
    on_date = next_date_on(day_of_week)
    thresholds = load_open_full_settings(conn)
    try:
        legs = [
            leg_bars(conn, first_org, first_dest, day_of_week, first_dest, on_date, thresholds),
            leg_bars(conn, second_org, second_dest, day_of_week, first_dest, on_date, thresholds),
        ]
        axes = {
            'left': axis_description(conn, first_org, first_dest, on_date),
            'right': axis_description(conn, second_dest, first_dest, on_date),
        }
    except UnconfirmedAirportError as error:
        return {'problem': str(error)}
    return {'problem': None, 'axes': axes, 'legs': legs, 'legend': legend_description()}
