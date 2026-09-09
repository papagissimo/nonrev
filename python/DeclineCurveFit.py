"""
DeclineCurveFit.py

Fits a per-(service, cabin) two-coefficient model - a shared decline slope
and a single C1 (leaves-9 corner) timing - meant to seed the live sliding
T-1 estimator, not to describe the true shape of a decline. Real declines
are lumpy (a group booking or cancellation is a genuine step change, not
noise around a curve); this deliberately stays a "gross, gross
approximation" - the live estimator's job is to slide this frozen line to
match whatever's actually been observed today, this script's only job is
to produce the two frozen numbers it slides.

Design, worked out over many rounds of real back-and-forth, several dead
ends included:

  PER-INSTANCE FIT. Rather than hand-rolling separate cases for "has
  interior readings" / "all-9 all day" / "all-0 all day" / "corner jump,
  no interior" (an earlier, messier version of this file did exactly
  that), each flight-day instance is fit directly against a Python
  function shaped like the model itself - flat at 9, linear through the
  decline, flat at 0 - via scipy.optimize.curve_fit, using EVERY raw
  reading that day (9s and 0s included, not just the 1-8 interior subset).
  This handles every case uniformly: an all-9 instance's fit is
  automatically constrained by its own 9-readings, no special-casing
  needed. C1's search range is bounded by that instance's own observed
  corner bracket (last-known-9 -> first-known-non-9) when it has one -
  keeps the optimizer away from the flat-gradient regions that would
  otherwise risk bad convergence.

  STEP-CHANGE DETECTION (his idea, and it works): after fitting, check the
  residuals of ONLY the interior (1-8) readings - 9s/0s should already sit
  exactly on the clamped fit, so they're not checked. If the RMSE of those
  residuals is above threshold, the single worst-residual interior
  reading is set aside as a detected step change (a real booking/
  cancellation jump, not noise) and the instance is refit without it,
  repeating until the fit is clean. Capped at half of that instance's own
  interior-reading count - confirmed against real data that an instance
  can occasionally have every reading tripled (a separate, real
  duplicate-row data-quality bug, ~9% of rows, tracked separately - not
  something this script tries to fix), which without a cap turns into a
  runaway removal loop chasing duplicate copies of the same real outlier
  one at a time. Past the cap, the fit is used as-is rather than
  discarded - an imperfect frozen curve beats none, matching this
  project's whole "gross, gross approximation" design stance.

  SLOPE POOLING. Per (org, dest, time-of-day cluster) PHYSICAL GROUP -
  pooled ACROSS days of week, since decline rate is a capacity/demand-mix
  phenomenon, not something that should vary just because it's a Tuesday.
  Restricted to instances that had at least one real interior reading
  (post-step-change-removal) - an all-9/all-0 instance's fitted slope is
  unidentifiable (no transition was ever observed, so curve_fit picks an
  arbitrary value near a bound) and would corrupt a naive average;
  confirmed directly against real data (excluding these raised plausible-
  gap groups from 72% to 95%). Plain mean across qualifying instances -
  tested against point-count and inverse-variance weighting; inverse-
  variance was confirmed WORSE (it hands the unidentifiable-slope
  instances outsized weight whenever curve_fit happens to report a
  deceptively tiny variance for a boundary-pinned fit), and point-count
  weighting performed the same as a plain mean once the unidentifiable
  instances were excluded, so plain mean is used for simplicity.

  GAP is not independently fit - once slope is known, gap = 9 / slope
  falls straight out (the time a 9-seat cabin takes to cross at that
  rate). Not a separate coefficient.

  C1 POOLING. Stays day-of-week-specific (a demand/behavior pattern,
  unlike slope) - reuses the existing service map. Per service, the
  MEDIAN of every one of its instances' own fitted C1 (including the
  all-9/all-0 instances - their C1 is well-constrained by the bracket
  even without a real transition, unlike their slope). His gut call:
  C1 varies wildly/randomly across instances, so a median is the
  reasonable frozen summary - and it's the parameter that matters least
  anyway, since the live estimator overrides it the instant a real
  sub-9 reading comes in.

Coefficient PERSISTENCE (a table someone can actually query, vs. this
script's console report) remains explicitly out of scope - still an open
design question from an earlier session, not decided here.

Run directly for a console report:
    python3 DeclineCurveFit.py
"""

