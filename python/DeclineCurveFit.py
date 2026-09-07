"""
DeclineCurveFit.py

First step of the seat decline-curve modeling work (see decline-curve-handoff.md).

This is a standalone, exploratory/calibration script - not wired into any live
dialog or estimator. It operates on the whole population of observations at
once, which is a fundamentally different kind of computation from everything
else in this codebase (per-instance logging, per-day scheduling), so it lives
on its own rather than folded into GraphObservations.py.

What it does:
  1. Clusters flightSchedule departure times per route into "services"
     (gap-based clustering - trusts that real clusters separate cleanly,
     confirmed against real data in a prior session).
  2. Joins observations to flightSchedule (via carrier/flightNumber/org/dest/
     dayOfWeek, dayOfWeek derived from flightDate) to attach each observation
     to a service + depTime.
  3. For each (service, cabin), extracts:
       - C1 brackets: [lower, upper] bounding when the cabin left 9
       - gap brackets: [lower, upper] bounding the C1-to-C2 gap, derived from
         each instance's C1 bracket and C2 bracket (C2 = C1 + gap, enforced
         by construction rather than fit independently - this prevents C2
         from ever being placed before C1 in the fitted model)
     Per-row cabin state uses the real (binary-search) value when present,
     falls back to the glance/cheap value when not, skips the row for that
     cabin when neither is present.
  4. Fits lifelines' interval-censored KaplanMeierFitter (the Turnbull
     estimator) to each population and reports summary results.

Coefficient selection (what single number to hand the live estimator) and
any curve-fitting against the fitted distributions is explicitly NOT this
script's job yet - that's a follow-on step once this output is reviewed.

Run directly for a console report:
    python3 DeclineCurveFit.py
"""

import sqlite3
from collections import defaultdict
from datetime import datetime

import numpy as np

DB_PATH = "nonrev.db"

CABIN_COLUMNS = {
    "y": ("y", "cheapY"),
    "cPlus": ("cPlus", "cheapCPlus"),
    "firstOrPS": ("firstOrPS", "cheapFirstOrPS"),
    "d1": ("d1", "cheapD1"),
}

# Gap-based clustering threshold for grouping distinct depTimes (minutes since
# midnight) into services on a route. Real services are hours apart; real
# intra-service spread is small (usually 5-20 min, occasionally up to ~65 min
# per prior flagged cases). 90 minutes is a deliberately generous gap that
# still won't merge two real services, per the settled clustering approach.
SERVICE_CLUSTER_GAP_MINUTES = 90

# lifelines' fit_interval_censoring rejects a literal -inf lower bound
# outright (confirmed directly - it errors even though it accepts +inf on
# the upper side without complaint). This is a library quirk, not a math
# one: a sufficiently large-magnitude finite stand-in behaves identically,
# for the same reason a finite right-censoring cutoff doesn't matter as
# long as it's beyond all real data (same idea already worked through for
# the right-censored/OPEN_PROXY side).
NEGATIVE_INFINITY_STANDIN = -1e6
# at departure), used only because real full/open/iffy classification isn't
# built yet. A right-censored C1 reading (still 9, no later reading that day)
# is only kept as informative evidence for the fit when it's this close to
# departure - matches the existing golden-ticket window. Far-from-departure
# right-censored readings carry almost no information (the corner could have
# happened five minutes later, or five hours later, or not at all that day)
# and are dropped from the fit entirely rather than treated as evidence.
# Replace this with real per-(service,cabin) classification once it exists -
# this is standing in for that, not a permanent corner-fitting rule.
OPEN_PROXY_HOURS_BEFORE_DEP = 1.5


def cluster_service_times(dep_times):
    """Gap-based clustering of a sorted list of depTimes (minutes since
    midnight) into services. Returns a list of clusters, each a list of the
    original depTimes belonging to that cluster."""
    if not dep_times:
        return []
    times = sorted(dep_times)
    clusters = [[times[0]]]
    for t in times[1:]:
        if t - clusters[-1][-1] <= SERVICE_CLUSTER_GAP_MINUTES:
            clusters[-1].append(t)
        else:
            clusters.append([t])
    return clusters


