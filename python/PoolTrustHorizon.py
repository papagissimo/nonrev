import os
import sqlite3
import sys

from Pools import PoolSnapshot, TRUST_BAR, TRUST_TOLERANCE, open_readings_by_leaf, risk_curve, trust_horizon

DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'nonrev.db')
TOLERANCES = [1 / 20, TRUST_TOLERANCE]
SHOWN_HOURS = [2, 3, 4, 5, 6, 8, 12, 24, 48]


def print_pool(name, open_readings):
    print(name)
    if not open_readings:
        print("  no open readings\n")
        return
    ended_full = sum(full for _, full in open_readings)
    print(f"  {len(open_readings)} open readings, {ended_full} on flights that ended full")
    for tolerance in TOLERANCES:
        horizon = trust_horizon(open_readings, tolerance)
        shown = "no horizon" if horizon is None else f"T-{horizon:.1f}"
        print(f"  risk at most 1 in {round(1 / tolerance)}: trust an open reading from {shown}")
    risk_at = risk_curve(open_readings)
    print("  risk an open reading taken at that hour ends full: " +
          ", ".join(f"T-{hours} {risk_at(hours):.0%}" for hours in SHOWN_HOURS))
    print()


def main():
    if not os.path.exists(DB_PATH):
        sys.exit(f"Database not found at {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    pools = PoolSnapshot(conn)
    print("How far out can an open reading be trusted? Every logged reading that says open (three cabins")
    print("summed, each cabin's latest known reading), scored against whether the flight ended full.")
    print("Risk by hour is a monotone fit (isotonic regression): risk never falls with distance from departure.")
    print("Trust from T-N: the farthest hour whose fitted risk is within the tolerance, kept only if that few")
    print(f"ending full inside it would happen by luck less than {TRUST_BAR:g} of the time at the pool's overall rate.")
    print()
    open_readings = open_readings_by_leaf(conn, pools.tree, pools.thresholds)
    for leaf in pools.pool_leaves:
        print_pool(pools.names[id(leaf)], open_readings.get(id(leaf), []))


if __name__ == "__main__":
    main()