import sqlite3
from collections import defaultdict
from datetime import datetime

import numpy as np
from scipy.optimize import curve_fit

from clustering import cluster_services, service_representative

DB_PATH = "nonrev.db"

CABIN_COLUMNS = {
    "y": ("y", "cheapY"),
    "cPlus": ("cPlus", "cheapCPlus"),
    "firstOrPS": ("firstOrPS", "cheapFirstOrPS"),
    "d1": ("d1", "cheapD1"),
}

# How well the fitted curve has to match an instance's own interior (1-8)
# readings before we stop hunting for step changes. In seats - a gross,
# round number, not derived from anything; tune by eye once real reports
# come out of this.
STEP_CHANGE_RMSE_THRESHOLD = 0.75

# Never remove more than this fraction of an instance's own interior
# readings hunting for step changes - confirmed necessary against real
# data (a duplicate-row data bug, tracked separately, can otherwise turn
# into a runaway loop chasing 3 copies of the same real outlier one at a
# time). Past the cap, the last fit found is used as-is rather than
# discarding the instance outright.
MAX_STEP_CHANGE_REMOVAL_FRACTION = 0.5


def load_observations(conn):
    """Reads avail-type observations directly - no flightSchedule join.
    depTime lives on the observation row itself now. Returns (rows,
    dropped_count): rows with an unparsable flightDate or a null depTime
    (a handful of legacy rows predating the depTime column, or an
    unconfirmed-airport gap at logging time - same class GraphObservations
    already drops for the same reason) are excluded and counted."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT observationId, carrier, org, dest, flightDate,
               checkTimestamp, hoursBeforeDep, depTime, readingType,
               y, cPlus, firstOrPS, d1,
               cheapY, cheapCPlus, cheapFirstOrPS, cheapD1
        FROM observations
        WHERE readingType = 'avail'
        """
    )
    obs_rows = cur.fetchall()
    col_names = [d[0] for d in cur.description]

    kept = []
    dropped_count = 0
    for row in obs_rows:
        r = dict(zip(col_names, row))
        if r["depTime"] is None:
            dropped_count += 1
            continue
        try:
            r["dayOfWeek"] = datetime.strptime(r["flightDate"], "%Y-%m-%d").strftime("%a")
        except ValueError:
            dropped_count += 1
            continue
        kept.append(r)

    return kept, dropped_count


def build_service_map(rows):
    """Clusters each (org, dest, dayOfWeek)'s observed depTimes into
    services via the shared gap-based clustering (clustering.py) - day-
    of-week-specific, per the later-settled service definition (NOT
    pooled across all 7 days the way ServiceGrouping.get_route_services
    is, for its different purpose of open/full pooling). Used for C1,
    which is a demand/behavior pattern and genuinely does vary by day of
    week (e.g. the Tuesday-after-a-long-weekend case).

    Returns dict: (org, dest, dayOfWeek, depTime) -> serviceId (int), plus
    a dict serviceId -> (org, dest, dayOfWeek, representative_depTime,
    cluster_size) for reporting."""
    by_group = defaultdict(set)
    for r in rows:
        by_group[(r["org"], r["dest"], r["dayOfWeek"])].add(r["depTime"])

    dep_time_to_service = {}
    service_info = {}
    next_service_id = 1

    for (org, dest, dow), times in by_group.items():
        clusters = cluster_services(times)
        for cluster in clusters:
            service_id = next_service_id
            next_service_id += 1
            rep_time = service_representative(cluster)
            service_info[service_id] = (org, dest, dow, rep_time, len(cluster))
            for t in cluster:
                dep_time_to_service[(org, dest, dow, t)] = service_id

    return dep_time_to_service, service_info