def build_service_map(conn):
    """Returns dict: (org, dest, depTime) -> serviceId (int), plus a dict
    serviceId -> (org, dest, representative_depTime) for reporting."""
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT org, dest, depTime FROM flightSchedule")
    rows = cur.fetchall()

    by_route = defaultdict(set)
    for org, dest, depTime in rows:
        by_route[(org, dest)].add(depTime)

    dep_time_to_service = {}
    service_info = {}
    next_service_id = 1

    for (org, dest), times in by_route.items():
        clusters = cluster_service_times(list(times))
        for cluster in clusters:
            service_id = next_service_id
            next_service_id += 1
            rep_time = int(round(sum(cluster) / len(cluster)))
            service_info[service_id] = (org, dest, rep_time, len(cluster))
            for t in cluster:
                dep_time_to_service[(org, dest, t)] = service_id

    return dep_time_to_service, service_info


def load_observations_with_schedule(conn):
    """Joins observations to flightSchedule (via carrier/flightNumber/org/
    dest/dayOfWeek) to attach depTime. Returns a list of dict rows. Rows with
    no matching flightSchedule entry are dropped (reported separately)."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT observationId, carrier, flightNumber, org, dest, flightDate,
               checkTimestamp, hoursBeforeDep, readingType,
               y, cPlus, firstOrPS, d1,
               cheapY, cheapCPlus, cheapFirstOrPS, cheapD1
        FROM observations
        WHERE readingType = 'avail'
        """
    )
    obs_rows = cur.fetchall()
    col_names = [d[0] for d in cur.description]

    cur.execute("SELECT carrier, flightNumber, org, dest, dayOfWeek, depTime FROM flightSchedule")
    sched_lookup = {}
    for carrier, flightNumber, org, dest, dow, depTime in cur.fetchall():
        sched_lookup[(carrier, flightNumber, org, dest, dow)] = depTime

    matched = []
    unmatched_count = 0
    for row in obs_rows:
        r = dict(zip(col_names, row))
        try:
            dow = datetime.strptime(r["flightDate"], "%Y-%m-%d").strftime("%a")
        except ValueError:
            unmatched_count += 1
            continue
        key = (r["carrier"], r["flightNumber"], r["org"], r["dest"], dow)
        depTime = sched_lookup.get(key)
        if depTime is None:
            unmatched_count += 1
            continue
        r["depTime"] = depTime
        r["dayOfWeek"] = dow
        matched.append(r)

    return matched, unmatched_count


def resolved_cabin_value(row, real_col, cheap_col):
    """Real (binary-search) value takes precedence; falls back to glance
    value; None if neither present. Coerces stray blank/whitespace cells."""
    for col in (real_col, cheap_col):
        v = row.get(col)
        if v is None:
            continue
        try:
            return int(v)
        except (ValueError, TypeError):
            continue
    return None


def parse_check_timestamp(ts):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(ts, fmt)
        except ValueError:
            continue
    return None


