"""
NightZoneBacktest.py

One-off analysis: which definition of "night" makes the live T1 estimator
predict best? Report only - nothing is written to the database, and the
live fit is untouched once the script exits.

Definitions compared, each using the nightStartHour/nightEndHour setting
as local clock hours:
  current   night by the Eastern clock, whatever the route (the live code)
  origin    night at the origin airport
  and       night at both ends at once
  or        night at either end
  half      night weight 1 where both ends are in night, 1/2 where only one is

Every definition runs through the same live fitting, pooling and
estimator code; only the night-hour count differs. Each route is fitted
on its own so the night count knows which two zones it is working in -
services never span routes, so this changes nothing else.

Scoring is leave-one-out for C1, slope AND night ratio, starting from the
raw reading(s) bracketing each horizon and scored against the raw reading
nearest T-1 (within GOOD_OBSERVATION_CUTOFF_HOURS). Only instances scored
under every definition are counted, so the definitions are compared on
identical cases.
"""
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, time as dt_time, timezone
from zoneinfo import ZoneInfo

import numpy as np

import DeclineCurveFit
import T1Estimator
from DeclineCurveFit import (
    DB_PATH, compute_all_fits, pool_slope, pool_c1, pool_night_ratio,
    bracket_with_weight, nearest_reading, T1_TARGET_HOURS_FOR_POOLING,
)
from DeclineCurveHierarchy import _load_threshold, _load_service_override, _load_route_override
from T4T1Backtest import (
    GOOD_OBSERVATION_CUTOFF_HOURS, CABIN_COLUMN_TO_KEY,
    group_keys_by_service, resolve_leave_one_out_coefficients,
    coefficients_supplied_to_live_estimator,
)
from settings import (
    load_settings, DECLINE_CURVE_SETTINGS_KEY, DEFAULT_DECLINE_CURVE_SETTINGS,
    DECLINE_CURVE_GLOBAL_DEFAULTS_KEY, DEFAULT_DECLINE_CURVE_GLOBAL_DEFAULTS,
)
from timezones import get_confirmed_timezone

DEFINITIONS = ["current", "origin", "and", "or", "half"]
HORIZONS = [4.0, 12.0]
CABINS = ["y", "cPlus", "firstOrPS"]
EASTERN = ZoneInfo("America/New_York")

original_night_overlap_hours = DeclineCurveFit._night_overlap_hours
original_load_observations = DeclineCurveFit.load_observations

active = {"definition": "current", "org": None, "dest": None,
          "org_zone": None, "dest_zone": None}


def night_intervals_utc(start_utc, end_utc, zone, night_start_hour, night_end_hour):
    intervals = []
    day = start_utc.astimezone(zone).date() - timedelta(days=1)
    last_day = end_utc.astimezone(zone).date()
    while day <= last_day:
        opens = datetime.combine(day, dt_time(night_start_hour), tzinfo=zone).astimezone(timezone.utc)
        closes = datetime.combine(day + timedelta(days=1), dt_time(night_end_hour),
                                  tzinfo=zone).astimezone(timezone.utc)
        lo, hi = max(start_utc, opens), min(end_utc, closes)
        if hi > lo:
            intervals.append((lo, hi))
        day += timedelta(days=1)
    return intervals


def total_hours(intervals):
    return sum((hi - lo).total_seconds() for lo, hi in intervals) / 3600.0


def overlap_hours(first, second):
    seconds = 0.0
    for lo_a, hi_a in first:
        for lo_b, hi_b in second:
            lo, hi = max(lo_a, lo_b), min(hi_a, hi_b)
            if hi > lo:
                seconds += (hi - lo).total_seconds()
    return seconds / 3600.0