def build_slope_group_map(rows):
    """Clusters each (org, dest)'s observed depTimes into "physical
    groups" POOLED ACROSS ALL 7 DAYS - deliberately coarser than
    build_service_map's day-of-week-specific services. Slope is a
    capacity/decline-rate phenomenon, not a demand-pattern one - no
    reason it should be split by day of week, and pooling across days
    gives it roughly 7x the instances to draw on.

    Returns dict: (org, dest, depTime) -> groupId (int), plus a dict
    groupId -> (org, dest, representative_depTime, cluster_size)."""
    by_route = defaultdict(set)
    for r in rows:
        by_route[(r["org"], r["dest"])].add(r["depTime"])

    dep_time_to_group = {}
    group_info = {}
    next_group_id = 1

    for (org, dest), times in by_route.items():
        clusters = cluster_services(times)
        for cluster in clusters:
            group_id = next_group_id
            next_group_id += 1
            rep_time = service_representative(cluster)
            group_info[group_id] = (org, dest, rep_time, len(cluster))
            for t in cluster:
                dep_time_to_group[(org, dest, t)] = group_id

    return dep_time_to_group, group_info


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


def gather_instances(rows, dep_time_to_service, dep_time_to_group, cabin):
    """For one cabin, groups matched observation rows into instances
    keyed by (serviceId, flightDate). Each instance also carries its own
    groupId (the cross-day slope group its depTime falls into - normally
    the same physical group regardless of which day it landed on, since
    both cluster the same underlying depTimes, just pooled differently).
    Readings are sorted chronologically (ascending real time = descending
    hoursBeforeDep).

    Returns dict: (serviceId, flightDate) -> {"group_id": int,
    "readings": [(hoursBeforeDep, value), ...] sorted chronologically}."""
    real_col, cheap_col = CABIN_COLUMNS[cabin]

    instances = defaultdict(lambda: {"group_id": None, "readings": []})
    for r in rows:
        val = resolved_cabin_value(r, real_col, cheap_col)
        if val is None:
            continue
        service_id = dep_time_to_service.get((r["org"], r["dest"], r["dayOfWeek"], r["depTime"]))
        if service_id is None:
            continue
        group_id = dep_time_to_group.get((r["org"], r["dest"], r["depTime"]))
        if group_id is None:
            continue
        if r["hoursBeforeDep"] is None:
            continue
        try:
            hbd = float(r["hoursBeforeDep"])
        except (ValueError, TypeError):
            continue
        key = (service_id, r["flightDate"])
        instances[key]["group_id"] = group_id
        instances[key]["readings"].append((hbd, val))

    for inst in instances.values():
        inst["readings"].sort(key=lambda x: -x[0])  # descending hoursBeforeDep = chronological

    return instances


def piecewise_model(hbd, c1, slope):
    """Flat at 9 until C1, linear decline (rate = slope, seats per hour)
    from there, flat at 0 once it's fully declined. hbd = hoursBeforeDep;
    larger hbd = further from departure."""
    return np.clip(9 - slope * (c1 - hbd), 0, 9)


def find_c1_bracket(readings):
    """Last-known-9 -> first-known-non-9, in hoursBeforeDep terms. None
    if this instance never left 9, or never showed a 9 to begin with."""
    last_nine_hbd = None
    first_non9_hbd = None
    for hbd, v in readings:  # already chronological (descending hbd)
        if v >= 9:
            last_nine_hbd = hbd
        elif first_non9_hbd is None and last_nine_hbd is not None:
            first_non9_hbd = hbd
    if last_nine_hbd is None or first_non9_hbd is None:
        return None
    return (first_non9_hbd, last_nine_hbd)  # (lower, upper)


