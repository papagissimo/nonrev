"""
DriftShiftBacktest.py

One-off analysis: does shifting the held reading by the typical drift from its
horizon to T-1 call the T-1 box better than plain hold? Report only; nothing is
written to the database.

Days, anchors, truth and boxes are HorizonDecisionBacktest's: all three cabins
summed per service-day, raw logged readings, truth within the T-1 window.

  hold          the reading at the horizon, carried to T-1 unchanged
  fleet median  hold plus the median change to T-1 of every other day at that horizon
  fleet mean    hold plus the mean change, same days
  band median   hold plus the median change of every other day at that horizon
                whose held value falls in the same band
"""
import os
import sqlite3
import sys
from collections import defaultdict

import numpy as np

import HorizonDecisionBacktest as H

BANDS = [(0, 2.5), (2.5, 7.5), (7.5, 15.5), (15.5, float("inf"))]
ARMS = ["hold", "fleet median", "fleet mean", "band median"]


def band_of(seats):
    return next(index for index, (low, high) in enumerate(BANDS) if low <= seats < high)


def leave_one_out(values, index, statistic):
    others = values[:index] + values[index + 1:]
    return statistic(others) if others else 0.0


def predictions_for(scored):
    holds = [d[5] for d in scored]
    changes = [d[4] - d[5] for d in scored]
    bands = [band_of(hold) for hold in holds]
    by_band = defaultdict(list)
    for position, band in enumerate(bands):
        by_band[band].append(position)

    rows = []
    for position, (hold, change) in enumerate(zip(holds, changes)):
        same_band = [changes[p] for p in by_band[bands[position]] if p != position]
        rows.append({
            "hold": hold,
            "fleet median": hold + leave_one_out(changes, position, np.median),
            "fleet mean": hold + leave_one_out(changes, position, np.mean),
            "band median": hold + (np.median(same_band) if same_band else 0.0),
        })
    return rows


def report_horizon(horizon, scored, thresholds):
    truths = [d[4] for d in scored]
    truth_boxes = [d[1] for d in scored]
    rows = predictions_for(scored)
    print(f"=== From T-{horizon:g}: {len(scored)} days ===")
    print(f"  {'arm':<13} {'right':>6} {'open->full':>11} {'full->open':>11} {'off by 1 box':>13} {'MAE':>6}")
    for arm in ARMS:
        predicted = [row[arm] for row in rows]
        boxes = [H.box(p, thresholds) for p in predicted]
        right = sum(b == t for b, t in zip(boxes, truth_boxes))
        open_full = sum(b == "open" and t == "full" for b, t in zip(boxes, truth_boxes))
        full_open = sum(b == "full" and t == "open" for b, t in zip(boxes, truth_boxes))
        near_miss = len(scored) - right - open_full - full_open
        mae = np.mean([abs(p - t) for p, t in zip(predicted, truths)])
        print(f"  {arm:<13} {right / len(scored):>6.1%} {open_full:>11} {full_open:>11} {near_miss:>13} {mae:>6.2f}")
    changes = [d[4] - d[5] for d in scored]
    print(f"  shift applied: fleet median {np.median(changes):+.1f}, fleet mean {np.mean(changes):+.2f}")
    print()


def main():
    if not os.path.exists(H.DB_PATH):
        sys.exit(f"Database not found at {H.DB_PATH}")
    conn = sqlite3.connect(H.DB_PATH)
    settings = H.load_settings(conn, key=H.DECLINE_CURVE_SETTINGS_KEY, defaults=H.DEFAULT_DECLINE_CURVE_SETTINGS)
    global_defaults = H.load_settings(
        conn, key=H.DECLINE_CURVE_GLOBAL_DEFAULTS_KEY, defaults=H.DEFAULT_DECLINE_CURVE_GLOBAL_DEFAULTS,
    )
    thresholds = H.load_open_full_settings(conn)
    result = H.compute_all_fits(
        conn, settings["stepChangeRmseThreshold"], settings["stepChangeMaxIterations"],
        night_start_hour=settings["nightStartHour"], night_end_hour=settings["nightEndHour"],
    )
    models = [H.CabinModel(conn, cabin, result["by_cabin"][cabin], result["service_info"], settings, global_defaults)
              for cabin in H.CABINS]
    days = H.score_days(models, thresholds)
    print(f"Hold vs drift-shifted hold, T-1 box per service-day (all cabins summed; full <= "
          f"{thresholds['fullThreshold']}, open >= {thresholds['openThreshold']}). Shifts are leave-one-out.\n")
    for horizon in H.HORIZONS:
        report_horizon(horizon, days[horizon], thresholds)


if __name__ == "__main__":
    main()