def night_hours_by_definition(t_start, t_end, night_start_hour, night_end_hour):
    definition = active["definition"]
    if definition == "current":
        return original_night_overlap_hours(t_start, t_end, night_start_hour, night_end_hour)
    if t_end <= t_start:
        return 0.0
    start_utc = t_start.replace(tzinfo=EASTERN).astimezone(timezone.utc)
    end_utc = t_end.replace(tzinfo=EASTERN).astimezone(timezone.utc)
    at_origin = night_intervals_utc(start_utc, end_utc, active["org_zone"], night_start_hour, night_end_hour)
    if definition == "origin":
        return total_hours(at_origin)
    at_dest = night_intervals_utc(start_utc, end_utc, active["dest_zone"], night_start_hour, night_end_hour)
    both = overlap_hours(at_origin, at_dest)
    either = total_hours(at_origin) + total_hours(at_dest) - both
    if definition == "and":
        return both
    if definition == "or":
        return either
    if definition == "half":
        return (both + either) / 2.0
    raise ValueError(f"unknown night definition {definition!r}")


def observations_for_active_route(conn):
    rows, dropped = original_load_observations(conn)
    return [r for r in rows if (r["org"], r["dest"]) == (active["org"], active["dest"])], dropped


def resolve_leave_one_out_night_ratio(conn, org, dest, day_of_week, dep_time, cabin,
                                      pooled_night, global_defaults):
    min_instances = _load_threshold(conn, org, dest, day_of_week, dep_time, cabin)["nightRatio"]
    if pooled_night is not None and pooled_night[1] >= min_instances:
        return pooled_night[0]
    for override in (_load_service_override(conn, org, dest, day_of_week, dep_time, cabin),
                     _load_route_override(conn, org, dest, cabin)):
        if override and override["nightRatio"] is not None:
            return override["nightRatio"]
    return global_defaults.get(cabin, {}).get("nightSlopeRatio") or 1.0


def t1_prediction_from(conn, org, dest, flight_date, dep_time, raw_readings, cabin,
                       coefficients, horizon_hours):
    bracket = bracket_with_weight(raw_readings, horizon_hours)
    if bracket is None:
        return None
    cabin_key = CABIN_COLUMN_TO_KEY[cabin]

    def replay(reading):
        row = dict.fromkeys(T1Estimator.CABIN_KEY_TO_COLUMN, None)
        row["hrs"], row[cabin_key] = reading
        with coefficients_supplied_to_live_estimator(coefficients):
            return T1Estimator.compute_t1_replay_column(conn, org, dest, flight_date, dep_time, [row])[0]

    if bracket[0] == "single":
        return replay(bracket[1])
    _, before, after, weight = bracket
    before_prediction, after_prediction = replay(before), replay(after)
    if before_prediction is None or after_prediction is None:
        return None
    return before_prediction + (after_prediction - before_prediction) * weight


def residuals_for_route(conn, settings, global_defaults):
    night_start_hour, night_end_hour = settings["nightStartHour"], settings["nightEndHour"]
    result = compute_all_fits(conn, settings["stepChangeRmseThreshold"], settings["stepChangeMaxIterations"],
                              night_start_hour=night_start_hour, night_end_hour=night_end_hour)
    service_info = result["service_info"]
    residuals = {}

    for cabin in CABINS:
        cabin_data = result["by_cabin"][cabin]
        fits_by_instance = cabin_data["fits_by_instance"]
        instances = cabin_data["instances"]
        keys_by_service = group_keys_by_service(fits_by_instance)

        for key, fit in fits_by_instance.items():
            if fit is None:
                continue
            sid, flight_date = key
            raw_readings = instances[key]["readings"]
            truth = nearest_reading(raw_readings, T1_TARGET_HOURS_FOR_POOLING)
            if truth is None or truth[0] > GOOD_OBSERVATION_CUTOFF_HOURS:
                continue

            others = {k: fits_by_instance[k] for k in keys_by_service[sid] if k != key}
            slope_entry = pool_slope(others, night_start_hour, night_end_hour).get(sid)
            if slope_entry is None:
                continue
            loo_slope, _gap, loo_slope_n = slope_entry
            org, dest, day_of_week, dep_time, _ = service_info[sid]
            night_ratio = resolve_leave_one_out_night_ratio(
                conn, org, dest, day_of_week, dep_time, cabin,
                pool_night_ratio(others, night_start_hour, night_end_hour).get(sid), global_defaults,
            )
            coefficients = resolve_leave_one_out_coefficients(
                conn, org, dest, day_of_week, dep_time, cabin,
                pool_c1(others).get(sid), (loo_slope, loo_slope_n), night_ratio, global_defaults,
            )
            usable = [r for r in raw_readings if r[0] > truth[0]]
            for horizon in HORIZONS:
                if not any(r[0] >= horizon for r in usable):
                    continue
                prediction = t1_prediction_from(conn, org, dest, flight_date, dep_time,
                                                usable, cabin, coefficients, horizon)
                if prediction is not None:
                    residuals[(org, dest, day_of_week, dep_time, flight_date, cabin, horizon)] = \
                        truth[1] - prediction
    return residuals


