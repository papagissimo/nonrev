"""
T4T1Backtest.py

Answers one question: given only what's knowable at T-4h, how well does
the live T-1 estimator (T1Estimator.compute_t1_replay_column) predict what
T-1h will actually turn out to be? A one-off analysis script, not part of
the live estimator or the settings hierarchy - run it, read the report,
decide whether T-4 is worth building anything further around.

THE PREDICTOR UNDER TEST IS THE LIVE ONE. Each prediction is a call to
T1Estimator.compute_t1_replay_column itself, so the expected-vs-unexpected
rail check and the slide-C1-only-when-surprised rule are the ones the
logging dialog actually runs. Until 2026-09-21 this script used
DeclineCurveFit.predict_t1_via_slide instead, which always slid C1 to the
reading and never consulted the pooled C1 - a different predictor for any
9 or 0 reading. pool_slope's own training objective still uses
predict_t1_via_slide; only what this script scores changed.

Like the live tool's replay of a T-4 moment, the two readings bracketing
T-4 (DeclineCurveFit.bracket_with_weight) are each run through the
estimator and the two T-1 predictions are interpolated by the bracket's
distance weight - his call, since on a real day T-4 can't be hit exactly.

LEAVE-ONE-OUT, NOT IN-SAMPLE. This script holds out one instance at a
time and rebuilds the held-out instance's service coefficients from every
OTHER instance of that same (day-of-week-specific) service: slope via
pool_slope, C1 via pool_c1 (median of the others' own fitted C1s). Those
coefficients are then resolved through the same tier walk as
DeclineCurveHierarchy.resolve_coefficients (derived if its instance count
meets the minimum, else service override, route override, global default)
and handed to the live estimator in place of its usual database lookup.
No instance's own T-1 reading influences the coefficients used to predict
it. The night/day ratio is the one on the instance's own fit and is NOT
leave-one-out.

GROUND TRUTH GATE. An instance only counts as a backtest case if its
nearest-to-T-1 reading is a real observation within 1.55h of departure
(his stated golden-ticket "solid ground truth" cutoff - see
nonrev-sqlite-rewrite notes). A nearest reading further out than that is
an estimate, not ground truth, and comparing a prediction against it
would just be comparing two guesses. An instance whose service has no
other instance to pool a slope from is skipped, as before, rather than
scored against override or global-default coefficients.

Reuses: DeclineCurveFit.compute_all_fits (for the fitted, step-corrected
per-instance data), pool_slope, pool_c1, bracket_with_weight,
nearest_reading, and the T4_TARGET_HOURS_FOR_POOLING /
T1_TARGET_HOURS_FOR_POOLING constants already defined there. Training and
evaluation targets differing is normal: pool_slope trains each candidate
slope against every instance's own last reading, and this script
evaluates specifically at T-1 against its own stricter
GOOD_OBSERVATION_CUTOFF_HOURS gate.
Nothing here is written back to the database - report only, printed to
console.
"""
import sqlite3
from collections import defaultdict
from contextlib import contextmanager

import numpy as np

import T1Estimator
from DeclineCurveHierarchy import _load_threshold, _load_service_override, _load_route_override
from settings import (
    load_settings, DECLINE_CURVE_SETTINGS_KEY, DEFAULT_DECLINE_CURVE_SETTINGS,
    DECLINE_CURVE_GLOBAL_DEFAULTS_KEY, DEFAULT_DECLINE_CURVE_GLOBAL_DEFAULTS,
)
from DeclineCurveFit import (
    DB_PATH, compute_all_fits, pool_slope, pool_c1, bracket_with_weight, nearest_reading,
    T4_TARGET_HOURS_FOR_POOLING, T1_TARGET_HOURS_FOR_POOLING,
)

GOOD_OBSERVATION_CUTOFF_HOURS = 1.55

if T1Estimator.T1_TARGET_HOURS != T1_TARGET_HOURS_FOR_POOLING:
    raise RuntimeError(
        f"T1Estimator predicts at {T1Estimator.T1_TARGET_HOURS}h but the backtest's ground truth "
        f"is read at {T1_TARGET_HOURS_FOR_POOLING}h - the two must match."
    )