def fit_instance(readings):
    """Fits the piecewise model to one flight-day instance via
    scipy.optimize.curve_fit against its FULL set of raw readings (9s and
    0s included), with C1's search range bounded by this instance's own
    corner bracket when it has one. Then hunts for step changes: if the
    fit's residuals on just the interior (1-8) readings are too large,
    the single worst one is set aside as a detected step change and the
    instance is refit without it, repeating (capped - see module
    docstring) until the fit is clean or the cap is hit.

    Returns a dict with c1, slope, n_interior (count of interior readings
    actually used in the final fit - 0 means this instance's slope is
    unidentifiable, see module docstring), n_points, and step_changes (a
    list of (hoursBeforeDep, observed_value, magnitude) for every reading
    set aside - magnitude is the fit residual at the time it was flagged,
    rounded to the nearest integer seat since a real booking/cancellation
    moves a whole number of seats; detection and ranking use the
    unrounded residual, only the recorded magnitude rounds) - or None if
    there's nothing fittable at all (fewer than 2 readings, or every
    reading at the same hoursBeforeDep)."""
    pts = list(readings)
    original_n_interior = sum(1 for _, v in pts if 1 <= v <= 8)
    max_removals = max(0, int(original_n_interior * MAX_STEP_CHANGE_REMOVAL_FRACTION))
    step_changes = []

    while True:
        if len(pts) < 2:
            return None
        xs = np.array([h for h, v in pts], dtype=float)
        ys = np.array([v for h, v in pts], dtype=float)
        if np.var(xs) == 0:
            return None

        bracket = find_c1_bracket(pts)
        c1_lo, c1_hi = bracket if bracket else (0.0, xs.max() + 1)
        c1_guess = (c1_lo + c1_hi) / 2
        try:
            popt, _ = curve_fit(
                piecewise_model, xs, ys, p0=[c1_guess, 1.0],
                bounds=([c1_lo, 1e-4], [c1_hi, 50]), maxfev=2000,
            )
        except (RuntimeError, ValueError):
            return None
        c1, slope = popt

        interior_idx = [i for i, (h, v) in enumerate(pts) if 1 <= v <= 8]
        if not interior_idx:
            return {"c1": float(c1), "slope": float(slope), "n_interior": 0,
                    "n_points": len(pts), "step_changes": step_changes}

        residuals = [ys[i] - piecewise_model(xs[i], c1, slope) for i in interior_idx]
        rmse = float(np.sqrt(np.mean(np.square(residuals))))
        if rmse <= STEP_CHANGE_RMSE_THRESHOLD or len(step_changes) >= max_removals:
            return {"c1": float(c1), "slope": float(slope), "n_interior": len(interior_idx),
                    "n_points": len(pts), "step_changes": step_changes}

        worst_local = int(np.argmax(np.abs(residuals)))
        worst_i = interior_idx[worst_local]
        # detection/ranking stays on the raw residual (see docstring); only
        # the RECORDED magnitude rounds to an integer - a real booking or
        # cancellation moves a whole number of seats, so "-1.6" isn't
        # describing anything that could have physically happened, even
        # though the underlying fit residual it's derived from is continuous
        step_changes.append((pts[worst_i][0], pts[worst_i][1], round(residuals[worst_local])))
        pts = pts[:worst_i] + pts[worst_i + 1:]


def pool_slope(fits_by_instance, service_to_group):
    """Plain mean of per-instance fitted slopes, restricted to instances
    with n_interior >= 1 (an instance with zero real interior readings
    has an unidentifiable slope - see module docstring for why this
    matters, confirmed directly against real data). Returns dict
    groupId -> (slope, gap_hours, n_instances_used)."""
    by_group = defaultdict(list)
    for (service_id, flight_date), fit in fits_by_instance.items():
        if fit is None or fit["n_interior"] < 1:
            continue
        gid = service_to_group.get(service_id)
        if gid is None:
            continue
        by_group[gid].append(fit["slope"])

    pooled = {}
    for gid, slopes in by_group.items():
        slope = float(np.mean(slopes))
        if slope <= 0:
            continue
        pooled[gid] = (slope, 9.0 / slope, len(slopes))
    return pooled


