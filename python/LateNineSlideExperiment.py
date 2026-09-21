"""
LateNineSlideExperiment.py

Offline experiment, not part of the live estimator: what if a "late 9"
(a 9 the service's pooled curve says should already have started
declining, by more than stepChangeRmseThreshold seats) no longer slid C1
to the reading's own time?

Today T1Estimator slides C1 to that reading - the decline is assumed to
start right then. In the data a late 9 usually holds far longer than
that. The rule here instead treats the hours from a late 9 until the
decline actually starts as exponentially distributed with one hazard
rate per cabin, and predicts T-1 as the expected value of the pooled
curve over that distribution, with the leftover probability mass (the
decline starts after departure) held at 9.

The hazard is estimated the way right-censored waiting times are
(maximum likelihood for an exponential): decline starts observed divided
by hours of exposure. Each instance with a late 9 contributes one
event, starting at its earliest late-9 reading. Exposure runs to the
instance's fitted C1 if it later dropped below 9, or to its last
reading if it never did (censored - all we know is the corner is at
least that late). Exposure is raw clock hours, so the slower overnight
decline is not credited. The constant hazard is an assumption, not a
finding.

Every other reading - interior values, expected 9s, zeros - still goes
through the live estimator unchanged, via the corrected
T4T1Backtest. Coefficients are leave-one-out as there, and the hazard
for a held-out instance excludes that instance's own event. One second-
order leak remains: whether OTHER instances count as late is judged with
coefficients that include the held-out instance.

Scores both predictors on identical instances, overall, on flights with
a T-4 reading of 9+, and within each saved scenario. Nothing is written
back to the database.
"""
import os
import sqlite3
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import T1Estimator
import T4T1Backtest as backtest
from DeclineCurveFit import (
    DB_PATH, compute_all_fits, pool_slope, pool_c1, bracket_with_weight, nearest_reading,
    piecewise_model, T4_TARGET_HOURS_FOR_POOLING, T1_TARGET_HOURS_FOR_POOLING,
)
from settings import (
    load_settings, DECLINE_CURVE_SETTINGS_KEY, DEFAULT_DECLINE_CURVE_SETTINGS,
    DECLINE_CURVE_GLOBAL_DEFAULTS_KEY, DEFAULT_DECLINE_CURVE_GLOBAL_DEFAULTS,
)

QUADRATURE_POINTS = 200
MIN_DROP_DURATION_HOURS = 0.01
OPEN_LINE = 8


def is_late_nine(hbd, val, coefficients, departure_dt, night_start_hour, night_end_hour, threshold):
    if val < 9:
        return False
    c1, slope, night_ratio = coefficients
    if c1 is None:
        return True
    pooled_value = float(piecewise_model(hbd, c1, slope, night_ratio, departure_dt,
                                         night_start_hour, night_end_hour))
    return abs(9 - pooled_value) > threshold


def late_nine_event(fit, coefficients, night_start_hour, night_end_hour, threshold):
    readings = fit["corrected_readings"]
    departure_dt = fit.get("departureDt")
    for hbd, val in readings:
        if is_late_nine(hbd, val, coefficients, departure_dt, night_start_hour, night_end_hour, threshold):
            last_hbd, last_val = readings[-1]
            if last_val >= 9:
                return {"duration": max(hbd - last_hbd, 0.0), "dropped": False}
            return {"duration": max(hbd - fit["c1"], MIN_DROP_DURATION_HOURS), "dropped": True}
    return None


def hazard_rate(events, excluding_key):
    drops = 0
    exposure = 0.0
    for key, event in events.items():
        if key != excluding_key:
            drops += event["dropped"]
            exposure += event["duration"]
    return drops / exposure if exposure > 0 else 0.0


def expected_t1_after_late_nine(hbd, rate, coefficients, departure_dt, night_start_hour, night_end_hour):
    c1, slope, night_ratio = coefficients
    if rate <= 0 or hbd <= 0:
        return 9.0
    step = hbd / QUADRATURE_POINTS
    total = np.exp(-rate * hbd) * 9.0
    for i in range(QUADRATURE_POINTS):
        wait = (i + 0.5) * step
        value = float(piecewise_model(T1_TARGET_HOURS_FOR_POOLING, hbd - wait, slope, night_ratio,
                                      departure_dt, night_start_hour, night_end_hour))
        total += rate * np.exp(-rate * wait) * step * value
    return total


def experimental_prediction(conn, org, dest, flight_date, dep_time, readings, cabin, coefficients,
                             departure_dt, rate, night_start_hour, night_end_hour, threshold):
    bracket = bracket_with_weight(readings, T4_TARGET_HOURS_FOR_POOLING)
    if bracket is None:
        return None
    cabin_key = backtest.CABIN_COLUMN_TO_KEY[cabin]

    def predict(reading):
        hbd, val = reading
        if is_late_nine(hbd, val, coefficients, departure_dt, night_start_hour, night_end_hour, threshold):
            return expected_t1_after_late_nine(hbd, rate, coefficients, departure_dt,
                                               night_start_hour, night_end_hour), True
        row = dict.fromkeys(T1Estimator.CABIN_KEY_TO_COLUMN, None)
        row["hrs"], row[cabin_key] = reading
        with backtest.coefficients_supplied_to_live_estimator({cabin: coefficients}):
            return T1Estimator.compute_t1_replay_column(conn, org, dest, flight_date, dep_time, [row])[0], False

    if bracket[0] == "single":
        return predict(bracket[1])

    _, before, after, weight = bracket
    before_prediction, before_late = predict(before)
    after_prediction, after_late = predict(after)
    if before_prediction is None or after_prediction is None:
        return None
    return before_prediction + (after_prediction - before_prediction) * weight, before_late or after_late