MEANINGFUL_DIFFERENCE_SEATS = 0.05


def summarize(definition, residuals, keys):
    arr = np.array([residuals[definition][k] for k in keys], dtype=float)
    line = f"{definition:>8}: mean {np.mean(arr):+.2f}  RMSE {np.sqrt(np.mean(arr ** 2)):.2f}"
    if definition == "current":
        return line
    better = worse = 0
    for k in keys:
        change = abs(residuals[definition][k]) - abs(residuals["current"][k])
        if change < -MEANINGFUL_DIFFERENCE_SEATS:
            better += 1
        elif change > MEANINGFUL_DIFFERENCE_SEATS:
            worse += 1
    return line + f"   vs current: better on {better}, worse on {worse}"


def main():
    conn = sqlite3.connect(DB_PATH)
    settings = load_settings(conn, key=DECLINE_CURVE_SETTINGS_KEY, defaults=DEFAULT_DECLINE_CURVE_SETTINGS)
    global_defaults = load_settings(conn, key=DECLINE_CURVE_GLOBAL_DEFAULTS_KEY,
                                    defaults=DEFAULT_DECLINE_CURVE_GLOBAL_DEFAULTS)
    routes = sorted({(r["org"], r["dest"]) for r in original_load_observations(conn)[0]})

    DeclineCurveFit._night_overlap_hours = night_hours_by_definition
    DeclineCurveFit.load_observations = observations_for_active_route
    residuals_by_definition = {d: {} for d in DEFINITIONS}
    try:
        for org, dest in routes:
            active.update(org=org, dest=dest,
                          org_zone=ZoneInfo(get_confirmed_timezone(conn, org)),
                          dest_zone=ZoneInfo(get_confirmed_timezone(conn, dest)))
            for definition in DEFINITIONS:
                active["definition"] = definition
                residuals_by_definition[definition].update(residuals_for_route(conn, settings, global_defaults))
    finally:
        DeclineCurveFit._night_overlap_hours = original_night_overlap_hours
        DeclineCurveFit.load_observations = original_load_observations

    common = set.intersection(*(set(r) for r in residuals_by_definition.values()))
    eastern_codes = {code.lower() for code, in conn.execute(
        "SELECT code FROM confirmedAirports WHERE tz = 'America/New_York'")}

    def report(title, keys):
        print(f"{title}: {len(keys)} cases")
        for definition in DEFINITIONS:
            print("  " + summarize(definition, residuals_by_definition, keys))

    print(f"Night = {settings['nightStartHour']:02d}:00-{settings['nightEndHour']:02d}:00 local. "
          f"Residual = actual T-1 minus predicted, seats. Leave-one-out C1, slope and night ratio.")
    for horizon in HORIZONS:
        at_horizon = [k for k in common if k[6] == horizon]
        off_eastern = [k for k in at_horizon if not {k[0], k[1]} <= eastern_codes]
        print()
        print(f"===== From T-{horizon:g} =====")
        for cabin in CABINS:
            report(f"{cabin}, all routes", [k for k in at_horizon if k[5] == cabin])
            report(f"{cabin}, routes touching a non-Eastern zone", [k for k in off_eastern if k[5] == cabin])


if __name__ == "__main__":
    main()