def extract_brackets(matched_rows, dep_time_to_service, cabin):
    """For one cabin, groups matched observation rows into instances
    (serviceId, flightDate), and within each instance extracts:
      - the C1 bracket (last-known-9 -> first-known-non-9), in a
        monotonically-increasing-with-time coordinate (timeCoord = -hoursBeforeDep)
      - the C2 bracket (last-known-nonzero -> first-known-0), same coordinate
    Returns (c1_brackets, c2_brackets), each a list of (lower, upper) tuples
    using np.inf for right-censored (never resolved) sides.
    """
    real_col, cheap_col = CABIN_COLUMNS[cabin]

    instances = defaultdict(list)
    for r in matched_rows:
        val = resolved_cabin_value(r, real_col, cheap_col)
        if val is None:
            continue
        service_id = dep_time_to_service.get((r["org"], r["dest"], r["depTime"]))
        if service_id is None:
            continue
        if r["hoursBeforeDep"] is None:
            continue
        try:
            hbd = float(r["hoursBeforeDep"])
        except (ValueError, TypeError):
            continue
        instances[(service_id, r["flightDate"])].append((hbd, val))

    c1_brackets = []
    c2_brackets = []

    for key, readings in instances.items():
        # sort by time coordinate ascending (timeCoord = -hoursBeforeDep,
        # so this is real chronological order within the day)
        readings.sort(key=lambda x: -x[0])
        time_coords = [-hbd for hbd, _ in readings]
        vals = [v for _, v in readings]

        # --- C1: leaves 9 ---
        last_nine_idx = None
        first_non_nine_idx = None
        for i, v in enumerate(vals):
            if v >= 9:
                last_nine_idx = i
            elif first_non_nine_idx is None:
                first_non_nine_idx = i
        if last_nine_idx is not None and first_non_nine_idx is not None and first_non_nine_idx > last_nine_idx:
            c1_lower = time_coords[last_nine_idx]
            c1_upper = time_coords[first_non_nine_idx]
        elif last_nine_idx is not None and first_non_nine_idx is None:
            # right-censored: still 9 at every check we have. Only keep this
            # as evidence if the last check was close to departure (OPEN_PROXY
            # threshold) - otherwise we genuinely don't know enough to say
            # anything, and including it would just park uninformative mass
            # at infinity in the fit.
            last_nine_hbd = readings[last_nine_idx][0]
            if last_nine_hbd <= OPEN_PROXY_HOURS_BEFORE_DEP:
                c1_lower = time_coords[last_nine_idx]
                c1_upper = np.inf
            else:
                c1_lower = c1_upper = None
        elif last_nine_idx is None and first_non_nine_idx is not None:
            # left-censored: the very first reading we have already shows
            # non-9. Mathematically legitimate evidence (see prior
            # discussion), but lifelines' fit_interval_censoring produces
            # confirmed-broken (non-monotonic) output for left-censored data
            # - a real library bug/limitation, not a data issue on our end.
            # Dropping these for now until that's resolved (icenReg test,
            # or a different tool) rather than feed the fit known-bad input.
            c1_lower = c1_upper = None
        else:
            c1_lower = c1_upper = None

        # --- C2: hits 0 ---
        last_nonzero_idx = None
        first_zero_idx = None
        for i, v in enumerate(vals):
            if v > 0:
                last_nonzero_idx = i
            elif first_zero_idx is None:
                first_zero_idx = i
        if last_nonzero_idx is not None and first_zero_idx is not None and first_zero_idx > last_nonzero_idx:
            c2_lower = time_coords[last_nonzero_idx]
            c2_upper = time_coords[first_zero_idx]
        elif last_nonzero_idx is not None and first_zero_idx is None:
            c2_lower = time_coords[last_nonzero_idx]
            c2_upper = np.inf
        else:
            c2_lower = c2_upper = None

        if c1_lower is not None:
            c1_brackets.append((key[0], c1_lower, c1_upper))

        # gap bracket only derivable when BOTH C1 and C2 are at least
        # partially bracketed for this instance
        if c1_lower is not None and c2_lower is not None:
            gap_lower = c2_lower - c1_upper if np.isfinite(c1_upper) else np.nan
            if c1_lower <= NEGATIVE_INFINITY_STANDIN:
                # c1_lower is a finite stand-in for -inf (left-censored C1) -
                # treat the gap's upper bound as genuinely unbounded rather
                # than doing arithmetic against the stand-in's arbitrary
                # magnitude, which would produce a technically-correct but
                # misleading huge finite number if anyone inspects it directly.
                gap_upper = np.inf
            else:
                gap_upper = (c2_upper - c1_lower) if np.isfinite(c2_upper) else np.inf
            if not np.isnan(gap_lower) and gap_lower >= 0:
                c2_brackets.append((key[0], gap_lower, gap_upper))

    return c1_brackets, c2_brackets


def fit_turnbull_grid(brackets, resolution=0.05, max_iter=500, tol=1e-8):
    """Fits a nonparametric interval-censored (Turnbull-style) population
    distribution directly, as a fine-grained probability mass distribution
    over time via an EM/self-consistency algorithm - rather than delegating
    to lifelines' fit_interval_censoring, which was confirmed (this session)
    to produce non-monotonic, invalid survival curves on ordinary real data
    (see decline-curve-handoff discussion; reproduced on both synthetic and
    real service data, independent of left-censoring).

    This implementation is structurally guaranteed correct in a way that
    matters here: it builds a genuine normalized probability distribution
    bin-by-bin, so its cumulative sum CANNOT be non-monotonic - there is no
    code path that could reproduce the lifelines failure, not just "tested
    and seemed fine." Verified via: (1) a synthetic case where every bracket
    is the same tight interval - median correctly concentrates there; (2) the
    exact real data that broke lifelines - now fits cleanly with a monotonic
    result.

    brackets: list of (serviceId, lower, upper) tuples, same shape as what
    extract_brackets returns. upper may be np.inf for right-censored - this
    gets capped at 0.0 (departure) as the grid's upper bound, which is the
    real physical constraint (C1 cannot happen after departure), not an
    approximation.

    Returns (centers, p) - centers is the grid's bin-center array, p is the
    fitted probability mass per bin (sums to 1, all non-negative).
    """
    lowers = np.array([b[1] for b in brackets], dtype=float)
    uppers = np.array([0.0 if not np.isfinite(b[2]) else b[2] for b in brackets], dtype=float)

    grid_min = lowers.min()
    grid_max = max(uppers.max(), 0.0)
    n_bins = max(int(np.ceil((grid_max - grid_min) / resolution)), 1)
    edges = np.linspace(grid_min, grid_max, n_bins + 1)
    centers = (edges[:-1] + edges[1:]) / 2

    indicator = (centers[None, :] >= lowers[:, None]) & (centers[None, :] <= uppers[:, None])
    keep = indicator.sum(axis=1) > 0
    indicator = indicator[keep]
    n = indicator.shape[0]
    if n == 0:
        return centers, np.zeros_like(centers)

    p = np.full(centers.shape[0], 1.0 / centers.shape[0])
    for _ in range(max_iter):
        S = indicator @ p
        S[S == 0] = 1e-300
        p_new = ((indicator / S[:, None]) * p[None, :]).sum(axis=0) / n
        if np.max(np.abs(p_new - p)) < tol:
            p = p_new
            break
        p = p_new

    assert np.all(p >= -1e-9), "negative probability mass produced - real bug, do not trust this fit"
    assert abs(p.sum() - 1.0) < 1e-6, "distribution does not sum to 1 - real bug, do not trust this fit"
    cdf = np.cumsum(p)
    assert np.all(np.diff(cdf) >= -1e-9), "non-monotonic CDF produced - real bug, do not trust this fit"

    return centers, p


