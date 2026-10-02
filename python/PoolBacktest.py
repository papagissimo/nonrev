import os
import sqlite3
import sys
from collections import defaultdict

import numpy as np

from BottomBacktest import CABINS, Scorer, median_slope, t4_anchors
from DeclineCurveFit import DB_PATH, compute_all_fits
from Pools import leaves, logged_flights
from Pools import grow as grow_pools
from ServiceGrouping import load_open_full_settings
from settings import (
    load_settings, DECLINE_CURVE_SETTINGS_KEY, DEFAULT_DECLINE_CURVE_SETTINGS,
    DECLINE_CURVE_GLOBAL_DEFAULTS_KEY, DEFAULT_DECLINE_CURVE_GLOBAL_DEFAULTS,
)

SAME_SERVICE_TOLERANCE_MINUTES = 30
ARMS = ["hold", "hold-fleet", "hold-pool", "global", "own", "fleet", "pool"]
OWN_COUNT_LABELS = ["0", "1", "2", "3", "4+"]


def own_count_label(count):
    return OWN_COUNT_LABELS[min(count, len(OWN_COUNT_LABELS) - 1)]


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


def typical_drop(drops, excluded_key):
    others = [drop for key, drop in drops.items() if key != excluded_key]
    return float(np.median(others)) if others else 0.0


def backtest_cabin(scorer, pool_by_service):
    keys_by_pool = defaultdict(list)
    for key, fit in scorer.fits.items():
        if fit is not None and pool_by_service.get(key[0]) is not None:
            keys_by_pool[pool_by_service[key[0]]].append(key)
    drops = {key: -scorer.hold_error(key) for key in scorer.fits
             if pool_by_service.get(key[0]) is not None and scorer.scorable(key)}
    drops_by_pool = defaultdict(dict)
    for key, drop in drops.items():
        drops_by_pool[pool_by_service[key[0]]][key] = drop

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
        hold_error = scorer.hold_error(key)
        anchor = truth - hold_error
        row["hold"].append(hold_error)
        row["hold-fleet"].append(truth - max(0.0, anchor - typical_drop(drops, key)))
        row["hold-pool"].append(truth - max(0.0, anchor - typical_drop(drops_by_pool[pool], key)))
        for arm, prediction in predictions.items():
            row[arm].append(truth - prediction)
    return errors


def mean_absolute(values):
    return float(np.mean(np.abs(values)))


def print_errors(title, errors):
    print(title)
    print(f"  {'own flights':<12}{'cases':>6}" + "".join(f"{arm:>11}" for arm in ARMS))
    for label in OWN_COUNT_LABELS + ["all"]:
        if label == "all":
            by_arm = {arm: [e for row in errors.values() for e in row[arm]] for arm in ARMS}
        else:
            by_arm = errors.get(label)
        if not by_arm or not by_arm["own"]:
            continue
        print(f"  {label:<12}{len(by_arm['own']):>6}" + "".join(f"{mean_absolute(by_arm[arm]):>11.2f}" for arm in ARMS))
    print()


SUMMED_METHODS = ["hold", "fleet median", "pool median", "fleet mean", "pool mean"]


def summed_service_days(scorers, pool_by_service):
    days = {}
    keys = set().union(*(scorer.fits.keys() for scorer in scorers))
    for key in keys:
        if pool_by_service.get(key[0]) is None:
            continue
        anchors, truths = [], []
        for scorer in scorers:
            if key not in scorer.fits or scorer.truth(key) is None or t4_anchors(scorer.raw_readings(key)) is None:
                break
            truths.append(scorer.truth(key))
            anchors.append(truths[-1] - scorer.hold_error(key))
        else:
            days[key] = (sum(anchors), sum(truths))
    return days


def typical(drops, statistic):
    return float(statistic(drops)) if drops else 0.0


def verdict(t1, thresholds):
    if t1 <= thresholds["fullThreshold"]:
        return "full"
    if t1 >= thresholds["openThreshold"]:
        return "open"
    return "between"


def backtest_summed(days, pool_by_service, thresholds):
    drops = {key: anchor - truth for key, (anchor, truth) in days.items()}
    scores = {method: {"errors": [], "right": 0, "openWasFull": 0, "fullWasOpen": 0} for method in SUMMED_METHODS}
    for key, (anchor, truth) in days.items():
        fleet = [drop for other, drop in drops.items() if other != key]
        pool = [drop for other, drop in drops.items()
                if other != key and pool_by_service[other[0]] == pool_by_service[key[0]]]
        corrections = {
            "hold": 0.0,
            "fleet median": typical(fleet, np.median), "pool median": typical(pool, np.median),
            "fleet mean": typical(fleet, np.mean), "pool mean": typical(pool, np.mean),
        }
        actual = verdict(truth, thresholds)
        for method, correction in corrections.items():
            prediction = max(0.0, anchor - correction)
            called = verdict(prediction, thresholds)
            score = scores[method]
            score["errors"].append(truth - prediction)
            score["right"] += called == actual
            score["openWasFull"] += called == "open" and actual == "full"
            score["fullWasOpen"] += called == "full" and actual == "open"
    return scores


def print_summed(days, scores):
    drops = [anchor - truth for anchor, truth in days.values()]
    print(f"Three cabins summed: {len(days)} service-days; T-4 -> T-1 change median {np.median(drops):g}, "
          f"mean {np.mean(drops):.2f} seats; {np.mean(np.array(drops) == 0):.0%} unchanged")
    print(f"  {'method':<14}{'MAE':>6}{'right call':>12}{'open, was full':>16}{'full, was open':>16}")
    for method in SUMMED_METHODS:
        score = scores[method]
        print(f"  {method:<14}{mean_absolute(score['errors']):>6.2f}{score['right'] / len(days):>12.1%}"
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

    print("Leave-one-out T-4 -> T-1 backtest: which slope predicts T-1 best?")
    print("  hold = the T-4 reading carried forward unchanged, as the live estimator does;")
    print("  hold-fleet / hold-pool = the T-4 reading minus the median T-4 -> T-1 drop of every other")
    print("  flight / every other flight in its pool, never below zero;")
    print("  global = override/global default only; own = median slope of this weekly service's other flights;")
    print("  fleet = median slope of every other flight; pool = median slope of every other flight in this weekly")
    print("  service's pool. C1 is handled the same in every column.")
    print("  Rows are split by how many other flights the weekly service has of its own. MAE in seats.")
    print()
    for number, leaf in enumerate(leaves(tree)):
        print(f"  Pool {number + 1}: {leaf_name(leaf)}")
    print()

    combined = defaultdict(lambda: defaultdict(list))
    scorers = []
    for cabin in CABINS:
        cabin_data = result["by_cabin"][cabin]
        scorer = Scorer(conn, cabin, cabin_data["fits_by_instance"], cabin_data["instances"],
                        service_info, global_defaults, settings["nightStartHour"], settings["nightEndHour"])
        scorers.append(scorer)
        errors = backtest_cabin(scorer, pool_by_service)
        print_errors(f"Cabin {cabin}", errors)
        for label, by_arm in errors.items():
            for arm, values in by_arm.items():
                combined[label][arm].extend(values)
    print_errors("All cabins", combined)
    days = summed_service_days(scorers, pool_by_service)
    print_summed(days, backtest_summed(days, pool_by_service, thresholds))


if __name__ == "__main__":
    main()
