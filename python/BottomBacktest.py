"""
BottomBacktest.py

One-off analysis: does adding a bottom time - hours before departure at
which a cabin's seat count hits bottom and stops dropping - improve the
decline-curve estimator's T-4 -> T-1 prediction? Report only; nothing is written to the
database.

Predictors, all scored leave-one-out on identical instances:
  holdT4         no model: T-1 is predicted to equal the reading at T-4
  current        the decline-curve estimator: slope from pool_slope, no bottom
  fittedBottom   slope = median of the service's per-instance fitted slopes,
                 plus a bottom fitted on the service's other days
  service >=X    default is holdT4 ("already bottomed by T-4"); a fitted
                 bottom is used instead only when, on the service's other
                 days, it beats holdT4 by at least X seats of mean absolute
                 error
  dayGroup >=X   the same, but judged on the other days of every matching
                 service across the day group (Mon+Fri, Sat+Sun, Tue-Thu)

A bottom is applied inside the curve estimator by evaluating the curve at
max(hoursBeforeDep, bottom) everywhere: the curve declines normally, then
holds flat from the bottom until departure. BOTTOM_DEFAULT_HOURS stands for
"still dropping at departure". Slope, C1 and night ratio always come from
the instance's own day-of-week service; only the bottom decision is pooled.

Services on different days match within a day group when they share a route
and their representative departure times fall in one clustering.py cluster.

C1, night ratio and the coefficient tier walk are exactly T4T1Backtest's.
Unlike T4T1Backtest, the T-4 value is the single raw reading nearest T-4,
used only within T4_TOLERANCE_HOURS of it, and truth is the raw reading
nearest T-1 - no bracket interpolation, whose far side is often the T-1
reading itself, and no step-corrected values.
"""
import os
import sqlite3
import sys
from collections import defaultdict
from contextlib import contextmanager

import numpy as np

import T1Estimator
from clustering import cluster_services
from DeclineCurveFit import (
    DB_PATH, compute_all_fits, pool_slope, pool_c1, nearest_reading,
    piecewise_model, solve_c1_from_reading,
    T4_TARGET_HOURS_FOR_POOLING, T1_TARGET_HOURS_FOR_POOLING,
)
from T4T1Backtest import (
    GOOD_OBSERVATION_CUTOFF_HOURS, CABIN_COLUMN_TO_KEY, group_keys_by_service,
    resolve_leave_one_out_coefficients, coefficients_supplied_to_live_estimator, mean_and_rmse,
)
from settings import (
    load_settings, DECLINE_CURVE_SETTINGS_KEY, DEFAULT_DECLINE_CURVE_SETTINGS,
    DECLINE_CURVE_GLOBAL_DEFAULTS_KEY, DEFAULT_DECLINE_CURVE_GLOBAL_DEFAULTS,
)

T4_TOLERANCE_HOURS = 1.0
BOTTOM_DEFAULT_HOURS = 0.75
BOTTOM_CANDIDATES = [BOTTOM_DEFAULT_HOURS] + [1.0 + 0.25 * i for i in range(1, 29)]
MARGINS = [0.5, 1.0]
DAY_GROUPS = {"Mon": "Mon+Fri", "Fri": "Mon+Fri", "Sat": "Sat+Sun", "Sun": "Sat+Sun",
              "Tue": "Tue-Thu", "Wed": "Tue-Thu", "Thu": "Tue-Thu"}
CABINS = ["y", "cPlus", "firstOrPS"]


def rule_names():
    return [f"{scope} >={margin}" for scope in ("service", "dayGroup") for margin in MARGINS]


PREDICTORS = ["holdT4", "current", "fittedBottom"] + rule_names()


@contextmanager
def bottomed_at(bottom_hours):
    if bottom_hours is None:
        yield
        return

    def bottomed_model(hbd, *args, **kwargs):
        return piecewise_model(np.maximum(hbd, bottom_hours), *args, **kwargs)

    def bottomed_solve_c1(hbd_r, *args, **kwargs):
        return solve_c1_from_reading(max(hbd_r, bottom_hours), *args, **kwargs)

    T1Estimator.piecewise_model = bottomed_model
    T1Estimator.solve_c1_from_reading = bottomed_solve_c1
    try:
        yield
    finally:
        T1Estimator.piecewise_model = piecewise_model
        T1Estimator.solve_c1_from_reading = solve_c1_from_reading


def solid_truth(readings):
    truth = nearest_reading(readings, T1_TARGET_HOURS_FOR_POOLING)
    if truth is None or truth[0] > GOOD_OBSERVATION_CUTOFF_HOURS:
        return None
    return truth[1]


