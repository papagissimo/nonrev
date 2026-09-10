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
  reading is CORRECTED IN PLACE - not removed - by its rounded-to-the-
  nearest-integer-seat residual, and the instance is refit with the
  corrected value. Repeats until the fit is clean or the iteration cap
  (settings-driven, see settings.py's declineCurveSettings /
  PoolingSettingsDialog) is hit. The corrected value is fed back
  unclamped - a correction that implies 10 seats or -3 is real signal
  (what the reading would have been without the step change), not an
  error to bound; scipy's least-squares handles an out-of-[0,9] target
  correctly on its own, no special-casing needed, and it naturally drops
  out of "interior" on the next pass since 1<=v<=8 no longer holds.
  Corrections to the same point ACCUMULATE across however many passes
  revisit it (the fit can shift under an already-corrected point as
  other corrections land elsewhere) - each point's own step_changes entry
  is the net sum of every correction applied to it, not just the last
  one. Never removes a point - the output set of readings is exactly the
  input set, just with any step changes ironed out; the record of what
  was corrected (and by how much) is itself the useful output for a
  future "how jumpy is this service" stat.

  This correct-in-place version has no structural guarantee of
  convergence the way the old remove-based version did (removing a point
  strictly shrinks the candidate set every pass; correcting one doesn't -
  the same point's value can be nudged by a nonzero amount on a later
  pass even after settling), so the iteration cap is a real requirement
  here, not just a nice-to-have. The historical duplicate-row bug (~9% of
  rows, confirmed via a direct query against his live nonrev.db to be
  fully resolved and pre-server-era - every remaining occurrence predates
  the Flask server's first commit) no longer factors into this cap at
  all; it's a general safety bound now, not a workaround for stale data.
  A pass that finds nothing worth correcting (rounded residual = 0, i.e.
  the worst point is already within half a seat of the curve) stops the
  loop immediately rather than burning iterations unproductively.

  SLOPE POOLING. Per (org, dest, dayOfWeek, time-of-day cluster) SERVICE
  - the EXACT same granularity as C1 (below), no separate cross-day
  grouping. Restricted to instances that had at least one real interior
  reading (post-step-change-correction) - an all-9/all-0 instance's
  fitted slope is unidentifiable (no transition was ever observed, so
  curve_fit picks an arbitrary value near a bound) and would corrupt a
  naive average; confirmed directly against real data (excluding these
  raised plausible-gap groups from 72% to 95%, back when pooling still
  crossed days - see SUPERSEDED below). NO MINIMUM instance count (his
  explicit call) - a service with exactly one qualifying instance just
  uses that instance's own slope directly; the next time that service
  runs, there are two to pool, and so on.

  TWICE SUPERSEDED, in order:
  1) Pooling used to be a plain mean across qualifying instances' own
     independently-fitted slopes (tested against point-count and
     inverse-variance weighting first - inverse-variance was confirmed
     WORSE, since it hands unidentifiable-slope instances outsized
     weight whenever curve_fit happens to report a deceptively tiny
     variance for a boundary-pinned fit; point-count weighting performed
     the same as a plain mean once those instances were excluded).
  2) Replaced with a real optimization (see below) - his call: a plain
     mean (of any kind) optimizes a PROXY, how well each instance fits
     on its own, not the thing that actually matters, how well the
     pooled slope performs in the live estimator's own slide mechanism.
     At this point pooling still crossed days of week, via a separate
     cross-day "physical group" concept (build_slope_group_map, now
     removed entirely) - on the original theory that decline rate is a
     capacity/demand phenomenon independent of day of week.
  3) The cross-day grouping itself was then retired (current state,
     this function) - his call, confirmed against real data: one such
     group showed a 40x spread (0.24 to 9.76 seats/hour) across its 19
     supposedly-pooled instances, clear evidence of genuinely different
     services being wrongly merged by the coarser cross-day grouping,
     not noise. Slope now pools no more broadly than C1 does. His
     explicit reassurance against over-tuning the underlying time
     clustering itself: don't worry about a service's own cluster
     boundary being slightly off (9:30 vs 10:15) - "on game day it's a
     ten o'clock flight plus or minus an hour, we don't care" - what was
     wrong was pooling ACROSS days on top of that clustering, not the
     clustering (SERVICE_GAP_MINUTES, clustering.py) itself.

  The optimization itself (step 2 above, unchanged by step 3 except for
  what counts as "the pool"): for a candidate slope, every qualifying
  instance in the service's own pool contributes one squared-error term
  - bracket that instance's own (step-change-corrected) readings at T-4
  hours (his real workflow checkpoint, not an arbitrary number - same
  bracket-nearest-target selection GraphObservations.compute_trajectory
  already uses), slide the curve through each bracketing reading at the
  candidate slope, predict forward to T-1 (the live estimator's own
  target), interpolate the two resulting PREDICTIONS (not the raw
  readings - his explicit call, since the two aren't quite identical
  near a 0/9 clamp and he wants whatever the live estimator would
  actually have shown), compare against the real reading nearest T-1.
  Summed across every instance with usable data on both sides, minimized
  via scipy.optimize.minimize_scalar, bounded to [min, max] of the
  service's own per-instance fitted slopes rather than curve_fit's
  generic physical bound. An optimum landing on either edge means the
  FIT itself is suspect (grouping is no longer a possible cause, now
  that pooling never crosses a service boundary) and falls back to the
  plain mean for that service. See
  pool_slope/bracket_with_weight/predict_t1_via_slide.

  C1 pooling (below) is entirely UNTOUCHED by any of this - it's the
  cold-start seed before any real reading exists, overridden the instant
  one comes in, so optimizing it for predictive accuracy wouldn't change
  anything (his own reasoning, confirmed rather than assumed).

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