def pool_c1(fits_by_instance):
    """Median of every instance's own fitted C1, per (day-of-week-
    specific) service - includes all-9/all-0 instances too, since their
    C1 is well-constrained by the bracket even without a real transition
    (only their slope is unidentifiable). Returns dict serviceId ->
    (median_c1, n_instances)."""
    by_service = defaultdict(list)
    for (service_id, flight_date), fit in fits_by_instance.items():
        if fit is None:
            continue
        by_service[service_id].append(fit["c1"])

    return {sid: (float(np.median(c1s)), len(c1s)) for sid, c1s in by_service.items()}


def main():
    conn = sqlite3.connect(DB_PATH)

    matched_rows, dropped_count = load_observations(conn)
    print(f"Loaded {len(matched_rows)} avail-type observations "
          f"({dropped_count} dropped - no depTime or unparsable flightDate).")

    dep_time_to_service, service_info = build_service_map(matched_rows)
    dep_time_to_group, group_info = build_slope_group_map(matched_rows)
    print(f"Built {len(service_info)} day-of-week-specific services (for C1) "
          f"and {len(group_info)} cross-day physical groups (for slope).")
    print()

    service_members = defaultdict(list)
    for (org, dest, dow, dep_time), sid in dep_time_to_service.items():
        service_members[sid].append((org, dest, dep_time))
    service_to_group = {}
    for sid, members in service_members.items():
        votes = defaultdict(int)
        for org, dest, dep_time in members:
            gid = dep_time_to_group.get((org, dest, dep_time))
            if gid is not None:
                votes[gid] += 1
        if votes:
            service_to_group[sid] = max(votes.items(), key=lambda kv: kv[1])[0]

    for cabin in ["y", "cPlus", "firstOrPS", "d1"]:
        print(f"=== Cabin: {cabin} ===")
        instances = gather_instances(matched_rows, dep_time_to_service, dep_time_to_group, cabin)

        fits_by_instance = {key: fit_instance(inst["readings"]) for key, inst in instances.items()}
        n_fit = sum(1 for f in fits_by_instance.values() if f is not None)
        n_step_changes = sum(len(f["step_changes"]) for f in fits_by_instance.values() if f)
        n_instances_with_step_changes = sum(1 for f in fits_by_instance.values() if f and f["step_changes"])

        group_slopes = pool_slope(fits_by_instance, service_to_group)
        c1_by_service = pool_c1(fits_by_instance)

        print(f"  {len(instances)} flight-day instances, {n_fit} fit successfully.")
        print(f"  {n_instances_with_step_changes} instances had at least one step change detected "
              f"({n_step_changes} total readings set aside as step changes).")
        print(f"  {len(group_slopes)}/{len(group_info)} physical groups got a resolvable slope, "
              f"{len(c1_by_service)}/{len(service_info)} services got a resolvable C1.")

        ranked = sorted(c1_by_service.items(), key=lambda kv: -kv[1][1])
        shown = 0
        for sid, (median_c1, n_instances) in ranked:
            gid = service_to_group.get(sid)
            if gid is None or gid not in group_slopes:
                continue
            slope, gap, n_slope_instances = group_slopes[gid]
            org, dest, dow, rep_time, _ = service_info[sid]
            hh, mm = divmod(rep_time, 60)
            print(f"  service {sid} ({org}-{dest} {dow} ~{hh:02d}{mm:02d}): "
                  f"slope={slope:.2f} seats/h (from {n_slope_instances} instances), "
                  f"gap={gap:.2f}h, C1 median={median_c1:.2f}h (from {n_instances} instances)")
            shown += 1
            if shown >= 10:
                break
        print()


if __name__ == "__main__":
    main()
