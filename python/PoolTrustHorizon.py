import os
import sqlite3
import sys


from DeclineCurveFit import DB_PATH, compute_all_fits
from HorizonDecisionBacktest import CABINS, GOOD_OBSERVATION_CUTOFF_HOURS, CabinModel, box
from PoolBacktest import grow_pools, leaf_index_by_weekly_service, leaf_name, leaves, pool_of_service
from PoolSplitReport import logged_flights
from ServiceGrouping import load_open_full_settings
from settings import (
    load_settings, DECLINE_CURVE_SETTINGS_KEY, DEFAULT_DECLINE_CURVE_SETTINGS,
    DECLINE_CURVE_GLOBAL_DEFAULTS_KEY, DEFAULT_DECLINE_CURVE_GLOBAL_DEFAULTS,
)

TOLERANCES = [1 / 20, 1 / 50]
SHOWN_HOURS = [3, 4, 6, 8, 12, 24, 48]


def latest_known(readings, hours):
    earlier = [reading for reading in readings if reading[0] >= hours]
    return min(earlier, key=lambda reading: reading[0])[1] if earlier else None


def open_calls(models, pool_by_service, thresholds):
    calls = {}
    day_keys = set.intersection(*(set(model.instances) for model in models))
    for key in day_keys:
        pool = pool_by_service.get(key[0])
        truths = [model.truth(key) for model in models]
        if pool is None or None in truths:
            continue
        ended_full = box(sum(truths), thresholds) == "full"
        per_cabin = [[r for r in model.raw_readings(key) if r[0] > GOOD_OBSERVATION_CUTOFF_HOURS] for model in models]
        for hours in sorted({reading[0] for readings in per_cabin for reading in readings}):
            held = [latest_known(readings, hours) for readings in per_cabin]
            if None not in held and box(sum(held), thresholds) == "open":
                calls.setdefault(pool, []).append((hours, ended_full))
    return calls


def burned_rate_within(calls, hours):
    within = [ended_full for reading_hours, ended_full in calls if reading_hours <= hours]
    return (sum(within) / len(within), len(within)) if within else (None, 0)


def trust_horizon(calls, tolerance):
    trusted = None
    for hours, _ in sorted(calls):
        rate, _count = burned_rate_within(calls, hours)
        if rate <= tolerance:
            trusted = hours
    return trusted


def print_pool(title, calls):
    print(title)
    if not calls:
        print("  no open readings\n")
        return
    burned = sum(ended_full for _, ended_full in calls)
    print(f"  {len(calls)} open readings, {burned} on flights that ended full")
    for tolerance in TOLERANCES:
        horizon = trust_horizon(calls, tolerance)
        shown = "never" if horizon is None else f"T-{horizon:.1f}"
        print(f"  burned at most 1 in {round(1 / tolerance)}: trust an open reading from {shown}")
    rates = []
    for hours in SHOWN_HOURS:
        rate, count = burned_rate_within(calls, hours)
        if count:
            rates.append(f"T-{hours} {rate:.0%} ({count})")
    print("  burned rate for open readings within: " + ", ".join(rates))
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

    print("How far out can an open reading be trusted? Every logged reading that says open (three cabins")
    print("summed, each cabin's latest known reading), scored against whether the flight ended full.")
    print("Trust from T-N means: of all open readings taken within N hours, at most the tolerance ended full.")
    print()
    calls = open_calls(models, pool_by_service, thresholds)
    for number, leaf in enumerate(leaves(tree)):
        print_pool(f"Pool {number + 1}: {leaf_name(leaf)}", calls.get(number, []))


if __name__ == "__main__":
    main()
