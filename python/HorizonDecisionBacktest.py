"""
HorizonDecisionBacktest.py

One-off analysis: from how far out does holding the latest reading call a
flight's T-1 box (full / between / open) better than the decline curve? Report
only; nothing is written to the database.

Scored per service-day, not averaged: each day's T-1 truth goes in a box and
each predictor's call from the horizon goes in a box. The report counts right
and wrong calls, lists every day either predictor called open that turned
out full, and breaks the T-4 calls down by service.

A day is all three cabins summed (y + cPlus + firstOrPS) and counts only when
every cabin has a T-1 truth and a reading near the horizon. Boxes use the
open/full settings' fullThreshold and openThreshold.

  hold    each cabin's reading at the horizon, carried to T-1 unchanged
  curve   T1Estimator.curve_t1_replay_column from the same readings, with leave-one-out
          coefficients built as T4T1Backtest builds them; a cabin with no
          leave-one-out slope falls through the tier walk to its override or
          global default, as the curve estimator would

Anchors and truth are the raw logged readings - what an estimator sees on the day
and what he actually saw - not the step-corrected ones T4T1Backtest scores.
See anchor_readings for how the horizon value is taken.
"""
import os
import sqlite3
import sys
from collections import Counter, defaultdict

import T1Estimator
from DeclineCurveFit import DB_PATH, compute_all_fits, pool_slope, pool_c1, nearest_reading
from ServiceGrouping import load_open_full_settings
from T4T1Backtest import (
    GOOD_OBSERVATION_CUTOFF_HOURS, CABIN_COLUMN_TO_KEY, group_keys_by_service,
    resolve_leave_one_out_coefficients, coefficients_supplied_to_live_estimator,
)
from settings import (
    load_settings, DECLINE_CURVE_SETTINGS_KEY, DEFAULT_DECLINE_CURVE_SETTINGS,
    DECLINE_CURVE_GLOBAL_DEFAULTS_KEY, DEFAULT_DECLINE_CURVE_GLOBAL_DEFAULTS,
)

HORIZONS = [4.0, 6.0, 8.0, 12.0, 24.0]
CABINS = ["y", "cPlus", "firstOrPS"]
BOXES = ["full", "between", "open"]


def horizon_tolerance(horizon):
    return max(1.0, 0.25 * horizon)


def anchor_readings(readings, horizon, tolerance):
    """[(reading, weight)] whose weighted sum is the value at the horizon, or
    None. Readings inside the T-1 truth window never anchor, so the answer
    can't leak into the prediction. Two readings straddling the horizon,
    each within twice the tolerance of it, are interpolated between;
    otherwise the single reading nearest the horizon is used when within
    the tolerance."""
    usable = [r for r in readings if r[0] > GOOD_OBSERVATION_CUTOFF_HOURS]
    farther = [r for r in usable if horizon <= r[0] <= horizon + 2 * tolerance]
    nearer = [r for r in usable if horizon - 2 * tolerance <= r[0] <= horizon]
    if farther and nearer:
        far = min(farther, key=lambda r: r[0])
        near = max(nearer, key=lambda r: r[0])
        if far[0] == near[0]:
            return [(far, 1.0)]
        weight_near = (far[0] - horizon) / (far[0] - near[0])
        return [(far, 1.0 - weight_near), (near, weight_near)]
    reading = nearest_reading(usable, horizon)
    if reading is None or abs(reading[0] - horizon) > tolerance:
        return None
    return [(reading, 1.0)]


def box(seats, thresholds):
    if seats <= thresholds["fullThreshold"]:
        return "full"
    if seats >= thresholds["openThreshold"]:
        return "open"
    return "between"


class CabinModel:
    def __init__(self, conn, cabin, cabin_data, service_info, settings, global_defaults):
        self.conn = conn
        self.cabin = cabin
        self.fits = cabin_data["fits_by_instance"]
        self.instances = cabin_data["instances"]
        self.service_info = service_info
        self.settings = settings
        self.global_defaults = global_defaults
        self.keys_by_service = group_keys_by_service(self.fits)

    def raw_readings(self, key):
        instance = self.instances.get(key)
        return instance["readings"] if instance else []

    def truth(self, key):
        reading = nearest_reading(self.raw_readings(key), 1.0)
        if reading is None or reading[0] > GOOD_OBSERVATION_CUTOFF_HOURS:
            return None
        return reading[1]

    def leave_one_out_coefficients(self, key):
        sid, _ = key
        fit = self.fits.get(key) or {}
        pool_fits = {k: self.fits[k] for k in self.keys_by_service[sid] if k != key}
        slope_entry = pool_slope(pool_fits, self.settings["nightStartHour"],
                                 self.settings["nightEndHour"]).get(sid)
        loo_slope = None if slope_entry is None else (slope_entry[0], slope_entry[2])
        org, dest, day_of_week, dep_time, _ = self.service_info[sid]
        return resolve_leave_one_out_coefficients(
            self.conn, org, dest, day_of_week, dep_time, self.cabin,
            pool_c1(pool_fits).get(sid), loo_slope, fit.get("nightRatio", 1.0) or 1.0,
            self.global_defaults,
        ) or None

    def predictions(self, key, horizon, coefficients):
        """(hold, curve) seats at T-1 for this cabin from the horizon, or None."""
        anchors = anchor_readings(self.raw_readings(key), horizon, horizon_tolerance(horizon))
        if anchors is None:
            return None
        sid, flight_date = key
        org, dest, _, dep_time, _ = self.service_info[sid]
        cabin_key = CABIN_COLUMN_TO_KEY[self.cabin]

        def replay(reading):
            row = dict.fromkeys(T1Estimator.CABIN_KEY_TO_COLUMN, None)
            row["hrs"], row[cabin_key] = reading
            with coefficients_supplied_to_live_estimator(coefficients):
                return T1Estimator.curve_t1_replay_column(self.conn, org, dest, flight_date, dep_time, [row])[0]

        curves = [replay(reading) for reading, _ in anchors]
        if None in curves:
            return None
        hold = sum(reading[1] * weight for reading, weight in anchors)
        curve = sum(value * weight for value, (_, weight) in zip(curves, anchors))
        return hold, curve


