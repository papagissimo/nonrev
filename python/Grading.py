"""
Computed flight grades for the logging dialog: a comfort letter and a
likelihood percent, e.g. "B 70".

Likelihood: a leg's chance is 1 minus its strike rate from the T1 grid
report. A connection needs its first leg, then at least one onward flight
out of the connecting airport; every later onward flight is another
chance, so backups raise the number. Each chance is scaled by how
makeable its layover is.

Comfort: departure-hour curve on the first leg, arrival-hour curve on the
last leg, multiplied together.

Itineraries are inferred per scenario and weekday: a cell whose origin is
another cell's destination is that cell's onward leg. A flight in several
switched-on scenarios shows the best letter and best percent found for it,
even when they come from different itineraries - the label says how
important a flight is to log.
"""

from datetime import timedelta

from scipy.interpolate import PchipInterpolator

from ServiceGrouping import load_open_full_settings
from T1GridReport import (
    airport_zone, counted_week_classes, format_minutes,
    get_t1_grid, next_date_on, route_duration_minutes, scheduled_dep_times,
    scheduled_service_finder, strike_rate, weekly_history,
)
from settings import load_settings, save_settings
from timezones import UnconfirmedAirportError, et_equivalent_datetime

GRADING_SETTINGS_KEY = 'gradingSettings'
# Starting values are his stated preferences (2026-09-23), tuned from the
# logging dialog's Settings panel. Curves are [minutes, percent] points;
# arrival minutes count from midnight of the departure day, so past 24:00
# means after midnight.
DEFAULT_GRADING_SETTINGS = {
    'layoverMakeable': [[0, 0], [20, 10], [40, 70], [60, 100]],
    'departureComfort': [[6 * 60, 0], [6 * 60 + 40, 10], [7 * 60, 30], [9 * 60 + 30, 100]],
    'arrivalComfort': [[19 * 60, 100], [23 * 60, 30], [24 * 60 + 30, 10], [25 * 60 + 30, 0]],
    # CVG is about 30 minutes further to drive than CMH, and they'd rather not drive it at all.
    'homeAirports': {
        'cmh': {'extraDriveMinutes': 0, 'comfortPercent': 100},
        'cvg': {'extraDriveMinutes': 30, 'comfortPercent': 85},
    },
    'letterFloors': {'A': 85, 'B': 65, 'C': 45, 'D': 25},
}
CURVE_LABELS = {'layoverMakeable': 'Layover makeable', 'departureComfort': 'Departure comfort',
                'arrivalComfort': 'Arrival comfort'}
CURVE_NAMES = list(CURVE_LABELS)
LETTERS = ['A', 'B', 'C', 'D']

IMPOSSIBLE_MARK = '\u2717'
THIN_MARK = '?'


def load_grading_settings(conn):
    return load_settings(conn, key=GRADING_SETTINGS_KEY, defaults=DEFAULT_GRADING_SETTINGS)


def format_point_minutes(minutes, as_clock):
    return f'{minutes // 60}:{minutes % 60:02d}' if as_clock else f'{minutes}m'


def curve_text(points, as_clock):
    return ', '.join(f'{format_point_minutes(m, as_clock)} {v:g}%' for m, v in points)


def settings_form(settings):
    """The settings as the panel shows them: each curve as editable text."""
    form = dict(settings)
    for name in CURVE_NAMES:
        form[name] = curve_text(settings[name], as_clock=name != 'layoverMakeable')
    return form


def parse_point_minutes(token, curve_label):
    try:
        if ':' in token:
            hours, minutes = token.split(':')
            if len(minutes) != 2 or not 0 <= int(minutes) < 60:
                raise ValueError
            return int(hours) * 60 + int(minutes)
        return int(token.rstrip('m'))
    except ValueError:
        raise ValueError(f'{curve_label}: "{token}" is not a time like 7:30 or minutes like 40m')


