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
    MIN_COUNTED_WEEKS_FOR_FULL_COLOR, airport_zone, counted_week_classes, format_minutes,
    get_t1_grid, next_date_on, route_duration_minutes, scheduled_dep_times,
    scheduled_service_finder, strike_rate, weekly_history,
)
from timezones import UnconfirmedAirportError, et_equivalent_datetime

# His stated preferences (2026-09-23). Minutes; values are 0-1.
LAYOVER_MAKEABLE = [(0, 0.0), (20, 0.10), (40, 0.70), (60, 1.0)]
DEPARTURE_COMFORT = [(6 * 60, 0.0), (6 * 60 + 40, 0.10), (7 * 60, 0.30), (9 * 60 + 30, 1.0)]
# Arrival is local time at the destination, counted from midnight of the departure day.
ARRIVAL_COMFORT = [(19 * 60, 1.0), (23 * 60, 0.30), (24 * 60 + 30, 0.10), (25 * 60 + 30, 0.0)]
# CVG is about 30 minutes further to drive than CMH, and they'd rather not drive it at all.
EXTRA_DRIVE_MINUTES = {'cvg': 30}
DISLIKED_DRIVE_FACTOR = {'cvg': 0.85}

COMFORT_LETTERS = [(0.85, 'A'), (0.65, 'B'), (0.45, 'C'), (0.25, 'D'), (0.0, 'F')]
IMPOSSIBLE_MARK = '\u2717'
THIN_MARK = '?'


def smooth_curve(points):
    """Smooth, monotone-between-points curve through the given points, flat
    beyond the first and last. The flat shoulders added here give PCHIP a
    zero slope at both ends, so clamping outside the range adds no kink."""
    minutes = [m for m, _ in points]
    values = [v for _, v in points]
    minutes = [minutes[0] - 60] + minutes + [minutes[-1] + 60]
    values = [values[0]] + values + [values[-1]]
    curve = PchipInterpolator(minutes, values)
    low, high = minutes[0], minutes[-1]
    return lambda m: float(curve(min(max(m, low), high)))


layover_makeable = smooth_curve(LAYOVER_MAKEABLE)
departure_comfort_at_home = smooth_curve(DEPARTURE_COMFORT)
arrival_comfort = smooth_curve(ARRIVAL_COMFORT)


def departure_comfort(org, local_minutes):
    shifted = local_minutes - EXTRA_DRIVE_MINUTES.get(org, 0)
    return departure_comfort_at_home(shifted) * DISLIKED_DRIVE_FACTOR.get(org, 1.0)


def comfort_letter(comfort):
    return next(letter for floor, letter in COMFORT_LETTERS if comfort >= floor)


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
                    flight.thin = len(classes) < MIN_COUNTED_WEEKS_FOR_FULL_COLOR
            self.flights.append(flight)


class Result:
    def __init__(self, comfort=None, likelihood=None, thin=False, impossible=False, problem=None):
        self.comfort, self.likelihood = comfort, likelihood
        self.thin, self.impossible, self.problem = thin, impossible, problem


def trip_comfort(first, last):
    return departure_comfort(first.org, first.dep_time) * arrival_comfort(last.arrival_minutes)


def connection_options(first, onward_flights):
    options = []
    for second in onward_flights:
        layover = (second.departs - first.arrives).total_seconds() / 60
        if layover <= 0:
            continue
        options.append((second, layover_makeable(layover)))
    return options


def first_leg_result(first, onward_flights):
    options = connection_options(first, onward_flights)
    if not options:
        return Result(impossible=True)
    known = [(second, makeable) for second, makeable in options if second.likelihood is not None]
    thin = first.thin or len(known) < len(options) or any(second.thin for second, _ in known)
    best_second = max(options, key=lambda o: trip_comfort(first, o[0]) * o[1] * (o[0].likelihood or 0))[0]
    comfort = trip_comfort(first, best_second)
    if first.likelihood is None:
        return Result(comfort=comfort, thin=True)
    all_onward_fail = 1.0
    for second, makeable in known:
        all_onward_fail *= 1.0 - makeable * second.likelihood
    return Result(comfort=comfort, likelihood=first.likelihood * (1.0 - all_onward_fail), thin=thin)


def second_leg_result(second, inbound_flights):
    options = []
    for first in inbound_flights:
        layover = (second.departs - first.arrives).total_seconds() / 60
        if layover > 0:
            options.append((first, layover_makeable(layover)))
    if not options:
        return Result(impossible=True)
    comfort_of = lambda first: trip_comfort(first, second)
    chance_of = lambda first, makeable: (first.likelihood or 0) * makeable * (second.likelihood or 0)
    best_first, makeable = max(options, key=lambda o: comfort_of(o[0]) * chance_of(*o))
    comfort = comfort_of(best_first)
    if second.likelihood is None or best_first.likelihood is None:
        return Result(comfort=comfort, thin=True)
    return Result(comfort=comfort, likelihood=chance_of(best_first, makeable),
                  thin=second.thin or best_first.thin)


def nonstop_result(flight):
    comfort = trip_comfort(flight, flight)
    return Result(comfort=comfort, likelihood=flight.likelihood, thin=flight.thin or flight.likelihood is None)


def result_text(result):
    if result.problem:
        return result.problem
    letter = comfort_letter(result.comfort) if result.comfort is not None else '\u2014'
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
                        result = first_leg_result(flight, linked_flights)
                    elif inbound:
                        result = second_leg_result(flight, linked_flights)
                    else:
                        result = nonstop_result(flight)
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
        text = result_text(best_result([result for _, result in entries]))
        if len(entries) == 1:
            return text
        return text + ' \u00b7 ' + ', '.join(sorted({name for name, _ in entries}, key=str.lower))
