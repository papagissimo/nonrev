"""
The live T1 estimator: the one T1 value shown anywhere in the app (his call:
he never wants to see two different T1 numbers side by side).

held_t1_replay_column holds the latest reading. Per cabin, independently,
the held value is that cabin's most recent KNOWN reading, unchanged - not
necessarily the most recent reading overall, since a "9, blank, blank"
partial entry deliberately leaves other cabins unlogged rather than implying
zero. A cabin never logged contributes nothing to the total, not zero; a row
where no cabin has been logged yet has nothing held (None).

compute_t1_replay_column is the live estimate: each row's held total plus the
typical move from a reading that far out (DriftCurve), at that row's own
hours before departure. Inside T-6 the move is zero, so there the estimate is
the held total.

curve_t1_replay_column is the decline-curve estimator the live one replaced,
kept for the backtests that measure it. Per cabin: take the pooled (c1,
slope, nightRatio) for this exact (org, dest, dayOfWeek, depTime, cabin),
resolved through DeclineCurveHierarchy. If the cabin's most recent known
reading is interior (1-8), slide the curve through it exactly and predict
forward to T1_TARGET_HOURS. If it's at a rail (9 or 0), compare it against
what the pooled curve alone predicts at the same hoursBeforeDep: within
declineCurveSettings' stepChangeRmseThreshold, use the pooled curve as-is;
beyond it, or with no pooled C1 to compare against, slide through the
reading. A cabin with no resolvable slope, or never logged, contributes
nothing; a row where every cabin is unresolvable is None.
"""

from datetime import datetime

from DeclineCurveFit import piecewise_model, solve_c1_from_reading, CABIN_COLUMNS
from DeclineCurveHierarchy import resolve_coefficients
from DriftCurve import current_points, shifted
from settings import load_settings, DECLINE_CURVE_SETTINGS_KEY, DEFAULT_DECLINE_CURVE_SETTINGS
from timezones import et_equivalent_datetime, UnconfirmedAirportError

T1_TARGET_HOURS = 1.0

CABIN_KEY_TO_COLUMN = {'y': 'y', 'cplus': 'cPlus', 'onePS': 'firstOrPS', 'd1': 'd1'}


def load_decline_curve_coefficients_for_flight(conn, org, dest, day_of_week, dep_time):
    """dict cabin_column -> (c1Hours, slopeSeatsPerHour, nightSlopeRatio),
    one entry per cabin - resolved through the full coefficients
    hierarchy (see DeclineCurveHierarchy.resolve_coefficients): derived
    data from declineCurveCoefficients when there's enough of it, else a
    hand-set service or route override, else the global default (for
    nightSlopeRatio, currently always the global default - tier 4
    derivation for it isn't built yet). A cabin only ever comes back
    missing from this dict if EVERY tier including the global default
    has no slope for it - practically shouldn't happen once the global
    default is filled in, but not assumed away."""
    result = {}
    for cabin_col in CABIN_COLUMNS:
        resolved = resolve_coefficients(conn, org, dest, day_of_week, dep_time, cabin_col)
        if resolved['slope'] is not None:
            result[cabin_col] = (resolved['c1'], resolved['slope'], resolved.get('nightRatio') or 1.0)
    return result


def latest_known_readings(readings, idx):
    """cabin_key -> (hrs, value) of each cabin's most recent known reading as
    of row idx (readings[idx:], since the list counts down in hrs)."""
    latest = {}
    for row in readings[idx:]:
        for cabin_key in CABIN_KEY_TO_COLUMN:
            if cabin_key not in latest and row.get(cabin_key) is not None:
                latest[cabin_key] = (row['hrs'], row[cabin_key])
    return latest


def held_t1_replay_column(readings):
    """readings: as returned by SeatLoggingDialog.previous_readings_for -
    most-recent-first (ascending hrs), each {'hrs':, 'y':, 'cplus':,
    'onePS':, 'd1':}, None where that cabin wasn't logged on that check.

    Returns a list the same length as readings - one held total per row,
    replay-style: the sum over cabins of each cabin's most recent known
    reading as of that row, or None where no cabin has been logged yet."""
    results = []
    for idx in range(len(readings)):
        latest = latest_known_readings(readings, idx)
        results.append(sum(value for _, value in latest.values()) if latest else None)
    return results


