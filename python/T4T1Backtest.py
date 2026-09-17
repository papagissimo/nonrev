"""
T4T1Backtest.py

Answers one question: given only what's knowable at T-4h, how well does
the pooled slope predict what T-1h will actually turn out to be? A
one-off analysis script, not part of the live estimator or the settings
hierarchy - run it, read the report, decide whether T-4 is worth
building anything further around.

LEAVE-ONE-OUT, NOT IN-SAMPLE. pool_slope (DeclineCurveFit.py) already
optimizes a candidate slope against exactly this kind of T-4-predicts-T-1
accuracy - but it scores each candidate slope using the SAME instances
that slope is then used for. Reporting that in-sample residual directly
would be circular: the slope was chosen, in part, to make that residual
small. This script instead holds out one instance at a time, pools the
slope from every OTHER instance of that same (day-of-week-specific)
service, and predicts the held-out instance's T-1 using that outside
slope - so no instance's own T-1 reading ever influences the slope used
to predict it.

C1 needs no leave-one-out treatment: predict_t1_via_slide re-derives C1
directly from the T-4 bracketing reading itself (solve_c1_from_reading),
never from a pooled/service-level C1, so there's nothing here to leak.

GROUND TRUTH GATE. An instance only counts as a backtest case if its
nearest-to-T-1 reading is a real observation within 1.55h of departure
(his stated golden-ticket "solid ground truth" cutoff - see
nonrev-sqlite-rewrite notes). A nearest reading further out than that is
an estimate, not ground truth, and comparing a prediction against it
would just be comparing two guesses.

Reuses, unchanged: DeclineCurveFit.compute_all_fits (for the fitted,
step-corrected per-instance data), pool_slope, predict_t1_via_slide,
nearest_reading, and the T4_TARGET_HOURS_FOR_POOLING /
T1_TARGET_HOURS_FOR_POOLING constants already defined there - this
script's own call sites and logic needed no edits when pool_slope's
internal objective changed 2026-09-17 (it now trains each candidate
slope against every instance's own last reading, not a fixed T-1 - see
DeclineCurveFit's module docstring). That's a deliberate difference,
not a leftover inconsistency: the loo_slope computed at line ~87 is
trained on that general, unbiased objective, then evaluated here
specifically at T-1 against this script's own stricter
GOOD_OBSERVATION_CUTOFF_HOURS ground-truth gate - training and
evaluation targets differing is normal, not something to reconcile.
Nothing here is written back to the database - report only, printed to
console.
"""
import sqlite3
from collections import defaultdict

import numpy as np

from settings import load_settings, DECLINE_CURVE_SETTINGS_KEY, DEFAULT_DECLINE_CURVE_SETTINGS
from DeclineCurveFit import (
    DB_PATH, compute_all_fits, pool_slope, predict_t1_via_slide, nearest_reading,
    T4_TARGET_HOURS_FOR_POOLING, T1_TARGET_HOURS_FOR_POOLING,
)

GOOD_OBSERVATION_CUTOFF_HOURS = 1.55


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


def backtest_cabin(fits_by_instance, night_start_hour, night_end_hour):
    """Leave-one-out T4->T1 backtest for one cabin's fits_by_instance
    (as produced by compute_all_fits' by_cabin[cabin]). Returns
    (per_service, skipped) - per_service is serviceId -> list of
    (actual - predicted) residuals; skipped is a dict of counts by
    reason (noLeaveOneOutSlope, noT4Bracket, noSolidT1Ground) for the
    end-of-run accounting."""
    by_service_keys = group_keys_by_service(fits_by_instance)
    per_service = defaultdict(list)
    skipped = defaultdict(int)

    for key, fit in fits_by_instance.items():
        if fit is None:
            continue
        sid, _flight_date = key
        readings = fit["corrected_readings"]

        truth = nearest_reading(readings, T1_TARGET_HOURS_FOR_POOLING)
        if truth is None or truth[0] > GOOD_OBSERVATION_CUTOFF_HOURS:
            skipped["noSolidT1Ground"] += 1
            continue

        loo_dict = {k: fits_by_instance[k] for k in by_service_keys[sid] if k != key}
        loo_pooled = pool_slope(loo_dict, night_start_hour, night_end_hour)
        loo_entry = loo_pooled.get(sid)
        if loo_entry is None:
            skipped["noLeaveOneOutSlope"] += 1
            continue
        loo_slope, _gap, _n = loo_entry

        pred = predict_t1_via_slide(
            readings, loo_slope, T4_TARGET_HOURS_FOR_POOLING, T1_TARGET_HOURS_FOR_POOLING,
            night_ratio=fit.get("nightRatio", 1.0) or 1.0,
            departure_dt=fit.get("departureDt"),
            night_start_hour=night_start_hour, night_end_hour=night_end_hour,
        )
        if pred is None:
            skipped["noT4Bracket"] += 1
            continue

        per_service[sid].append(truth[1] - pred)

    return per_service, skipped


def mean_and_rmse(residuals):
    arr = np.array(residuals, dtype=float)
    return float(np.mean(arr)), float(np.sqrt(np.mean(np.square(arr))))


def main():
    conn = sqlite3.connect(DB_PATH)
    settings = load_settings(conn, key=DECLINE_CURVE_SETTINGS_KEY, defaults=DEFAULT_DECLINE_CURVE_SETTINGS)
    result = compute_all_fits(
        conn, settings["stepChangeRmseThreshold"], settings["stepChangeMaxIterations"],
        night_start_hour=settings["nightStartHour"], night_end_hour=settings["nightEndHour"],
    )
    service_info = result["service_info"]

    print(f"Leave-one-out T-4 -> T-1 backtest (ground truth requires a real reading "
          f"within {GOOD_OBSERVATION_CUTOFF_HOURS}h of departure).")
    print()

    for cabin, cabin_data in result["by_cabin"].items():
        per_service, skipped = backtest_cabin(
            cabin_data["fits_by_instance"], settings["nightStartHour"], settings["nightEndHour"],
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
              f"no T-4 bracket: {skipped['noT4Bracket']}")

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