CABIN_COLUMN_TO_KEY = {column: key for key, column in T1Estimator.CABIN_KEY_TO_COLUMN.items()}


def group_keys_by_service(fits_by_instance):
    """serviceId -> list of (serviceId, flightDate) keys with a non-None
    fit - the same grouping pool_slope does internally, exposed here so
    a single held-out key's leave-one-out pool only has to touch its own
    service's instances, not run pool_slope over the whole cabin."""
    by_service = defaultdict(list)
    for key, fit in fits_by_instance.items():
        if fit is not None:
            by_service[key[0]].append(key)
    return by_service


def resolve_leave_one_out_coefficients(conn, org, dest, day_of_week, dep_time, cabin,
                                        loo_c1, loo_slope, night_ratio, global_defaults):
    """{cabin: (c1, slope, nightRatio)} in the shape
    T1Estimator.load_decline_curve_coefficients_for_flight returns, with
    the derived tier built from leave-one-out (value, instanceCount)
    pairs instead of the stored table. Mirrors the tier walk in
    DeclineCurveHierarchy.resolve_coefficients - if that walk changes,
    this must change with it. Returns {} when no slope resolves."""
    min_instances = _load_threshold(conn, org, dest, day_of_week, dep_time, cabin)
    service_override = _load_service_override(conn, org, dest, day_of_week, dep_time, cabin)
    route_override = _load_route_override(conn, org, dest, cabin)
    global_entry = global_defaults.get(cabin, {})

    def resolve_one(quantity, derived, global_key):
        if derived is not None and derived[1] >= min_instances[quantity]:
            return derived[0]
        if service_override and service_override[quantity] is not None:
            return service_override[quantity]
        if route_override and route_override[quantity] is not None:
            return route_override[quantity]
        return global_entry.get(global_key)

    c1 = resolve_one("c1", loo_c1, "c1Hours")
    slope = resolve_one("slope", loo_slope, "slopeSeatsPerHour")
    if slope is None:
        return {}
    return {cabin: (c1, slope, night_ratio)}


@contextmanager
def coefficients_supplied_to_live_estimator(coefficients):
    """Temporarily replaces T1Estimator's database coefficient lookup with
    the given dict, restoring the original on exit."""
    original = T1Estimator.load_decline_curve_coefficients_for_flight
    T1Estimator.load_decline_curve_coefficients_for_flight = lambda *_args: coefficients
    try:
        yield
    finally:
        T1Estimator.load_decline_curve_coefficients_for_flight = original


def live_t1_prediction(conn, org, dest, flight_date, dep_time, readings, cabin, coefficients):
    """The live estimator's T-1 prediction for one cabin as of T-4:
    each reading bracketing T-4 is replayed through
    T1Estimator.compute_t1_replay_column on its own, then the two
    predictions are interpolated by the bracket's weight. None if there
    is no bracket or the estimator has nothing to say."""
    bracket = bracket_with_weight(readings, T4_TARGET_HOURS_FOR_POOLING)
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
    before_prediction = replay(before)
    after_prediction = replay(after)
    if before_prediction is None or after_prediction is None:
        return None
    return before_prediction + (after_prediction - before_prediction) * weight


