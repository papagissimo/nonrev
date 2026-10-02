import os
import sqlite3
import sys
from collections import defaultdict

import numpy as np

from BottomBacktest import CABINS, Scorer, median_slope
from DeclineCurveFit import DB_PATH, compute_all_fits
from PoolSplitReport import ATTRIBUTES, logged_flights
from PoolSplitReport import grow as grow_pools
from ServiceGrouping import load_open_full_settings
from settings import (
    load_settings, DECLINE_CURVE_SETTINGS_KEY, DEFAULT_DECLINE_CURVE_SETTINGS,
    DECLINE_CURVE_GLOBAL_DEFAULTS_KEY, DEFAULT_DECLINE_CURVE_GLOBAL_DEFAULTS,
)

SAME_SERVICE_TOLERANCE_MINUTES = 30
ARMS = ["hold", "global", "own", "fleet", "pool"]
OWN_COUNT_LABELS = ["0", "1", "2", "3", "4+"]


def own_count_label(count):
    return OWN_COUNT_LABELS[min(count, len(OWN_COUNT_LABELS) - 1)]


def leaves(node):
    if "children" not in node:
        return [node]
    return [leaf for child in node["children"] for leaf in leaves(child)]


def leaf_name(leaf):
    if not leaf["path"]:
        return "Everything"
    return " / ".join(f"{attribute}: {len(levels)} values" if len(levels) > 6 else f"{attribute}: {', '.join(sorted(levels))}"
                      for attribute, levels, _sibling, _p in leaf["path"])


def leaf_index_by_weekly_service(tree):
    index = {}
    for number, leaf in enumerate(leaves(tree)):
        for flight in leaf["flights"]:
            index[(flight["org"], flight["dest"], flight["day"], flight["serviceMinutes"])] = number
    return index


def pool_of_service(service, weekly_service_leaf):
    org, dest, day, rep_minutes, _ = service
    candidates = [(abs(minutes - rep_minutes), leaf) for (o, d, w, minutes), leaf in weekly_service_leaf.items()
                  if (o, d, w) == (org, dest, day)]
    if not candidates:
        return None
    distance, leaf = min(candidates)
    return leaf if distance <= SAME_SERVICE_TOLERANCE_MINUTES else None


def arm_coefficients(scorer, key, own_pool, fleet_fits, pooled_fits):
    return {
        "global": scorer.coefficients(key, own_pool, None),
        "own": scorer.coefficients(key, own_pool, median_slope(own_pool)),
        "fleet": scorer.coefficients(key, own_pool, median_slope(fleet_fits)),
        "pool": scorer.coefficients(key, own_pool, median_slope(pooled_fits)),
    }


def backtest_cabin(scorer, pool_by_service):
    keys_by_pool = defaultdict(list)
    for key, fit in scorer.fits.items():
        if fit is not None and pool_by_service.get(key[0]) is not None:
            keys_by_pool[pool_by_service[key[0]]].append(key)

    errors = defaultdict(lambda: defaultdict(list))
    for key in scorer.fits:
        pool = pool_by_service.get(key[0])
        if pool is None or not scorer.scorable(key):
            continue
        own_pool = scorer.service_pool(key, set())
        pooled_fits = {k: scorer.fits[k] for k in keys_by_pool[pool] if k != key}
        fleet_fits = {k: fit for k, fit in scorer.fits.items() if fit is not None and k != key}
        predictions = {}
        for arm, coefficients in arm_coefficients(scorer, key, own_pool, fleet_fits, pooled_fits).items():
            predictions[arm] = scorer.predict(key, coefficients, None) if coefficients else None
        if any(prediction is None for prediction in predictions.values()):
            continue
        truth = scorer.truth(key)
        row = errors[own_count_label(len(own_pool))]
        row["hold"].append(scorer.hold_error(key))
        for arm, prediction in predictions.items():
            row[arm].append(truth - prediction)
    return errors


def mean_absolute(values):
    return float(np.mean(np.abs(values)))


def print_errors(title, errors):
    print(title)
    print(f"  {'own flights':<12}{'cases':>6}" + "".join(f"{arm + ' MAE':>12}" for arm in ARMS))
    for label in OWN_COUNT_LABELS + ["all"]:
        if label == "all":
            by_arm = {arm: [e for row in errors.values() for e in row[arm]] for arm in ARMS}
        else:
            by_arm = errors.get(label)
        if not by_arm or not by_arm["own"]:
            continue
        print(f"  {label:<12}{len(by_arm['own']):>6}" + "".join(f"{mean_absolute(by_arm[arm]):>12.2f}" for arm in ARMS))
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

    print("Leave-one-out T-4 -> T-1 backtest: which slope predicts T-1 best?")
    print("  hold = the T-4 reading carried forward unchanged, as the live estimator does;")
    print("  global = override/global default only; own = median slope of this weekly service's other flights;")
    print("  fleet = median slope of every other flight; pool = median slope of every other flight in this weekly")
    print("  service's pool. C1 is handled the same in every column.")
    print("  Rows are split by how many other flights the weekly service has of its own. MAE in seats.")
    print()
    for number, leaf in enumerate(leaves(tree)):
        print(f"  Pool {number + 1}: {leaf_name(leaf)}")
    print()

    combined = defaultdict(lambda: defaultdict(list))
    for cabin in CABINS:
        cabin_data = result["by_cabin"][cabin]
        scorer = Scorer(conn, cabin, cabin_data["fits_by_instance"], cabin_data["instances"],
                        service_info, global_defaults, settings["nightStartHour"], settings["nightEndHour"])
        errors = backtest_cabin(scorer, pool_by_service)
        print_errors(f"Cabin {cabin}", errors)
        for label, by_arm in errors.items():
            for arm, values in by_arm.items():
                combined[label][arm].extend(values)
    print_errors("All cabins", combined)


if __name__ == "__main__":
    main()