def summarize_grid_fit(brackets):
    """Reports median (when resolvable) and the right-censored fraction,
    same spirit as the earlier lifelines-based summarize_fit."""
    n = len(brackets)
    if n < 5:
        return f"insufficient data (n={n})"
    n_right_censored = sum(1 for b in brackets if not np.isfinite(b[2]))
    pct_rc = n_right_censored / n * 100

    centers, p = fit_turnbull_grid(brackets)
    if p.sum() == 0:
        return f"fit failed (n={n})"
    cdf = np.cumsum(p)
    idx = np.searchsorted(cdf, 0.5)
    if idx >= len(centers):
        return f"UNRESOLVED - {pct_rc:.0f}% of n={n} right-censored, no median crossing"
    median_val = centers[idx]
    return f"median={median_val:.2f}h (n={n}, {pct_rc:.0f}% right-censored)"


def main():
    conn = sqlite3.connect(DB_PATH)

    dep_time_to_service, service_info = build_service_map(conn)
    print(f"Built {len(service_info)} services from flightSchedule departure-time clustering.")

    matched_rows, unmatched_count = load_observations_with_schedule(conn)
    print(f"Matched {len(matched_rows)} avail-type observations to flightSchedule "
          f"({unmatched_count} unmatched/dropped).")
    print()

    for cabin in ["y", "cPlus", "firstOrPS", "d1"]:
        print(f"=== Cabin: {cabin} ===")
        c1_brackets, gap_brackets = extract_brackets(matched_rows, dep_time_to_service, cabin)
        print(f"  Total C1 brackets: {len(c1_brackets)}   Total gap brackets: {len(gap_brackets)}")

        # per-service breakdown, only for services with enough data to bother
        by_service_c1 = defaultdict(list)
        by_service_gap = defaultdict(list)
        for b in c1_brackets:
            by_service_c1[b[0]].append(b)
        for b in gap_brackets:
            by_service_gap[b[0]].append(b)

        reportable_services = sorted(
            set(by_service_c1) | set(by_service_gap),
            key=lambda sid: -len(by_service_c1.get(sid, []))
        )

        shown = 0
        for sid in reportable_services:
            c1_for_service = by_service_c1.get(sid, [])
            gap_for_service = by_service_gap.get(sid, [])
            if len(c1_for_service) < 5:
                continue
            org, dest, rep_time, n_days = service_info[sid]
            c1_summary = summarize_grid_fit(c1_for_service)
            gap_summary = summarize_grid_fit(gap_for_service) if gap_for_service else "insufficient data (n=0)"
            hh, mm = divmod(rep_time, 60)
            print(f"  service {sid} ({org}-{dest} ~{hh:02d}{mm:02d}):")
            print(f"    C1:  {c1_summary}")
            print(f"    gap: {gap_summary}")
            shown += 1
            if shown >= 10:
                print(f"  ... ({len(reportable_services) - shown} more services with >=5 C1 brackets not shown)")
                break
        print()


if __name__ == "__main__":
    main()