def backtest_cabin(conn, cabin, fits_by_instance, service_info, night_start_hour, night_end_hour,
                    global_defaults):
    """Leave-one-out T4->T1 backtest for one cabin's fits_by_instance
    (as produced by compute_all_fits' by_cabin[cabin]). Returns
    (per_service, skipped) - per_service is serviceId -> list of
    (actual - predicted) residuals; skipped is a dict of counts by
    reason (noLeaveOneOutSlope, noT4Bracket, noSolidT1Ground,
    noLiveEstimate) for the end-of-run accounting."""
    by_service_keys = group_keys_by_service(fits_by_instance)
    per_service = defaultdict(list)
    skipped = defaultdict(int)

    for key, fit in fits_by_instance.items():
        if fit is None:
            continue
        sid, flight_date = key
        readings = fit["corrected_readings"]

        truth = nearest_reading(readings, T1_TARGET_HOURS_FOR_POOLING)
        if truth is None or truth[0] > GOOD_OBSERVATION_CUTOFF_HOURS:
            skipped["noSolidT1Ground"] += 1
            continue

        loo_dict = {k: fits_by_instance[k] for k in by_service_keys[sid] if k != key}
        loo_slope_entry = pool_slope(loo_dict, night_start_hour, night_end_hour).get(sid)
        if loo_slope_entry is None:
            skipped["noLeaveOneOutSlope"] += 1
            continue
        loo_slope, _gap, loo_slope_n = loo_slope_entry
        loo_c1 = pool_c1(loo_dict).get(sid)

        if bracket_with_weight(readings, T4_TARGET_HOURS_FOR_POOLING) is None:
            skipped["noT4Bracket"] += 1
            continue

        org, dest, day_of_week, dep_time, _ = service_info[sid]
        coefficients = resolve_leave_one_out_coefficients(
            conn, org, dest, day_of_week, dep_time, cabin,
            loo_c1, (loo_slope, loo_slope_n), fit.get("nightRatio", 1.0) or 1.0, global_defaults,
        )
        pred = live_t1_prediction(conn, org, dest, flight_date, dep_time, readings, cabin, coefficients)
        if pred is None:
            skipped["noLiveEstimate"] += 1
            continue

        per_service[sid].append(truth[1] - pred)

    return per_service, skipped


def mean_and_rmse(residuals):
    arr = np.array(residuals, dtype=float)
    return float(np.mean(arr)), float(np.sqrt(np.mean(np.square(arr))))


def main():
    conn = sqlite3.connect(DB_PATH)
    settings = load_settings(conn, key=DECLINE_CURVE_SETTINGS_KEY, defaults=DEFAULT_DECLINE_CURVE_SETTINGS)
    global_defaults = load_settings(
        conn, key=DECLINE_CURVE_GLOBAL_DEFAULTS_KEY, defaults=DEFAULT_DECLINE_CURVE_GLOBAL_DEFAULTS,
    )
    result = compute_all_fits(
        conn, settings["stepChangeRmseThreshold"], settings["stepChangeMaxIterations"],
        night_start_hour=settings["nightStartHour"], night_end_hour=settings["nightEndHour"],
    )
    service_info = result["service_info"]

    print(f"Leave-one-out T-4 -> T-1 backtest of the live estimator (ground truth requires a real "
          f"reading within {GOOD_OBSERVATION_CUTOFF_HOURS}h of departure).")
    print()

    for cabin, cabin_data in result["by_cabin"].items():
        per_service, skipped = backtest_cabin(
            conn, cabin, cabin_data["fits_by_instance"], service_info,
            settings["nightStartHour"], settings["nightEndHour"], global_defaults,
        )

        all_residuals = [r for rs in per_service.values() for r in rs]
        print(f"=== Cabin: {cabin} ===")
        if all_residuals:
            overall_mean, overall_rmse = mean_and_rmse(all_residuals)
            print(f"  {len(all_residuals)} backtestable instances across {len(per_service)} services: "
                  f"mean={overall_mean:+.2f} seats, RMSE={overall_rmse:.2f} seats")
        else:
            print("  No backtestable instances this cabin.")
        print(f"  skipped - no solid T-1 ground truth: {skipped['noSolidT1Ground']}, "
              f"no leave-one-out slope available: {skipped['noLeaveOneOutSlope']}, "
              f"no T-4 bracket: {skipped['noT4Bracket']}, "
              f"no live estimate: {skipped['noLiveEstimate']}")

        ranked = sorted(per_service.items(), key=lambda kv: -len(kv[1]))
        shown = 0
        for sid, residuals in ranked:
            if len(residuals) < 2:
                continue
            mean_r, rmse_r = mean_and_rmse(residuals)
            org, dest, dow, rep_time, _ = service_info[sid]
            hh, mm = divmod(rep_time, 60)
            print(f"    service {sid} ({org}-{dest} {dow} ~{hh:02d}{mm:02d}): "
                  f"n={len(residuals)}, mean={mean_r:+.2f}, RMSE={rmse_r:.2f}")
            shown += 1
            if shown >= 15:
                break
        print()


if __name__ == "__main__":
    main()