def t4_reading(readings):
    reading = nearest_reading(readings, T4_TARGET_HOURS_FOR_POOLING)
    if reading is None or abs(reading[0] - T4_TARGET_HOURS_FOR_POOLING) > T4_TOLERANCE_HOURS:
        return None
    return reading


def median_slope(pool_fits):
    slopes = [fit["slope"] for fit in pool_fits.values() if fit["slope"] is not None]
    if not slopes:
        return None
    return float(np.median(slopes)), len(slopes)


def build_day_groups(service_info):
    """serviceId -> day-group id, matching services of one route across the
    days of a day group by clustering their representative departure times."""
    by_route_group = defaultdict(list)
    for sid, (org, dest, dow, rep_time, _) in service_info.items():
        by_route_group[(org, dest, DAY_GROUPS[dow])].append((rep_time, sid))
    group_of = {}
    for route_group, members in by_route_group.items():
        for index, cluster in enumerate(cluster_services(rep for rep, _ in members)):
            for rep, sid in members:
                if rep in cluster:
                    group_of[sid] = (route_group, index)
    return group_of


class Scorer:
    def __init__(self, conn, cabin, fits_by_instance, instances, service_info, global_defaults,
                 night_start_hour, night_end_hour):
        self.conn = conn
        self.cabin = cabin
        self.fits = fits_by_instance
        self.instances = instances
        self.service_info = service_info
        self.global_defaults = global_defaults
        self.night_start_hour = night_start_hour
        self.night_end_hour = night_end_hour
        self.keys_by_service = group_keys_by_service(fits_by_instance)

    def raw_readings(self, key):
        return self.instances[key]["readings"]

    def truth(self, key):
        return solid_truth(self.raw_readings(key))

    def hold_error(self, key):
        return self.truth(key) - t4_reading(self.raw_readings(key))[1]

    def scorable(self, key):
        return (self.fits.get(key) is not None and self.truth(key) is not None
                and t4_reading(self.raw_readings(key)) is not None)

    def service_pool(self, key, excluded):
        return {k: self.fits[k] for k in self.keys_by_service[key[0]] if k != key and k not in excluded}

    def coefficients(self, key, pool_fits, slope_entry):
        sid, _ = key
        org, dest, day_of_week, dep_time, _ = self.service_info[sid]
        loo_c1 = pool_c1(pool_fits).get(sid)
        night_ratio = self.fits[key].get("nightRatio", 1.0) or 1.0
        return resolve_leave_one_out_coefficients(
            self.conn, org, dest, day_of_week, dep_time, self.cabin,
            loo_c1, slope_entry, night_ratio, self.global_defaults,
        )

    def curve_coefficients(self, key, excluded):
        pool_fits = self.service_pool(key, excluded)
        slope_entry = median_slope(pool_fits)
        if slope_entry is None:
            return None
        return self.coefficients(key, pool_fits, slope_entry) or None

    def predict(self, key, coefficients, bottom_hours):
        sid, flight_date = key
        org, dest, _, dep_time, _ = self.service_info[sid]
        row = dict.fromkeys(T1Estimator.CABIN_KEY_TO_COLUMN, None)
        row["hrs"], row[CABIN_COLUMN_TO_KEY[self.cabin]] = t4_reading(self.raw_readings(key))
        with bottomed_at(bottom_hours), coefficients_supplied_to_live_estimator(coefficients):
            return T1Estimator.curve_t1_replay_column(self.conn, org, dest, flight_date, dep_time, [row])[0]

    def training_cases(self, training_keys, held_out):
        """[(key, coefficients, truth, holdT4 error)] for every training day
        that can be scored, each with coefficients from its own service
        excluding both itself and the held-out day."""
        cases = []
        for key in training_keys:
            if not self.scorable(key):
                continue
            coefficients = self.curve_coefficients(key, {held_out})
            if coefficients is None:
                continue
            cases.append((key, coefficients, self.truth(key), self.hold_error(key)))
        return cases

    def best_bottom(self, cases):
        """(bottom, mean absolute error) of the SSE-best candidate on cases,
        or (BOTTOM_DEFAULT_HOURS, None) when nothing could be scored."""
        if not cases:
            return BOTTOM_DEFAULT_HOURS, None
        best = None
        for bottom_hours in BOTTOM_CANDIDATES:
            errors = []
            for key, coefficients, truth, _ in cases:
                pred = self.predict(key, coefficients, bottom_hours)
                if pred is None:
                    break
                errors.append(truth - pred)
            else:
                sse = float(np.sum(np.square(errors)))
                if best is None or sse < best[0] - 1e-9:
                    best = (sse, bottom_hours, float(np.mean(np.abs(errors))))
        return (BOTTOM_DEFAULT_HOURS, None) if best is None else (best[1], best[2])