def parse_percent(token, curve_label):
    try:
        value = float(token.rstrip('%'))
    except ValueError:
        raise ValueError(f'{curve_label}: "{token}" is not a percent')
    if not 0 <= value <= 100:
        raise ValueError(f'{curve_label}: {token} is outside 0-100%')
    return value


def parse_curve_text(text, curve_label):
    points = []
    for pair in str(text).split(','):
        tokens = pair.split()
        if len(tokens) != 2:
            raise ValueError(f'{curve_label}: "{pair.strip()}" should be a time and a percent, e.g. 7:00 30%')
        points.append([parse_point_minutes(tokens[0], curve_label), parse_percent(tokens[1], curve_label)])
    if len(points) < 2:
        raise ValueError(f'{curve_label}: needs at least two points')
    if any(later[0] <= earlier[0] for earlier, later in zip(points, points[1:])):
        raise ValueError(f'{curve_label}: times must go strictly later, left to right')
    return points


def number_in_range(value, label, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise ValueError(f'{label} must be a number from {low} to {high}')
    return value


def parse_grading_form(form):
    """The panel's form back into stored settings, or ValueError naming what's wrong."""
    settings = {name: parse_curve_text(form.get(name, ''), CURVE_LABELS[name]) for name in CURVE_NAMES}
    settings['homeAirports'] = {
        airport.lower(): {
            'extraDriveMinutes': number_in_range(values.get('extraDriveMinutes'), f'{airport.upper()} extra drive', -180, 180),
            'comfortPercent': number_in_range(values.get('comfortPercent'), f'{airport.upper()} comfort', 0, 100),
        }
        for airport, values in (form.get('homeAirports') or {}).items()
    }
    floors = {letter: number_in_range((form.get('letterFloors') or {}).get(letter), f'{letter} floor', 0, 100)
              for letter in LETTERS}
    if any(floors[later] >= floors[earlier] for earlier, later in zip(LETTERS, LETTERS[1:])):
        raise ValueError('Letter floors must go down from A to D')
    settings['letterFloors'] = floors
    return settings


def save_grading_form(conn, form):
    save_settings(conn, parse_grading_form(form), key=GRADING_SETTINGS_KEY)


def smooth_curve(points):
    """Smooth curve through the given [minutes, percent] points, never
    overshooting between them, flat beyond the first and last. The flat
    shoulders added here give PCHIP a zero slope at both ends, so clamping
    outside the range adds no kink. Returns a 0-1 fraction."""
    minutes = [m for m, _ in points]
    values = [v / 100 for _, v in points]
    minutes = [minutes[0] - 60] + minutes + [minutes[-1] + 60]
    values = [values[0]] + values + [values[-1]]
    curve = PchipInterpolator(minutes, values)
    low, high = minutes[0], minutes[-1]
    return lambda m: float(curve(min(max(m, low), high)))


class GradingModel:
    def __init__(self, settings):
        self.layover_makeable = smooth_curve(settings['layoverMakeable'])
        self.departure_comfort_at_home = smooth_curve(settings['departureComfort'])
        self.arrival_comfort = smooth_curve(settings['arrivalComfort'])
        self.home_airports = settings['homeAirports']
        floors = settings['letterFloors']
        self.letter_floors = [(floors[letter] / 100, letter) for letter in LETTERS] + [(0.0, 'F')]

    def departure_comfort(self, org, local_minutes):
        home = self.home_airports.get(org, {'extraDriveMinutes': 0, 'comfortPercent': 100})
        shifted = local_minutes - home['extraDriveMinutes']
        return self.departure_comfort_at_home(shifted) * home['comfortPercent'] / 100

    def trip_comfort(self, first, last):
        return self.departure_comfort(first.org, first.dep_time) * self.arrival_comfort(last.arrival_minutes)

    def letter(self, comfort):
        return next(letter for floor, letter in self.letter_floors if comfort >= floor)


class Flight:
    """dep_time is local minutes at the origin, as flightSchedule stores it."""

    def __init__(self, conn, org, dest, dep_time, duration, on_date):
        self.org, self.dest, self.dep_time = org, dest, dep_time
        self.departs = et_equivalent_datetime(conn, dep_time, org, on_date)
        self.arrives = self.departs + timedelta(minutes=duration)
        local_arrival = self.arrives.astimezone(airport_zone(conn, dest))
        self.arrival_minutes = (local_arrival.date() - on_date).days * 24 * 60 \
            + local_arrival.hour * 60 + local_arrival.minute
        self.likelihood = None
        self.thin = True


class RouteDay:
    """One route's scheduled flights on one weekday, each with its likelihood."""

    def __init__(self, conn, org, dest, day, thresholds):
        self.problem = None
        self.flights = []
        duration = route_duration_minutes(conn, org, dest)
        if duration is None:
            self.problem = f'no flight time for {org.upper()}\u2192{dest.upper()}'
            return
        on_date = next_date_on(day)
        grid = get_t1_grid(conn, org, dest, day)
        find_service = scheduled_service_finder(conn, org, dest, day)
        for dep_time in scheduled_dep_times(conn, org, dest, day):
            flight = Flight(conn, org, dest, dep_time, duration, on_date)
            service_minutes = find_service(dep_time)
            if service_minutes is not None:
                classes = counted_week_classes(weekly_history(grid, format_minutes(service_minutes)), thresholds)
                if classes:
                    flight.likelihood = 1.0 - strike_rate(classes)
                    flight.thin = False
            self.flights.append(flight)


class Result:
    def __init__(self, comfort=None, likelihood=None, thin=False, impossible=False, problem=None):
        self.comfort, self.likelihood = comfort, likelihood
        self.thin, self.impossible, self.problem = thin, impossible, problem


def layover_minutes(first, second):
    return (second.departs - first.arrives).total_seconds() / 60


def first_leg_result(model, first, onward_flights):
    options = [(second, model.layover_makeable(layover_minutes(first, second)))
               for second in onward_flights if layover_minutes(first, second) > 0]
    if not options:
        return Result(impossible=True)
    known = [(second, makeable) for second, makeable in options if second.likelihood is not None]
    thin = first.thin or len(known) < len(options) or any(second.thin for second, _ in known)
    best_second = max(options, key=lambda o: model.trip_comfort(first, o[0]) * o[1] * (o[0].likelihood or 0))[0]
    comfort = model.trip_comfort(first, best_second)
    if first.likelihood is None:
        return Result(comfort=comfort, thin=True)
    all_onward_fail = 1.0
    for second, makeable in known:
        all_onward_fail *= 1.0 - makeable * second.likelihood
    return Result(comfort=comfort, likelihood=first.likelihood * (1.0 - all_onward_fail), thin=thin)


def second_leg_result(model, second, inbound_flights):
    options = [(first, model.layover_makeable(layover_minutes(first, second)))
               for first in inbound_flights if layover_minutes(first, second) > 0]
    if not options:
        return Result(impossible=True)
    comfort_of = lambda first: model.trip_comfort(first, second)
    chance_of = lambda first, makeable: (first.likelihood or 0) * makeable * (second.likelihood or 0)
    best_first, makeable = max(options, key=lambda o: comfort_of(o[0]) * chance_of(*o))
    comfort = comfort_of(best_first)
    if second.likelihood is None or best_first.likelihood is None:
        return Result(comfort=comfort, thin=True)
    return Result(comfort=comfort, likelihood=chance_of(best_first, makeable),
                  thin=second.thin or best_first.thin)


def nonstop_result(model, flight):
    comfort = model.trip_comfort(flight, flight)
    return Result(comfort=comfort, likelihood=flight.likelihood, thin=flight.thin or flight.likelihood is None)


def result_text(model, result):
    if result.problem:
        return result.problem
    letter = model.letter(result.comfort) if result.comfort is not None else '\u2014'
    if result.impossible:
        return f'{letter} {IMPOSSIBLE_MARK}'
    if result.likelihood is None:
        return f'{letter} {THIN_MARK}'
    percent = int(round(result.likelihood * 10)) * 10
    return f'{letter} {percent}{THIN_MARK if result.thin else ""}'


def best_result(results):
    real = [r for r in results if not r.problem]
    if not real:
        return results[0]
    possible = [r for r in real if not r.impossible]
    if not possible:
        return max(real, key=lambda r: r.comfort if r.comfort is not None else -1)
    comforts = [r.comfort for r in possible if r.comfort is not None]
    with_likelihood = [r for r in possible if r.likelihood is not None]
    if not with_likelihood:
        return Result(comfort=max(comforts) if comforts else None, thin=True)
    most_likely = max(with_likelihood, key=lambda r: r.likelihood)
    return Result(comfort=max(comforts) if comforts else None,
                  likelihood=most_likely.likelihood, thin=most_likely.thin)


class FlightGrades:
    """Computed grades from switched-on scenarios, looked up per scheduled
    flight. Built once per logging batch; each scenario-day is worked out
    the first time one of its flights is asked for."""

    def __init__(self, conn):
        self.conn = conn
        self.thresholds = load_open_full_settings(conn)
        self.model = GradingModel(load_grading_settings(conn))
        self.route_days = {}
        self.cells_by_day = {}
        for name, org, dest, day in conn.execute(
            """SELECT s.name, c.org, c.dest, c.dayOfWeek
               FROM scenarioCells c JOIN scenarios s ON s.id = c.scenarioId
               WHERE s.active = 1"""
        ):
            self.cells_by_day.setdefault(day, {}).setdefault(name, set()).add((org, dest))
        self.results_by_day = {}

    def route_day(self, org, dest, day):
        key = (org, dest, day)
        if key not in self.route_days:
            self.route_days[key] = RouteDay(self.conn, org, dest, day, self.thresholds)
        return self.route_days[key]

    def scenario_results(self, day, cells):
        """{(org, dest, depTime): Result} for one scenario's cells on one day."""
        results = {}
        for org, dest in cells:
            inbound = [c for c in cells if c[1] == org]
            onward = [c for c in cells if c[0] == dest]
            try:
                route = self.route_day(org, dest, day)
                if route.problem:
                    for dep_time in scheduled_dep_times(self.conn, org, dest, day):
                        results[(org, dest, dep_time)] = Result(problem=route.problem)
                    continue
                if inbound and onward:
                    for flight in route.flights:
                        results[(org, dest, flight.dep_time)] = Result(problem='not graded: 3+ legs')
                    continue
                linked_routes = [self.route_day(o, d, day) for o, d in (inbound or onward)]
                linked_problem = next((r.problem for r in linked_routes if r.problem), None)
                linked_flights = [f for r in linked_routes for f in r.flights]
                for flight in route.flights:
                    if linked_problem:
                        result = Result(problem=linked_problem)
                    elif onward:
                        result = first_leg_result(self.model, flight, linked_flights)
                    elif inbound:
                        result = second_leg_result(self.model, flight, linked_flights)
                    else:
                        result = nonstop_result(self.model, flight)
                    results[(org, dest, flight.dep_time)] = result
            except UnconfirmedAirportError as error:
                for dep_time in scheduled_dep_times(self.conn, org, dest, day):
                    results[(org, dest, dep_time)] = Result(problem=str(error))
        return results

    def results_for_day(self, day):
        if day not in self.results_by_day:
            by_flight = {}
            for name, cells in self.cells_by_day.get(day, {}).items():
                for key, result in self.scenario_results(day, cells).items():
                    by_flight.setdefault(key, []).append((name, result))
            self.results_by_day[day] = by_flight
        return self.results_by_day[day]

    def text_for(self, org, dest, day, dep_time):
        entries = self.results_for_day(day).get((org, dest, dep_time), [])
        if not entries:
            return ''
        text = result_text(self.model, best_result([result for _, result in entries]))
        if len(entries) == 1:
            return text
        return text + ' \u00b7 ' + ', '.join(sorted({name for name, _ in entries}, key=str.lower))
