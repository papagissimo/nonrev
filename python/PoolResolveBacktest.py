import os
import sqlite3
import sys
from collections import defaultdict

from DeclineCurveFit import DB_PATH, compute_all_fits
from HorizonDecisionBacktest import CABINS, CabinModel, anchor_readings, box, horizon_tolerance
from PoolBacktest import leaf_index_by_weekly_service, leaf_name, leaves, pool_of_service
from PoolBacktest import grow_pools
from PoolSplitReport import logged_flights
from ServiceGrouping import load_open_full_settings
from settings import (
    load_settings, DECLINE_CURVE_SETTINGS_KEY, DEFAULT_DECLINE_CURVE_SETTINGS,
    DECLINE_CURVE_GLOBAL_DEFAULTS_KEY, DEFAULT_DECLINE_CURVE_GLOBAL_DEFAULTS,
)

HORIZONS = [3.0, 4.0, 6.0, 8.0, 12.0, 24.0]


def held_seats(model, key, horizon):
    anchors = anchor_readings(model.raw_readings(key), horizon, horizon_tolerance(horizon))
    if anchors is None:
        return None
    return sum(reading[1] * weight for reading, weight in anchors)


def score_by_pool(models, pool_by_service, thresholds):
    scores = defaultdict(lambda: defaultdict(lambda: {"days": 0, "right": 0, "openWasFull": 0, "fullWasOpen": 0}))
    day_keys = set.intersection(*(set(model.instances) for model in models))
    for key in day_keys:
        pool = pool_by_service.get(key[0])
        truths = [model.truth(key) for model in models]
        if pool is None or None in truths:
            continue
        actual = box(sum(truths), thresholds)
        for horizon in HORIZONS:
            held = [held_seats(model, key, horizon) for model in models]
            if None in held:
                continue
            called = box(sum(held), thresholds)
            score = scores[pool][horizon]
            score["days"] += 1
            score["right"] += called == actual
            score["openWasFull"] += called == "open" and actual == "full"
            score["fullWasOpen"] += called == "full" and actual == "open"
    return scores


def print_pool(title, by_horizon):
    print(title)
    print(f"  {'from':<6}{'days':>6}{'right call':>12}{'open, was full':>16}{'full, was open':>16}")
    for horizon in HORIZONS:
        score = by_horizon.get(horizon)
        if not score or not score["days"]:
            continue
        print(f"  T-{horizon:<4g}{score['days']:>6}{score['right'] / score['days']:>12.0%}"
              f"{score['openWasFull']:>16}{score['fullWasOpen']:>16}")
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
    tree = grow_pools(logged_flights(conn, thresholds["openThreshold"]), [])
    weekly_service_leaf = leaf_index_by_weekly_service(tree)
    result = compute_all_fits(
        conn, settings["stepChangeRmseThreshold"], settings["stepChangeMaxIterations"],
        night_start_hour=settings["nightStartHour"], night_end_hour=settings["nightEndHour"],
    )
    service_info = result["service_info"]
    pool_by_service = {sid: pool_of_service(service, weekly_service_leaf) for sid, service in service_info.items()}
    models = [CabinModel(conn, cabin, result["by_cabin"][cabin], service_info, settings, global_defaults)
              for cabin in CABINS]

    print("Hold the reading from each horizon, three cabins summed: how often does it make the right T-1 call?")
    print(f"Full <= {thresholds['fullThreshold']}, open >= {thresholds['openThreshold']}. "
          "Each horizon counts only the days with a reading near it.")
    print()
    scores = score_by_pool(models, pool_by_service, thresholds)
    for number, leaf in enumerate(leaves(tree)):
        print_pool(f"Pool {number + 1}: {leaf_name(leaf)}", scores.get(number, {}))


if __name__ == "__main__":
    main()