def leave_one_out_coefficients_by_instance(conn, cabin, fits, service_info, night_start_hour,
                                            night_end_hour, global_defaults):
    by_service = backtest.group_keys_by_service(fits)
    resolved = {}
    for key, fit in fits.items():
        if fit is None:
            continue
        sid = key[0]
        others = {k: fits[k] for k in by_service[sid] if k != key}
        slope_entry = pool_slope(others, night_start_hour, night_end_hour).get(sid)
        if slope_entry is None:
            continue
        org, dest, day_of_week, dep_time, _ = service_info[sid]
        coefficients = backtest.resolve_leave_one_out_coefficients(
            conn, org, dest, day_of_week, dep_time, cabin, pool_c1(others).get(sid),
            (slope_entry[0], slope_entry[2]), fit.get("nightRatio", 1.0) or 1.0, global_defaults,
        )
        if coefficients:
            resolved[key] = coefficients[cabin]
    return resolved


def scenario_service_ids(conn, service_info):
    scenarios = {}
    for scenario_id, name in conn.execute("SELECT id, name FROM scenarios"):
        cells = {tuple(row) for row in conn.execute(
            "SELECT org, dest, dayOfWeek FROM scenarioCells WHERE scenarioId=?", (scenario_id,))}
        scenarios[name] = {sid for sid, info in service_info.items() if tuple(info[:3]) in cells}
    return scenarios


def summarize(label, truth, baseline, experiment):
    if not truth:
        return
    truth, baseline, experiment = (np.array(x, dtype=float) for x in (truth, baseline, experiment))
    print(f"  {label} (n={len(truth)}): live rule mean {np.mean(truth - baseline):+.2f} / RMSE "
          f"{np.sqrt(np.mean((truth - baseline) ** 2)):.2f};  new rule mean {np.mean(truth - experiment):+.2f} / RMSE "
          f"{np.sqrt(np.mean((truth - experiment) ** 2)):.2f}")


def main():
    conn = sqlite3.connect(DB_PATH)
    settings = load_settings(conn, key=DECLINE_CURVE_SETTINGS_KEY, defaults=DEFAULT_DECLINE_CURVE_SETTINGS)
    night_start_hour, night_end_hour = settings["nightStartHour"], settings["nightEndHour"]
    threshold = settings["stepChangeRmseThreshold"]
    global_defaults = load_settings(conn, key=DECLINE_CURVE_GLOBAL_DEFAULTS_KEY,
                                    defaults=DEFAULT_DECLINE_CURVE_GLOBAL_DEFAULTS)
    result = compute_all_fits(conn, threshold, settings["stepChangeMaxIterations"],
                              night_start_hour=night_start_hour, night_end_hour=night_end_hour)
    service_info = result["service_info"]
    scenarios = scenario_service_ids(conn, service_info)

    for cabin in ("y", "cPlus", "firstOrPS"):
        fits = result["by_cabin"][cabin]["fits_by_instance"]
        coefficients_by_key = leave_one_out_coefficients_by_instance(
            conn, cabin, fits, service_info, night_start_hour, night_end_hour, global_defaults)

        events = {}
        for key, coefficients in coefficients_by_key.items():
            event = late_nine_event(fits[key], coefficients, night_start_hour, night_end_hour, threshold)
            if event:
                events[key] = event
        drops = sum(e["dropped"] for e in events.values())
        exposure = sum(e["duration"] for e in events.values())
        print(f"=== Cabin: {cabin} ===")
        print(f"  late-9 events: {len(events)} ({drops} later dropped), exposure {exposure:.0f}h, "
              f"hazard {drops / exposure if exposure else 0:.3f}/h "
              f"(mean wait until decline starts {exposure / drops if drops else float('inf'):.1f}h)")

        rows = []
        for key, coefficients in coefficients_by_key.items():
            fit = fits[key]
            readings = fit["corrected_readings"]
            truth = nearest_reading(readings, T1_TARGET_HOURS_FOR_POOLING)
            if truth is None or truth[0] > backtest.GOOD_OBSERVATION_CUTOFF_HOURS:
                continue
            sid, flight_date = key
            org, dest, _dow, dep_time, _ = service_info[sid]
            baseline = backtest.live_t1_prediction(conn, org, dest, flight_date, dep_time, readings,
                                                    cabin, {cabin: coefficients})
            experiment = experimental_prediction(
                conn, org, dest, flight_date, dep_time, readings, cabin, coefficients,
                fit.get("departureDt"), hazard_rate(events, key), night_start_hour, night_end_hour, threshold)
            if baseline is None or experiment is None:
                continue
            t4 = nearest_reading(readings, T4_TARGET_HOURS_FOR_POOLING)
            rows.append({"sid": sid, "truth": truth[1], "baseline": baseline, "experiment": experiment[0],
                         "rule_applied": experiment[1], "t4_nine": t4 is not None and t4[1] >= 9})

        def pick(subset):
            return ([r["truth"] for r in subset], [r["baseline"] for r in subset], [r["experiment"] for r in subset])

        summarize("all backtestable", *pick(rows))
        summarize("late-9 rule applied", *pick([r for r in rows if r["rule_applied"]]))
        nines = [r for r in rows if r["t4_nine"]]
        summarize("T-4 reading 9+", *pick(nines))
        if nines:
            print(f"    of those, predicted below {OPEN_LINE}: live rule {sum(r['baseline'] < OPEN_LINE for r in nines)}, "
                  f"new rule {sum(r['experiment'] < OPEN_LINE for r in nines)}; "
                  f"actually still 9+ at T-1: {sum(r['truth'] >= 9 for r in nines)}")
        for name, sids in scenarios.items():
            summarize(f"scenario {name}", *pick([r for r in rows if r["sid"] in sids]))
        print()


if __name__ == "__main__":
    main()