def backtest_cabin(scorer, group_of):
    residuals = {name: {} for name in PREDICTORS}
    curve_chosen = defaultdict(int)
    keys_by_group = defaultdict(list)
    for key in scorer.fits:
        if scorer.fits[key] is not None:
            keys_by_group[group_of[key[0]]].append(key)

    for key in scorer.fits:
        if not scorer.scorable(key):
            continue
        sid, _ = key
        truth = scorer.truth(key)
        hold_error = scorer.hold_error(key)

        own_pool = scorer.service_pool(key, set())
        current_entry = pool_slope(own_pool, scorer.night_start_hour, scorer.night_end_hour).get(sid)
        coefficients = scorer.curve_coefficients(key, set())
        if current_entry is None or coefficients is None:
            continue
        slope, _gap, n = current_entry
        current_coefficients = scorer.coefficients(key, own_pool, (slope, n))
        current_pred = scorer.predict(key, current_coefficients, None) if current_coefficients else None
        if current_pred is None:
            continue

        residuals["holdT4"][key] = hold_error
        residuals["current"][key] = truth - current_pred

        scopes = {
            "service": [k for k in scorer.keys_by_service[sid] if k != key],
            "dayGroup": [k for k in keys_by_group[group_of[sid]] if k != key],
        }
        for scope, training_keys in scopes.items():
            cases = scorer.training_cases(training_keys, key)
            bottom_hours, bottom_mae = scorer.best_bottom(cases)
            curve_error = None
            if bottom_mae is not None:
                pred = scorer.predict(key, coefficients, bottom_hours)
                curve_error = None if pred is None else truth - pred
            if scope == "service":
                residuals["fittedBottom"][key] = hold_error if curve_error is None else curve_error
            hold_mae = float(np.mean([abs(case[3]) for case in cases])) if cases else None
            for margin in MARGINS:
                name = f"{scope} >={margin}"
                use_curve = (curve_error is not None and hold_mae - bottom_mae >= margin)
                residuals[name][key] = curve_error if use_curve else hold_error
                curve_chosen[name] += use_curve
    return residuals, curve_chosen


def report_cabin(scorer, group_of):
    residuals, curve_chosen = backtest_cabin(scorer, group_of)
    keys = list(residuals["holdT4"])
    print(f"=== Cabin: {scorer.cabin} ===")
    if not keys:
        print("  No instance scored.\n")
        return
    print(f"  {len(keys)} instances across {len({sid for sid, _ in keys})} services:")
    for name in PREDICTORS:
        errors = [residuals[name][k] for k in keys]
        mean_r, rmse_r = mean_and_rmse(errors)
        within_one = sum(1 for r in errors if abs(r) <= 1.0) / len(errors)
        chosen = f"  curve used on {curve_chosen[name]}" if name in curve_chosen else ""
        print(f"  {name:<15} mean={mean_r:+.2f}  RMSE={rmse_r:.2f}  within 1 seat={within_one:.0%}{chosen}")
    print()


def main():
    if not os.path.exists(DB_PATH):
        sys.exit(f"Database not found at {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    settings = load_settings(conn, key=DECLINE_CURVE_SETTINGS_KEY, defaults=DEFAULT_DECLINE_CURVE_SETTINGS)
    global_defaults = load_settings(
        conn, key=DECLINE_CURVE_GLOBAL_DEFAULTS_KEY, defaults=DEFAULT_DECLINE_CURVE_GLOBAL_DEFAULTS,
    )
    result = compute_all_fits(
        conn, settings["stepChangeRmseThreshold"], settings["stepChangeMaxIterations"],
        night_start_hour=settings["nightStartHour"], night_end_hour=settings["nightEndHour"],
    )
    group_of = build_day_groups(result["service_info"])
    print(f"Leave-one-out T-4 -> T-1 bottom backtest (ground truth: a reading within "
          f"{GOOD_OBSERVATION_CUTOFF_HOURS}h of departure; error = actual - predicted).\n")
    for cabin in CABINS:
        cabin_data = result["by_cabin"][cabin]
        scorer = Scorer(conn, cabin, cabin_data["fits_by_instance"], cabin_data["instances"],
                        result["service_info"], global_defaults,
                        settings["nightStartHour"], settings["nightEndHour"])
        report_cabin(scorer, group_of)


if __name__ == "__main__":
    main()