Coefficient PERSISTENCE: refresh_decline_curve_coefficients() writes the
pooled per-(org,dest,dayOfWeek,depTime,cabin) results to the
declineCurveCoefficients table, same recompute-from-scratch/manual-
launcher-button/startup-refresh pattern as FloorEstimates.py. Keyed by
every individual observed depTime (not a cluster-representative time),
mirroring dep_time_to_service/dep_time_to_group directly - a read-time
consumer does an exact-match lookup by (org, dest, dayOfWeek, depTime,
cabin), no live reclustering needed to use these numbers.

UNITS: slope is stored and used as seats/hour throughout, no exceptions
- deliberately not minutes/seat, even though that's a more natural unit
in his head for this particular quantity. Decided against a dual
representation (fit internally in one unit, display/store in another) -
two different unit systems for the same quantity living in the same
codebase is asking for exactly the kind of silent mismatch that's easy
to miss and expensive to debug later.

Run directly for a console report:
    python3 DeclineCurveFit.py
"""

import sqlite3
from collections import defaultdict
from datetime import datetime

import numpy as np
from scipy.optimize import curve_fit, minimize_scalar

from clustering import cluster_services, service_representative
from settings import load_settings, DECLINE_CURVE_SETTINGS_KEY, DEFAULT_DECLINE_CURVE_SETTINGS

DB_PATH = "nonrev.db"

CABIN_COLUMNS = {
    "y": ("y", "cheapY"),
    "cPlus": ("cPlus", "cheapCPlus"),
    "firstOrPS": ("firstOrPS", "cheapFirstOrPS"),
    "d1": ("d1", "cheapD1"),
}


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


def gather_instances(rows, dep_time_to_service, cabin):
    """For one cabin, groups matched observation rows into instances
    keyed by (serviceId, flightDate). Readings are sorted chronologically
    (ascending real time = descending hoursBeforeDep).

    Returns dict: (serviceId, flightDate) -> {"readings":
    [(hoursBeforeDep, value), ...] sorted chronologically}."""
    real_col, cheap_col = CABIN_COLUMNS[cabin]

    instances = defaultdict(lambda: {"readings": []})
    for r in rows:
        val = resolved_cabin_value(r, real_col, cheap_col)
        if val is None:
            continue
        service_id = dep_time_to_service.get((r["org"], r["dest"], r["dayOfWeek"], r["depTime"]))
        if service_id is None:
            continue
        if r["hoursBeforeDep"] is None:
            continue
        try:
            hbd = float(r["hoursBeforeDep"])
        except (ValueError, TypeError):
            continue
        key = (service_id, r["flightDate"])
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


def fit_instance(readings, rmse_threshold, max_iterations):
    """Fits the piecewise model to one flight-day instance via
    scipy.optimize.curve_fit against its FULL set of raw readings (9s and
    0s included), with C1's search range bounded by this instance's own
    corner bracket when it has one. Then hunts for step changes: if the
    fit's residuals on just the interior (1-8) readings are too large,
    the single worst one is CORRECTED IN PLACE (not removed - see module
    docstring) by its rounded residual, and the instance is refit with
    the corrected value. Repeats until the fit is clean, a pass finds
    nothing worth correcting (rounded residual = 0), or max_iterations is
    hit.

    rmse_threshold: seats, stop correcting once interior RMSE is at or
    below this. max_iterations: hard cap on correction passes - required
    here since correcting in place doesn't shrink the candidate set the
    way removal did, so there's no structural convergence guarantee.

    Returns a dict with c1, slope, n_interior (count of interior readings
    in the FINAL fit - 0 means this instance's slope is unidentifiable,
    see module docstring), n_points (unchanged throughout - nothing is
    ever removed), and step_changes (a list of (hoursBeforeDep,
    original_observed_value, net_correction) - one entry per point that
    received at least one nonzero correction, net_correction being the
    SUM of every correction applied to it across however many passes
    revisited it, each already an integer seat count) - or None if
    there's nothing fittable at all (fewer than 2 readings, or every
    reading at the same hoursBeforeDep)."""
    if len(readings) < 2:
        return None

    hbds = [h for h, v in readings]
    originals = [v for h, v in readings]
    pts = list(readings)  # values mutate in place; never shrinks
    corrections = defaultdict(int)

    def build_result(c1, slope, n_interior):
        step_changes = [
            (hbds[i], originals[i], corrections[i])
            for i in sorted(corrections) if corrections[i] != 0
        ]
        return {"c1": float(c1), "slope": float(slope), "n_interior": n_interior,
                "n_points": len(pts), "step_changes": step_changes,
                "corrected_readings": list(pts)}

    iterations = 0
    while True:
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
            return build_result(c1, slope, 0)

        residuals = [ys[i] - piecewise_model(xs[i], c1, slope) for i in interior_idx]
        rmse = float(np.sqrt(np.mean(np.square(residuals))))
        if rmse <= rmse_threshold or iterations >= max_iterations:
            return build_result(c1, slope, len(interior_idx))

        worst_local = int(np.argmax(np.abs(residuals)))
        worst_i = interior_idx[worst_local]
        rounded = round(residuals[worst_local])
        if rounded == 0:
            # Worst point is already within half a seat of the curve -
            # nothing left worth correcting. Stop rather than burn
            # iterations relabeling the same near-zero residual.
            return build_result(c1, slope, len(interior_idx))

        corrections[worst_i] += rounded
        h, v = pts[worst_i]
        pts[worst_i] = (h, v - rounded)  # unclamped - see module docstring
        iterations += 1


# Target hours for the slope-pooling objective (see pool_slope docstring)
# - deliberately his real operational T-4/T-1 checkpoints, not arbitrary
# numbers: T-1 is the same live-estimator target used everywhere else in
# this project (GraphObservations.T1_TARGET_HOURS), T-4 is the point his
# own workflow already tries to get a reading near before committing to
# game day.
T4_TARGET_HOURS_FOR_POOLING = 4.0
T1_TARGET_HOURS_FOR_POOLING = 1.0


def bracket_with_weight(readings, target_hours):
    """Same bracket-nearest-target reading selection as
    GraphObservations.compute_trajectory (straddle the target with the
    nearest reading on each side when a real straddle exists; otherwise
    the two nearest readings on whichever side has everything) - kept as
    its own copy here rather than imported, since this operates on this
    module's plain (hbd, value) tuples, not the dict shape
    GraphObservations uses, and pooling shouldn't need to import the
    graphing module.

    Returns None if readings is empty. ('single', reading) if only one
    reading is available on its own (exactly one reading total, or one
    side is empty with fewer than two candidates there). Otherwise
    ('pair', b, a, weight) where b/a are the two bracketing (or
    two-nearest-same-side) readings and weight = (b[0] - target_hours) /
    (b[0] - a[0]) - the exact interpolation weight compute_trajectory
    itself uses, handed back so a caller can apply it to whatever it
    computed FROM b and a (predictions, here - not the raw readings
    themselves)."""
    if not readings:
        return None
    if len(readings) == 1:
        return ("single", readings[0])

    before = [r for r in readings if r[0] >= target_hours]
    after = [r for r in readings if r[0] <= target_hours]

    def nearest_of(lst, want_min):
        best = None
        for cur in lst:
            if best is None:
                best = cur
            elif want_min and cur[0] < best[0]:
                best = cur
            elif not want_min and cur[0] > best[0]:
                best = cur
        return best

    def two_nearest(lst):
        ordered = sorted(lst, key=lambda r: abs(r[0] - target_hours))
        return ordered[0], ordered[1]

    if before and after:
        b = nearest_of(before, True)
        a = nearest_of(after, False)
    elif before:
        if len(before) < 2:
            return ("single", before[0])
        b, a = two_nearest(before)
    else:
        if len(after) < 2:
            return ("single", after[0])
        b, a = two_nearest(after)

    if b[0] == a[0]:
        return ("single", b)
    weight = (b[0] - target_hours) / (b[0] - a[0])
    return ("pair", b, a, weight)


def nearest_reading(readings, target_hours):
    """The single real reading closest to target_hours - used as ground
    truth (his term: "the latest T-1 measurement"), not a bracket
    interpolation - he wants the actual observed value nearest T-1, not
    a synthesized one."""
    if not readings:
        return None
    return min(readings, key=lambda r: abs(r[0] - target_hours))


def predict_t1_via_slide(readings, slope, t4_hours, t1_hours):
    """What the live sliding estimator would predict at t1_hours, using
    only readings available at/around t4_hours and the given candidate
    slope - the core of the slope-pooling objective (see pool_slope).
    Brackets readings at t4_hours; for each bracketing reading, slides
    the curve through it (solves for the C1 that makes the frozen curve
    at this slope pass through that reading) and predicts forward to
    t1_hours; if there were two bracketing readings, interpolates the
    two resulting T1 PREDICTIONS using the bracket's own distance
    weighting (his explicit call - not interpolating the readings before
    sliding; the two aren't quite identical near a 0/9 clamp, and he
    wants the version that matches what the live estimator would
    actually have shown at each moment). Returns None if readings is
    empty (nothing to anchor on this side)."""
    bracket = bracket_with_weight(readings, t4_hours)
    if bracket is None:
        return None

    def slide_predict(reading):
        hbd_r, val_r = reading
        c1_prime = hbd_r + (9.0 - val_r) / slope
        return float(piecewise_model(t1_hours, c1_prime, slope))

    if bracket[0] == "single":
        return slide_predict(bracket[1])

    _, b, a, weight = bracket
    b_pred = slide_predict(b)
    a_pred = slide_predict(a)
    return b_pred + (a_pred - b_pred) * weight


def pool_slope(fits_by_instance):
    """Pooled per-(day-of-week-specific) SERVICE slope via direct
    optimization against real predictive accuracy - his call, replacing
    an earlier plain-mean-of-per-instance-slopes approach, which
    optimized a proxy (per-instance fit quality) rather than the thing
    that actually matters (how well the pooled slope predicts, via the
    live estimator's own slide mechanism, what a real T-1 reading turns
    out to be).

    SUPERSEDED: slope used to be pooled across a coarser cross-day
    "physical group" (build_slope_group_map, now removed), on the theory
    that decline rate is a capacity/demand phenomenon independent of day
    of week. His call to retire that entirely, confirmed against real
    data: one such group showed a 40x spread (0.24 to 9.76 seats/hour)
    across its 19 supposedly-pooled instances - clear evidence of
    genuinely different services being merged by the coarser grouping,
    not noise. Slope is now pooled at EXACTLY the same granularity as C1
    (see pool_c1) - per day-of-week-specific service, using only that
    service's own instances, nothing pooled across days or routes. His
    explicit reassurance: don't worry about a service's own time-cluster
    boundary being slightly off (9:30 vs 10:15) - "on game day it's a
    ten o'clock flight plus or minus an hour, we don't care" - the
    SERVICE_GAP_MINUTES clustering that already defines a service (see
    clustering.py / build_service_map) is fine as-is; what was wrong was
    pooling ACROSS days on top of that, not the clustering itself.

    NO MINIMUM instance count (his explicit call) - a service with
    exactly one qualifying instance just uses that instance's own
    already-fitted slope directly, however noisy; there's no "not enough
    data, fall back to the two-point method" threshold anywhere in this
    function. The next time that service runs, there are two instances
    to pool: this genuinely improves over time rather than needing a
    minimum bar up front.

    For a candidate slope, each qualifying instance contributes one
    squared-error term via predict_t1_via_slide (see that function) -
    bracket at T-4, slide-and-predict to T-1 from each bracketing
    reading (using this instance's own step-change-CORRECTED readings,
    the same data its own curve_fit used), compare against
    nearest_reading's real value at T-1. Summed across every qualifying
    instance, minimized via scipy.optimize.minimize_scalar bounded to
    [min, max] of that SERVICE's own already-fitted per-instance slopes
    (not curve_fit's generic physical bound) - his call: an optimum
    landing on either edge means the fit itself is suspect (grouping is
    no longer a possible cause, now that pooling never crosses a service
    boundary), so that case falls back to the plain mean instead of
    trusting a rail-slammed answer.

    Two different eligibility bars, both his explicit calls:
    - n_interior >= 1 (an identifiable per-instance slope exists at
      all) qualifies an instance for the min/max bound AND for the
      plain-mean fallback - same bar as before this change.
    - Actually contributing a term to the optimization's sum needs
      MORE than that: a usable T-4 bracket (>=1 reading) and a real
      reading to serve as T-1 ground truth. Nothing hardcodes how
      close "close enough" is on either side - an instance's own
      readings either support this or they don't (his explicit call:
      let a thin day fail to resolve rather than paper over it with an
      arbitrary tolerance; it isn't a permanent problem, just this
      run's).

    Returns dict serviceId -> (slope, gap_hours, n_instances) - same
    shape as the old groupId-keyed version, just keyed by service now;
    n_instances counts the bound/fallback-eligible instances (matching
    what this return value has always meant here), not the narrower
    optimization-eligible subset."""
    by_service = defaultdict(list)
    for (service_id, flight_date), fit in fits_by_instance.items():
        if fit is None or fit["n_interior"] < 1:
            continue
        by_service[service_id].append(fit)

    pooled = {}
    for sid, fits in by_service.items():
        per_instance_slopes = [f["slope"] for f in fits]
        slope_lo, slope_hi = min(per_instance_slopes), max(per_instance_slopes)
        fallback_slope = float(np.mean(per_instance_slopes))

        if slope_lo >= slope_hi:
            # Every instance in this group agrees exactly (or there's
            # only one) - nothing to optimize, use it directly.
            slope = slope_lo
        else:
            scoring_fits = [
                f for f in fits
                if bracket_with_weight(f["corrected_readings"], T4_TARGET_HOURS_FOR_POOLING) is not None
                and nearest_reading(f["corrected_readings"], T1_TARGET_HOURS_FOR_POOLING) is not None
            ]
            if not scoring_fits:
                # Nothing usable to actually test a candidate slope
                # against - fall back rather than optimize an empty sum.
                slope = fallback_slope
            else:
                def objective(candidate_slope, _fits=scoring_fits):
                    total = 0.0
                    for f in _fits:
                        readings = f["corrected_readings"]
                        pred = predict_t1_via_slide(
                            readings, candidate_slope,
                            T4_TARGET_HOURS_FOR_POOLING, T1_TARGET_HOURS_FOR_POOLING,
                        )
                        truth = nearest_reading(readings, T1_TARGET_HOURS_FOR_POOLING)
                        total += (pred - truth[1]) ** 2
                    return total

                res = minimize_scalar(objective, bounds=(slope_lo, slope_hi), method="bounded")
                slope = float(res.x)
                # Rail check (see docstring) - a small relative
                # tolerance, not exact equality, since the bounded
                # optimizer can land a hair off the true edge.
                edge_tol = (slope_hi - slope_lo) * 1e-6
                if slope <= slope_lo + edge_tol or slope >= slope_hi - edge_tol:
                    slope = fallback_slope

        if slope <= 0:
            continue
        pooled[sid] = (slope, 9.0 / slope, len(fits))
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


def compute_all_fits(conn, rmse_threshold, max_iterations):
    """Shared fitting work for all four cabins - the expensive part
    (curve_fit per instance), factored out so both the console report
    (main()) and the persistence step (refresh_decline_curve_coefficients)
    run it exactly once rather than twice.

    Returns a dict: dep_time_to_service, service_info, and by_cabin:
    {cabin: {instances, fits_by_instance, service_slopes, c1_by_service}}."""
    matched_rows, dropped_count = load_observations(conn)
    dep_time_to_service, service_info = build_service_map(matched_rows)

    by_cabin = {}
    for cabin in ["y", "cPlus", "firstOrPS", "d1"]:
        instances = gather_instances(matched_rows, dep_time_to_service, cabin)
        fits_by_instance = {
            key: fit_instance(inst["readings"], rmse_threshold, max_iterations)
            for key, inst in instances.items()
        }
        by_cabin[cabin] = {
            "instances": instances,
            "fits_by_instance": fits_by_instance,
            "service_slopes": pool_slope(fits_by_instance),
            "c1_by_service": pool_c1(fits_by_instance),
        }

    return {
        "dropped_count": dropped_count,
        "n_rows": len(matched_rows),
        "dep_time_to_service": dep_time_to_service,
        "service_info": service_info,
        "by_cabin": by_cabin,
    }


def refresh_decline_curve_coefficients(conn):
    """Recomputes declineCurveCoefficients from scratch (deletes and
    rewrites the whole table, never incremental - same
    recompute-from-scratch/manual-launcher-button/startup-refresh pattern
    as FloorEstimates.refresh_floor_estimates). One row per (org, dest,
    dayOfWeek, depTime, cabin) actually observed in the data - keyed by
    every individual depTime seen, not a cluster-representative time, so
    a read-time consumer does a plain exact-match lookup with no live
    reclustering needed. c1 AND slope both come from that depTime's own
    (day-of-week-specific) service now - no separate cross-day grouping
    for slope anymore (see pool_slope's docstring for why that was
    retired). A cabin/depTime combination with no resolvable c1 or no
    resolvable slope simply gets a NULL in that column - a consumer's
    lookup miss/NULL means "no fit available yet", not zero.

    Returns {rowsWritten, byCabin: {cabin: {nInstances, nFit,
    nInstancesWithStepChanges, nStepChangesTotal, nServicesWithSlope,
    nServicesResolved, nServicesTotal}}, raw: the full compute_all_fits()
    result, for callers (like main()'s console report) that want the
    underlying per-service numbers without re-running the fit}."""
    settings = load_settings(conn, key=DECLINE_CURVE_SETTINGS_KEY, defaults=DEFAULT_DECLINE_CURVE_SETTINGS)
    result = compute_all_fits(conn, settings["stepChangeRmseThreshold"], settings["stepChangeMaxIterations"])

    conn.execute("DELETE FROM declineCurveCoefficients")

    rows_written = 0
    summary_by_cabin = {}
    for cabin, cabin_data in result["by_cabin"].items():
        fits_by_instance = cabin_data["fits_by_instance"]
        service_slopes = cabin_data["service_slopes"]
        c1_by_service = cabin_data["c1_by_service"]

        for (org, dest, dow, dep_time), sid in result["dep_time_to_service"].items():
            c1_entry = c1_by_service.get(sid)
            slope_entry = service_slopes.get(sid)
            if c1_entry is None and slope_entry is None:
                continue
            c1_val, n_c1 = c1_entry if c1_entry else (None, 0)
            slope_val, _gap_val, n_slope = slope_entry if slope_entry else (None, None, 0)
            conn.execute(
                """INSERT INTO declineCurveCoefficients
                   (org, dest, dayOfWeek, depTime, cabin, c1Hours, slopeSeatsPerHour,
                    nInstancesC1, nInstancesSlope)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (org, dest, dow, dep_time, cabin, c1_val, slope_val, n_c1, n_slope),
            )
            rows_written += 1

        n_fit = sum(1 for f in fits_by_instance.values() if f is not None)
        n_step_changes = sum(len(f["step_changes"]) for f in fits_by_instance.values() if f)
        n_with_step_changes = sum(1 for f in fits_by_instance.values() if f and f["step_changes"])
        summary_by_cabin[cabin] = {
            "nInstances": len(cabin_data["instances"]),
            "nFit": n_fit,
            "nInstancesWithStepChanges": n_with_step_changes,
            "nStepChangesTotal": n_step_changes,
            "nServicesWithSlope": len(service_slopes),
            "nServicesResolved": len(c1_by_service),
            "nServicesTotal": len(result["service_info"]),
        }

    conn.commit()
    return {"rowsWritten": rows_written, "byCabin": summary_by_cabin, "raw": result}


def main():
    conn = sqlite3.connect(DB_PATH)

    refreshed = refresh_decline_curve_coefficients(conn)
    result = refreshed["raw"]
    service_info = result["service_info"]

    print(f"Loaded {result['n_rows']} avail-type observations "
          f"({result['dropped_count']} dropped - no depTime or unparsable flightDate).")
    print(f"Built {len(service_info)} day-of-week-specific services (C1 and slope both pooled at this "
          f"granularity now - no separate cross-day grouping).")
    print(f"Wrote {refreshed['rowsWritten']} rows to declineCurveCoefficients.")
    print()

    for cabin, cabin_data in result["by_cabin"].items():
        summary = refreshed["byCabin"][cabin]
        service_slopes = cabin_data["service_slopes"]
        c1_by_service = cabin_data["c1_by_service"]

        print(f"=== Cabin: {cabin} ===")
        print(f"  {summary['nInstances']} flight-day instances, {summary['nFit']} fit successfully.")
        print(f"  {summary['nInstancesWithStepChanges']} instances had at least one step change corrected "
              f"({summary['nStepChangesTotal']} total corrections applied).")
        print(f"  {summary['nServicesWithSlope']}/{summary['nServicesTotal']} services got a resolvable slope, "
              f"{summary['nServicesResolved']}/{summary['nServicesTotal']} services got a resolvable C1.")

        ranked = sorted(c1_by_service.items(), key=lambda kv: -kv[1][1])
        shown = 0
        for sid, (median_c1, n_instances) in ranked:
            if sid not in service_slopes:
                continue
            slope, gap, n_slope_instances = service_slopes[sid]
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