def score_days(models, thresholds):
    """{horizon: [(key, truth_box, hold_box, curve_box, truth, hold, curve)]}"""
    day_keys = set.intersection(*(set(model.instances) for model in models))
    coefficients = {}
    for key in day_keys:
        per_cabin = [model.leave_one_out_coefficients(key) for model in models]
        truths = [model.truth(key) for model in models]
        if None not in per_cabin and None not in truths:
            coefficients[key] = (per_cabin, sum(truths))

    days = defaultdict(list)
    for horizon in HORIZONS:
        for key, (per_cabin, truth) in coefficients.items():
            predictions = [model.predictions(key, horizon, c) for model, c in zip(models, per_cabin)]
            if None in predictions:
                continue
            hold = sum(p[0] for p in predictions)
            curve = sum(p[1] for p in predictions)
            days[horizon].append((key, box(truth, thresholds), box(hold, thresholds), box(curve, thresholds),
                                  truth, hold, curve))
    return days


def service_label(service_info, key):
    sid, flight_date = key
    org, dest, dow, rep_time, _ = service_info[sid]
    hh, mm = divmod(rep_time, 60)
    return f"{org}-{dest} {dow} ~{hh:02d}{mm:02d}", flight_date


def print_summary(horizon, scored):
    print(f"=== From T-{horizon:g} ({len(scored)} days, straddle within "
          f"{2 * horizon_tolerance(horizon):g}h each side, else a reading within "
          f"{horizon_tolerance(horizon):g}h) ===")
    outcome = Counter((d[2] == d[1], d[3] == d[1]) for d in scored)
    print(f"  both right {outcome[(True, True)]}, only hold right {outcome[(True, False)]}, "
          f"only curve right {outcome[(False, True)]}, both wrong {outcome[(False, False)]}")
    for name, index in (("hold", 2), ("curve", 3)):
        called_open_was_full = sum(1 for d in scored if d[index] == "open" and d[1] == "full")
        called_full_was_open = sum(1 for d in scored if d[index] == "full" and d[1] == "open")
        print(f"  {name:<5} called open, was full: {called_open_was_full:>3}   "
              f"called full, was open: {called_full_was_open:>3}")


def print_open_but_full_days(scored, service_info):
    misses = [d for d in scored if d[1] == "full" and "open" in (d[2], d[3])]
    if not misses:
        return
    print("  days called open that were full:")
    for key, _, hold_box, curve_box, truth, hold, curve in sorted(
            misses, key=lambda d: service_label(service_info, d[0])):
        label, flight_date = service_label(service_info, key)
        print(f"    {label} {flight_date}: T-1 {truth:.0f}; hold {hold:.1f} ({hold_box}), "
              f"curve {curve:.1f} ({curve_box})")


def print_service_table(scored, service_info):
    by_service = defaultdict(list)
    for d in scored:
        by_service[service_label(service_info, d[0])[0]].append(d)
    print("  per service (days, hold right, curve right) - services with any wrong call:")
    for label in sorted(by_service):
        days = by_service[label]
        hold_right = sum(1 for d in days if d[2] == d[1])
        curve_right = sum(1 for d in days if d[3] == d[1])
        if hold_right == curve_right == len(days):
            continue
        print(f"    {label}: {len(days)} days, hold {hold_right}, curve {curve_right}")


def report(days, service_info):
    for horizon in HORIZONS:
        scored = days[horizon]
        print_summary(horizon, scored)
        print_open_but_full_days(scored, service_info)
        if horizon == HORIZONS[0]:
            print_service_table(scored, service_info)
        print()


def main():
    if not os.path.exists(DB_PATH):
        sys.exit(f"Database not found at {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    settings = load_settings(conn, key=DECLINE_CURVE_SETTINGS_KEY, defaults=DEFAULT_DECLINE_CURVE_SETTINGS)
    global_defaults = load_settings(
        conn, key=DECLINE_CURVE_GLOBAL_DEFAULTS_KEY, defaults=DEFAULT_DECLINE_CURVE_GLOBAL_DEFAULTS,
    )
    thresholds = load_open_full_settings(conn)
    result = compute_all_fits(
        conn, settings["stepChangeRmseThreshold"], settings["stepChangeMaxIterations"],
        night_start_hour=settings["nightStartHour"], night_end_hour=settings["nightEndHour"],
    )
    models = [CabinModel(conn, cabin, result["by_cabin"][cabin], result["service_info"], settings, global_defaults)
              for cabin in CABINS]
    print(f"Hold vs decline curve, T-1 box per service-day (all cabins summed; full <= "
          f"{thresholds['fullThreshold']}, open >= {thresholds['openThreshold']}; truth is a raw reading "
          f"within {GOOD_OBSERVATION_CUTOFF_HOURS}h of departure).\n")
    report(score_days(models, thresholds), result["service_info"])


if __name__ == "__main__":
    main()