def compute_t1_replay_column(conn, org, dest, flight_date, dep_time, readings):
    """Same readings and return shape as held_t1_replay_column; each row's
    held total shifted by DriftCurve's typical move at that row's hrs."""
    points = current_points(conn)
    return [shifted(held, points, reading['hrs'])
            for held, reading in zip(held_t1_replay_column(readings), readings)]


def curve_t1_replay_column(conn, org, dest, flight_date, dep_time, readings):
    """Same readings and return shape as held_t1_replay_column, but each
    row's estimate is the decline-curve prediction described in the module
    docstring.

    Each cabin's resolved nightSlopeRatio and this flight's departure
    timestamp are threaded into every piecewise_model/solve_c1_from_reading
    call, so overnight hours decline slower. departure_dt comes from
    et_equivalent_datetime (dep_time is minutes-since-midnight ORIGIN-local,
    not HHMM digits), then treated as naive ET - the zone checkTimestamp is
    logged in, and the zone the night-ratio default was calibrated against."""
    day_of_week = datetime.strptime(flight_date, "%Y-%m-%d").strftime("%a")
    coeffs = load_decline_curve_coefficients_for_flight(conn, org, dest, day_of_week, dep_time)
    if not coeffs:
        return [None] * len(readings)

    settings = load_settings(conn, key=DECLINE_CURVE_SETTINGS_KEY, defaults=DEFAULT_DECLINE_CURVE_SETTINGS)
    unexpected_threshold = settings['stepChangeRmseThreshold']
    night_start_hour = settings['nightStartHour']
    night_end_hour = settings['nightEndHour']

    departure_dt = None
    try:
        flight_date_obj = datetime.strptime(flight_date, "%Y-%m-%d").date()
        departure_dt = et_equivalent_datetime(conn, dep_time, org, flight_date_obj).replace(tzinfo=None)
    except (ValueError, TypeError, UnconfirmedAirportError):
        departure_dt = None

    results = []
    for idx in range(len(readings)):
        pool = readings[idx:]  # this row + everything chronologically earlier
        total = 0.0
        any_resolved = False
        for cabin_key, cabin_col in CABIN_KEY_TO_COLUMN.items():
            c1_slope_night = coeffs.get(cabin_col)
            if c1_slope_night is None:
                continue
            pooled_c1, slope, night_ratio = c1_slope_night

            anchor = None
            for row in pool:
                v = row.get(cabin_key)
                if v is not None:
                    anchor = (row['hrs'], v)
                    break
            if anchor is None:
                continue

            hbd_r, val_r = anchor
            if 1 <= val_r <= 8:
                # Interior reading - well-determined, solve for the C1
                # that makes the curve pass through it exactly.
                c1_prime = solve_c1_from_reading(hbd_r, val_r, slope, night_ratio, departure_dt,
                                                  night_start_hour, night_end_hour)
            elif pooled_c1 is None:
                # Rail reading, but nothing to compare it against -
                # slide anyway, it's the only information available.
                c1_prime = solve_c1_from_reading(hbd_r, val_r, slope, night_ratio, departure_dt,
                                                  night_start_hour, night_end_hour)
            else:
                pooled_val_at_hbd_r = float(piecewise_model(
                    hbd_r, pooled_c1, slope, night_ratio, departure_dt,
                    night_start_hour, night_end_hour,
                ))
                if abs(val_r - pooled_val_at_hbd_r) > unexpected_threshold:
                    # Unexpected - genuinely surprising given the pooled
                    # curve, slide on it (his terms: "unexpectedly early
                    # zero" / "unexpectedly late nine").
                    c1_prime = solve_c1_from_reading(hbd_r, val_r, slope, night_ratio, departure_dt,
                                                      night_start_hour, night_end_hour)
                else:
                    # Expected - the pooled curve already explains this
                    # reading, nothing new to slide on.
                    c1_prime = pooled_c1

            total += float(piecewise_model(
                T1_TARGET_HOURS, c1_prime, slope, night_ratio, departure_dt,
                night_start_hour, night_end_hour,
            ))
            any_resolved = True

        results.append(total if any_resolved else None)
    return results
