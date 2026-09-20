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
  candidate slope, predict forward to THAT INSTANCE'S OWN LAST READING'S
  hour, interpolate the two resulting PREDICTIONS (not the raw readings -
  his explicit call, since the two aren't quite identical near a 0/9
  clamp and he wants whatever the live estimator would actually have
  shown), compare directly against that same last reading. Fixed
  2026-09-17: the target used to be a fixed T-1, scored against
  whichever real reading happened to be nearest T-1 - for a sparse or
  undeparted instance whose data stops well short of T-1, that compared
  a T-1 prediction against a much-earlier actual, corrupting the
  residual regardless of slope quality. Predicting to and scoring
  against the instance's own last reading makes prediction target and
  ground truth the same point by construction, for every instance
  equally, with no distance-based "nearest" search left to get it
  wrong. Summed across every slope-identified instance in the pool
  (every one has a last reading, so nothing needs excluding anymore),
  minimized via scipy.optimize.minimize_scalar, bounded to [min, max]
  of the service's own per-instance fitted slopes rather than
  curve_fit's generic physical bound. An optimum landing on either edge
  means the FIT itself is suspect (grouping is no longer a possible
  cause, now that pooling never crosses a service boundary) and falls
  back to the plain mean for that service. pool_night_ratio mirrors
  this exactly, with candidate night ratio in place of candidate slope.
  See pool_slope/pool_night_ratio/bracket_with_weight/
  predict_t1_via_slide.

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
launcher-button/startup-refresh pattern FloorEstimates.py used to. Keyed by
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
from datetime import datetime, timedelta, time as dt_time

import numpy as np
from scipy.optimize import minimize_scalar, brentq

import os

from clustering import cluster_services, service_representative
from settings import load_settings, DECLINE_CURVE_SETTINGS_KEY, DEFAULT_DECLINE_CURVE_SETTINGS
from DeclineCurveHierarchy import resolve_coefficients
from PoolingSettingsDialog import excluded_date_where_clause
from observation_filters import not_seat_map_only_where_clause
from timezones import et_equivalent_datetime, UnconfirmedAirportError

# Anchored to this script's own location, not the current working
# directory - a bare "nonrev.db" here silently creates a fresh empty
# db wherever you happen to run the script FROM (e.g. python/nonrev.db
# if run from inside python/), rather than erroring. Matches the
# pattern server.py/create_db.py/confirm_airports.py already use.
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'nonrev.db')

# Safety cap on how far solve_c1_from_reading/solve_zero_crossing_from_
# reading will widen their brentq search bracket - night_ratio can now
# solve arbitrarily close to 0 (an instance that's essentially flat
# overnight), and 1/night_ratio blowing up would otherwise overflow
# datetime arithmetic. 90 days is generous relative to any real
# hoursBeforeDep value in this project, never actually binding for a
# realistic instance - it only guards the degenerate case.
MAX_BRACKET_SEARCH_HOURS = 24 * 90

CABIN_COLUMNS = {
    "y": "y",
    "cPlus": "cPlus",
    "firstOrPS": "firstOrPS",
    "d1": "d1",
}


def load_observations(conn):
    """Reads observations directly - no flightSchedule join.
    depTime lives on the observation row itself now. Returns (rows,
    dropped_count): rows with an unparsable flightDate or a null depTime
    (a handful of legacy rows predating the depTime column, or an
    unconfirmed-airport gap at logging time - same class GraphObservations
    already drops for the same reason) are excluded and counted.

    Also excludes any row whose flightDate falls in a saved
    excludedDateRanges window (2026-09-16 - this is THE reason that table
    exists: a three-day-weekend or a period of schedule churn is real,
    known-anomalous behavior that would otherwise get pooled right in
    alongside a service's normal day-to-day pattern. Confirmed this was
    silently NOT happening before this fix - excludedDateRanges existed
    and had real ranges entered, but nothing here was checking it)."""
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT observationId, carrier, org, dest, flightDate,
               checkTimestamp, hoursBeforeDep, depTime,
               y, cPlus, firstOrPS, d1
        FROM observations
        WHERE {excluded_date_where_clause()}
          AND {not_seat_map_only_where_clause()}
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


def slide_c1_through_readings(readings, pooled_c1, slope, night_ratio, departure_dt,
                                night_start_hour=22, night_end_hour=7):
    """Walks one cabin's readings in CHRONOLOGICAL order, starting from
    pooled_c1 (the service's own resolved anchor - "what the coefficient
    says"), sliding C1 as needed to stay consistent with each actual
    reading in turn - his design (2026-09-15), replacing the earlier
    previous-reading-only projection that both the live Steps column and
    the curve-estimate hint used to rely on.

    An INTERIOR reading (1-8) pins C1 exactly - only one C1 is
    consistent with an exact interior value at a known hbd, given a
    fixed slope - so C1 always moves to match it, and the step is
    however far the pre-update C1 was predicting.

    A RAIL reading (9 or 0) only BOUNDS C1, it doesn't pin it - a 9 is
    consistent with any C1 <= this reading's hbd, a 0 with any C1large
    enough that decline has already completed by this hbd. If the
    CURRENT running C1 already satisfies that bound, nothing moves and
    the step is 0 - a rail reading that matches what's already assumed
    isn't a surprise, no matter how long the gap since the last one was.
    If it doesn't, C1 slides to the boundary value (the MINIMAL
    correction that restores consistency) - and each rail type can only
    ever push C1 in ONE direction: an inconsistent 9 always pulls C1
    down (the corner must be later than currently modeled - still full
    when the curve expected some decline), an inconsistent 0 always
    pushes C1 up (the corner must be earlier - fully declined sooner
    than the curve expected) - never the reverse for that same rail
    type, since a rail sits at one of piecewise_model's two clips.

    This is the fix for the old rail-to-rail bug: two honest 9s in a row
    separated by a long gap used to report a large fabricated step
    (projecting forward from the first 9 at the pooled slope predicted a
    big decline that never had to happen, since a rail never actually
    pinned that decline in the first place) - now correctly reports 0.

    A rail-driven slide is only reported as a step once the cabin has
    already shown at least one interior reading so far today (his
    refinement, 2026-09-16: "C1 is allowed to slide without calling that
    a step change, unless we're already on the slope part"). Before that
    point, a rail correcting C1 is just ordinary calibration - the
    running C1 wasn't well-anchored yet anyway, so there's nothing
    genuinely surprising about it moving; the step is suppressed to 0
    even when C1 itself still slides underneath. Once an interior
    reading has confirmed the cabin is genuinely mid-decline, every
    later inconsistency - rail or interior - is a real step, since by
    then there's an actual established curve to be surprised against.

    Returns a list, same length/order as readings, of (predicted_before,
    step, c1_after) - predicted_before is what the running C1 said
    BEFORE this reading was folded in (the actual "surprise" baseline),
    step is the rounded seat-equivalent correction (0 for an
    already-consistent rail, or for a pre-slope rail correction even
    when C1 does move), c1_after is the running C1 once this reading has
    been folded in. The LAST entry's c1_after is "today's current best
    estimate of C1," suitable for projecting forward to right now (see
    curve_estimates_for_row) - unaffected by the reporting rule above,
    since C1 itself always slides the same way regardless of whether
    that slide gets shown as a step."""
    current_c1 = pooled_c1
    on_slope = False  # flips true on the first interior reading
    out = []
    for hbd, val in readings:
        predicted = float(piecewise_model(hbd, current_c1, slope, night_ratio, departure_dt,
                                           night_start_hour, night_end_hour))
        if val >= 9:
            if predicted >= 9 - 1e-9:
                step = 0
                # already consistent - current_c1 unchanged, a rail
                # never tightens itself further, no evidence it should.
            else:
                step = int(round(val - predicted)) if on_slope else 0
                current_c1 = solve_c1_from_reading(hbd, 9, slope, night_ratio, departure_dt,
                                                    night_start_hour, night_end_hour)
        elif val <= 0:
            if predicted <= 1e-9:
                step = 0
            else:
                step = int(round(val - predicted)) if on_slope else 0
                current_c1 = solve_c1_from_reading(hbd, 0, slope, night_ratio, departure_dt,
                                                    night_start_hour, night_end_hour)
        else:
            # Interior - exact pin, always moves, regardless of whether
            # it happens to be numerically close to the old prediction.
            # Also the trigger that puts the cabin "on the slope" for
            # every reading after this one, gating whether a later rail
            # correction gets reported (see docstring).
            step = int(round(val - predicted))
            current_c1 = solve_c1_from_reading(hbd, val, slope, night_ratio, departure_dt,
                                                night_start_hour, night_end_hour)
            on_slope = True
        out.append((predicted, step, current_c1))
    return out


def resolved_cabin_value(row, real_col):
    """The real (binary-search-confirmed) value, or None if this cabin
    wasn't logged on this reading. No glance/cheap fallback - retired
    2026-09-14 (his call: the fit uses only real actual readings now,
    never a glance-derived substitute of any kind). Coerces stray
    blank/whitespace cells."""
    v = row.get(real_col)
    if v is None:
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def gather_instances(rows, dep_time_to_service, cabin):
    """For one cabin, groups matched observation rows into instances
    keyed by (serviceId, flightDate). Readings are sorted chronologically
    (ascending real time = descending hoursBeforeDep).

    Returns dict: (serviceId, flightDate) -> {"readings":
    [(hoursBeforeDep, value), ...] sorted chronologically, "depTime":
    this instance's own actual departure time (HHMM int) - needed to
    convert a reading's hoursBeforeDep into a real calendar timestamp
    for day/night classification (see piecewise_model). Taken from
    whichever contributing reading has the SMALLEST hoursBeforeDep (the
    most recently logged check for this instance), on the same
    least-likely-to-be-stale reasoning already used elsewhere for depTime
    drift (see nonrev-hoursbeforeDep-hardening). This is scoped to this
    cabin's own subset of rows, so it can differ by a few minutes across
    cabins on the same flight-day - irrelevant at the day/night
    granularity this is used for."""
    real_col = CABIN_COLUMNS[cabin]

    instances = defaultdict(lambda: {"readings": [], "depTime": None, "_minHbd": None})
    for r in rows:
        val = resolved_cabin_value(r, real_col)
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
        inst = instances[key]
        inst["readings"].append((hbd, val))
        if inst["_minHbd"] is None or hbd < inst["_minHbd"]:
            inst["_minHbd"] = hbd
            inst["depTime"] = r["depTime"]

    for inst in instances.values():
        inst["readings"].sort(key=lambda x: -x[0])  # descending hoursBeforeDep = chronological
        del inst["_minHbd"]

    return instances


def _night_overlap_hours(t_start, t_end, night_start_hour, night_end_hour):
    """Total hours of [t_start, t_end) that fall inside the recurring
    daily night window [night_start_hour:00, next-day night_end_hour:00)
    - e.g. 22:00-07:00. t_start/t_end are naive local datetimes (domestic
    only - no timezone crossing, see effective_hours_between); assumes
    t_start <= t_end."""
    if t_end <= t_start:
        return 0.0
    total = 0.0
    day = t_start.date() - timedelta(days=1)
    while day <= t_end.date():
        night_open = datetime.combine(day, dt_time(night_start_hour, 0))
        night_close = night_open + timedelta(hours=(24 - night_start_hour) + night_end_hour)
        lo = max(t_start, night_open)
        hi = min(t_end, night_close)
        if hi > lo:
            total += (hi - lo).total_seconds() / 3600.0
        day += timedelta(days=1)
    return total


def _day_night_hours_between(t_start, t_end, night_start_hour, night_end_hour):
    """Same window-overlap accounting as effective_hours_between, but
    returns the day/night components SEPARATELY rather than blending
    them with a ratio - needed now that fit_slope_and_night_ratio solves
    for day-slope and night-slope as two independent linear unknowns
    instead of taking night_ratio as a fixed external input. Order of
    t_start/t_end doesn't matter - always returns non-negative
    (day_hours, night_hours)."""
    lo, hi = (t_start, t_end) if t_start <= t_end else (t_end, t_start)
    total_hours = (hi - lo).total_seconds() / 3600.0
    night_hours = _night_overlap_hours(lo, hi, night_start_hour, night_end_hour)
    return total_hours - night_hours, night_hours


def effective_hours_between(t_start, t_end, night_ratio, night_start_hour=22, night_end_hour=7):
    """Elapsed hours between two real local timestamps, with hours
    inside the nightly [night_start_hour, night_end_hour) window counted
    at night_ratio instead of 1.0 - the day/night decline-rate split
    (see settings.DEFAULT_DECLINE_CURVE_SETTINGS: his call was a step
    change at these two clock times, not a smooth curve - a real thing
    the world does (mostly asleep, then mostly not), not smoothing
    something that's actually gradual). Order of t_start/t_end doesn't
    matter - always returns a non-negative value; the caller
    (piecewise_model) handles which direction is "forward"."""
    lo, hi = (t_start, t_end) if t_start <= t_end else (t_end, t_start)
    total_hours = (hi - lo).total_seconds() / 3600.0
    night_hours = _night_overlap_hours(lo, hi, night_start_hour, night_end_hour)
    day_hours = total_hours - night_hours
    return day_hours + night_ratio * night_hours


def piecewise_model(hbd, c1, slope, night_ratio=1.0, departure_dt=None,
                     night_start_hour=22, night_end_hour=7):
    """Flat at 9 until C1, then declines - slope seats/hour during the
    day, slope*night_ratio seats/hour overnight - flat at 0 once fully
    declined. hbd = hoursBeforeDep; larger hbd = further from departure.

    night_ratio/departure_dt default to (1.0, None), which collapses
    this back to the original uniform-rate model exactly (departure_dt
    is irrelevant when ratio=1, so a caller that hasn't been updated to
    pass real calendar context keeps working unchanged). A caller that
    DOES want the day/night split must pass a real departure_dt (the
    flight's actual local departure timestamp) - hbd and c1 are both
    "hours before THIS departure", converted internally to real
    timestamps via departure_dt so effective_hours_between can tell
    which portion of the gap was day vs. night.

    Vectorized hbd (a numpy array, as scipy.optimize.curve_fit passes)
    is supported via an explicit per-element loop when night_ratio != 1,
    since datetime/timedelta arithmetic doesn't vectorize the way plain
    subtraction does - the uniform-rate fast path (ratio == 1) is
    unaffected and stays fully vectorized."""
    if night_ratio == 1.0 or departure_dt is None:
        return np.clip(9 - slope * (c1 - hbd), 0, 9)

    hbd_arr = np.atleast_1d(np.asarray(hbd, dtype=float))
    t_c1 = departure_dt - timedelta(hours=float(c1))
    out = np.empty_like(hbd_arr)
    for i, h in enumerate(hbd_arr):
        t_h = departure_dt - timedelta(hours=float(h))
        elapsed = effective_hours_between(t_c1, t_h, night_ratio, night_start_hour, night_end_hour)
        if h > c1:
            elapsed = -elapsed
        out[i] = 9 - slope * elapsed
    result = np.clip(out, 0, 9)
    return result if np.ndim(hbd) > 0 else float(result[0])


def solve_c1_from_reading(hbd_r, val_r, slope, night_ratio=1.0, departure_dt=None,
                           night_start_hour=22, night_end_hour=7):
    """Given a reading (hbd_r, val_r) known to sit at or past the C1
    corner, solves for the C1 that makes the model pass through it
    exactly - i.e. the "slide the curve through this reading" anchor
    step used throughout predict_t1_via_slide and T1Estimator.

    Closed-form when night_ratio == 1 (the original uniform-rate
    algebra: c1 = hbd_r + (9 - val_r) / slope). Otherwise this can't be
    solved algebraically anymore, because the day/night split of the
    span between C1 and the reading depends on where C1 itself falls -
    a numeric root-find instead (scipy.optimize.brentq), bounded
    between the uniform-rate answer (a lower bound - the true C1 can
    only be further out, since night hours count for less) and the
    all-night worst case. A val_r of exactly 9 (target_elapsed == 0) is
    a trivial closed-form case regardless of night_ratio - c1 == hbd_r,
    zero elapsed needed - handled directly rather than risking brentq on
    a degenerate zero-width bracket (this case comes up often now that
    slide_c1_through_readings calls this for every inconsistent-rail
    reading, not just interior ones)."""
    target_elapsed = min((9.0 - val_r) / slope, MAX_BRACKET_SEARCH_HOURS)
    if target_elapsed <= 0:
        return hbd_r
    if night_ratio == 1.0 or departure_dt is None:
        return hbd_r + target_elapsed

    def f(c1_candidate):
        t_c1 = departure_dt - timedelta(hours=c1_candidate)
        t_r = departure_dt - timedelta(hours=hbd_r)
        return effective_hours_between(t_c1, t_r, night_ratio, night_start_hour, night_end_hour) - target_elapsed

    lo = hbd_r
    hi = hbd_r + min(target_elapsed / max(night_ratio, 1e-6), MAX_BRACKET_SEARCH_HOURS)
    if hi <= lo:
        hi = lo + 1e-3
    try:
        return brentq(f, lo, hi, xtol=1e-4)
    except ValueError:
        # Bracket failed (shouldn't happen given the bounds above, but
        # falling back to the uniform-rate answer beats crashing).
        return hbd_r + target_elapsed


def solve_zero_crossing_from_reading(hbd_r, val_r, slope, night_ratio=1.0, departure_dt=None,
                                      night_start_hour=22, night_end_hour=7):
    """Mirror of solve_c1_from_reading for the BOTTOM corner: given a
    reading (hbd_r, val_r) known to sit at or before the zero-crossing
    (still declining, not yet at 0), solves the hoursBeforeDep at which
    the model reaches exactly 0 at this slope/night_ratio - i.e. walks
    FORWARD in time (toward departure, decreasing hbd) instead of
    backward, otherwise the same closed-form-when-ratio-1,
    brentq-otherwise structure. A val_r of exactly 0 (target_elapsed ==
    0) returns hbd_r directly, same degenerate-bracket reasoning as
    solve_c1_from_reading's val_r == 9 case."""
    target_elapsed = min(val_r / slope, MAX_BRACKET_SEARCH_HOURS)
    if target_elapsed <= 0:
        return hbd_r
    if night_ratio == 1.0 or departure_dt is None:
        return hbd_r - target_elapsed

    def f(c2_candidate):
        t_r = departure_dt - timedelta(hours=hbd_r)
        t_c2 = departure_dt - timedelta(hours=c2_candidate)
        return effective_hours_between(t_r, t_c2, night_ratio, night_start_hour, night_end_hour) - target_elapsed

    hi = hbd_r
    lo = hbd_r - min(target_elapsed / max(night_ratio, 1e-6), MAX_BRACKET_SEARCH_HOURS)
    if lo >= hi:
        lo = hi - 1e-3
    try:
        return brentq(f, lo, hi, xtol=1e-4)
    except ValueError:
        return hbd_r - target_elapsed


def fit_slope_and_night_ratio(window_readings, departure_dt, night_start_hour, night_end_hour,
                               default_night_ratio):
    """STAGE 1 of fit_instance. Solves day-slope and night-slope
    JOINTLY and LINEARLY from consecutive gaps across window_readings
    (already restricted to the [last-9 .. first-0] transition window -
    see fit_instance). Each consecutive pair contributes one row:
    valueChange = day_slope*day_hours + night_slope*night_hours -
    genuinely linear in the two unknowns, solved via ordinary least
    squares (numpy.linalg.lstsq). night_slope is then clamped into [0,
    day_slope] (night can't be steeper than day) as a POST-HOC clamp
    rather than a constrained solve - that bound is a relation BETWEEN
    the two unknowns, not a box bound on either one alone, so it can't
    be handed to the solver as a bound the way a single-variable limit
    could; clamping after the fact is the pragmatic equivalent.

    Consecutive gaps only (not every pairwise combination of readings in
    the window) - the simplest reading of "every pair that constrains a
    rate", avoiding the double-counting a full pairwise expansion would
    introduce from overlapping spans.

    Falls back to a single unknown (day_slope only, via plain total
    elapsed hours with no day/night split, paired with
    default_night_ratio returned as-is) whenever: departure_dt is None
    (no real calendar context to classify day vs. night at all); there's
    no night-side evidence at all across every gap (nothing to identify
    a ratio from); or the two-unknown solve comes back rank-deficient or
    with a non-positive day_slope (degenerate data).

    Returns (day_slope, night_ratio, has_night_evidence), or None if
    day_slope can't be identified at all (fewer than 2 usable gaps, or
    every gap has zero elapsed hours). has_night_evidence is True only
    when night_ratio was actually solved from real night-side data, not
    just passed through as the caller's default - the two-unknown solve
    is what sets it, everything routed through day_slope_only_fallback
    leaves it False."""
    gaps = []
    for (hbd_a, val_a), (hbd_b, val_b) in zip(window_readings, window_readings[1:]):
        if departure_dt is not None:
            t_a = departure_dt - timedelta(hours=float(hbd_a))
            t_b = departure_dt - timedelta(hours=float(hbd_b))
            day_h, night_h = _day_night_hours_between(t_a, t_b, night_start_hour, night_end_hour)
        else:
            day_h, night_h = float(hbd_a - hbd_b), 0.0
        if day_h + night_h <= 0:
            continue
        gaps.append((val_a - val_b, day_h, night_h))

    if not gaps:
        return None

    def day_slope_only_fallback():
        total_elapsed = sum(g[1] + g[2] for g in gaps)
        total_change = sum(g[0] for g in gaps)
        if total_elapsed <= 0:
            return None
        day_slope = total_change / total_elapsed
        return (day_slope, default_night_ratio, False) if day_slope > 0 else None

    total_night_hours = sum(g[2] for g in gaps)
    if departure_dt is None or total_night_hours <= 1e-6 or len(gaps) < 2:
        return day_slope_only_fallback()

    A = np.array([[g[1], g[2]] for g in gaps], dtype=float)
    b = np.array([g[0] for g in gaps], dtype=float)
    (day_slope, night_slope), _residuals, rank, _sv = np.linalg.lstsq(A, b, rcond=None)

    if rank < 2 or day_slope <= 0:
        return day_slope_only_fallback()

    night_slope = min(max(night_slope, 0.0), day_slope)
    return (float(day_slope), float(night_slope / day_slope), True)


def fit_instance(readings, rmse_threshold, max_iterations,
                  night_ratio=1.0, departure_dt=None,
                  night_start_hour=22, night_end_hour=7):
    """Fits the piecewise model to one flight-day instance in TWO
    STAGES rather than one joint nonlinear solve (see module docstring
    for why): STAGE 1 (fit_slope_and_night_ratio) solves day-slope and
    night-slope directly from the interior readings and the two
    transition brackets, using only the [last-9 .. first-0] window (see
    below); STAGE 2 solves c1 algebraically from that fixed slope,
    anchored off the first non-9 reading in the window
    (solve_c1_from_reading) - or, if the window contains no non-9
    reading at all (never left 9), off the window's own start as a
    placeholder with slope left unidentified.

    TRANSITION WINDOW (first-edge/last-edge, 2026-09-18): anchored
    independently at each end, not by scanning for rail values
    anywhere in the middle. Start: if the readings' own first point is
    a 9, walk forward through however many 9s lead the sequence and
    anchor on the LAST one - the single 9 immediately before this
    instance's first-ever departure from 9. If the first point isn't a
    9 (logging picked up mid-decline), there's no edge to trim, so the
    window starts at index 0. End: symmetric - if the last point is a
    0, walk backward through the trailing 0s and anchor on the FIRST
    one; if the last point isn't a 0 (still declining, unresolved as
    of the latest check - including an instance that hit 0 once,
    bounced back to 9, and is declining again), there's no edge yet,
    so the window ends at the last index. Only genuinely redundant
    flat rail time at the two ends gets trimmed this way; every
    reading between the two anchors - interior values, a full bounce
    back to 9, a second decline, whatever wobbles got corrected in
    place - feeds stage 1 untouched. Superseded the 2026-09-17
    first-seen-9/last-seen-0 version, which kept the full flat run at
    both ends and measurably diluted the fitted slope toward zero
    (every 9->9 or 0->0 pair in the window still fed the least-squares
    solve).

    Both declining segments in a bounce-back-and-redecline case are
    real - not one "true" segment with the other forced onto it. A
    cancellation reopening seats back toward 9 mid-decline interrupts
    one continuous erosion into two; correction is what puts the
    pieces back into one fittable line, not a tool bending a fake
    segment to match a real one. Because correction is unclamped, a
    heavily-corrected interior point can legitimately land outside the
    0-9 range (an implied 11, an implied -2) once enough step-change
    activity gets folded in - that's the model working as intended,
    not a bug: the rail bounds are physical limits on what gets
    OBSERVED, not limits on what a corrected fitting point is allowed
    to be. This windowing accepts that a genuine two-segment
    bounce-back won't always converge cleanly to one (c1, slope) line
    (see DECLINE_CURVE_DESIGN.md) - a fit gained on the instances
    where it does converge is a net win regardless of the ones where
    it still doesn't.

    CORRECTION-IN-PLACE, RESCOPED: a correction pass can land on an
    interior residual OR a bracket residual - the model's predicted
    value at ANY 9-or-0-valued point inside the window can disagree
    with the observed rail value there (e.g. c1, solved from an
    interior reading further in, implies the corner happened earlier
    than a genuine 9 actually observed) - exactly the same footing as
    an interior step change: a booking event can land right at a
    crossing (or a bounce back to 9) as easily as mid-decline. Every
    9/0-valued point in the window is correction-eligible, not just
    its two edges, so a mid-sequence bounce-back gets the same
    unclamped repair an interior point already gets rather than
    silently forcing a window restart (see TRANSITION WINDOW above).
    Only INTERIOR residuals count toward the rmse_threshold
    convergence check, though - rails should sit exactly on the
    clamped model once things are consistent, so a residual there
    doesn't get to loosen the stopping bar, it just stays eligible for
    correction like anything else. max_iterations remains a hard cap
    regardless of convergence, same as before - required whenever
    correcting in place doesn't shrink the candidate set the way
    removal would, so there's no structural convergence guarantee.

    Returns a dict with c1, slope (None if unidentifiable - see above),
    nightRatio (this instance's own solved ratio, or the externally
    supplied default when there wasn't enough night-side evidence to
    solve one), n_interior, n_points (unchanged throughout - nothing is
    ever removed), step_changes (a list of (hoursBeforeDep,
    original_observed_value, net_correction), same shape as before),
    and iterations (how many correction passes this instance actually
    took, for visibility into how hard max_iterations is being leaned
    on) - or None if there's nothing fittable at all (fewer than 2
    readings)."""
    if len(readings) < 2:
        return None

    hbds = [h for h, v in readings]
    originals = [v for h, v in readings]
    pts = list(readings)  # values mutate in place; never shrinks
    corrections = defaultdict(int)

    def window_bounds(cur_pts):
        n = len(cur_pts)

        lo = 0
        if cur_pts[0][1] >= 9:
            for i, (h, v) in enumerate(cur_pts):
                if v >= 9:
                    lo = i
                else:
                    break

        hi = n - 1
        if cur_pts[-1][1] <= 0:
            for i in range(n - 1, -1, -1):
                if cur_pts[i][1] <= 0:
                    hi = i
                else:
                    break

        return lo, hi

    def stage_fit(cur_pts):
        lo, hi = window_bounds(cur_pts)
        window = cur_pts[lo:hi + 1]
        solved = fit_slope_and_night_ratio(
            window, departure_dt, night_start_hour, night_end_hour, night_ratio,
        )
        if solved is None:
            return None
        day_slope, resolved_night_ratio, has_night_evidence = solved

        first_non9 = next(((h, v) for h, v in window if v < 9), None)
        if first_non9 is not None:
            c1 = solve_c1_from_reading(
                first_non9[0], first_non9[1], day_slope, resolved_night_ratio,
                departure_dt, night_start_hour, night_end_hour,
            )
        else:
            c1 = window[0][0]
        return day_slope, resolved_night_ratio, c1, has_night_evidence

    def build_result(day_slope, resolved_night_ratio, c1, n_interior, has_night_evidence=False):
        step_changes = [
            (hbds[i], originals[i], corrections[i])
            for i in sorted(corrections) if corrections[i] != 0
        ]
        return {"c1": float(c1), "slope": float(day_slope) if day_slope is not None else None,
                "nightRatio": float(resolved_night_ratio), "nightRatioResolved": has_night_evidence,
                "n_interior": n_interior,
                "n_points": len(pts), "step_changes": step_changes,
                "corrected_readings": list(pts), "iterations": iterations}

    iterations = 0
    while True:
        interior_idx = [i for i, (h, v) in enumerate(pts) if 1 <= v <= 8]
        fit = stage_fit(pts)
        if fit is None:
            # No identifiable slope at all (e.g. never left 9 within
            # the window) - fall back to a window-anchored c1 with
            # slope left unresolved (None), matching the previous
            # n_interior==0 convention that pool_slope already knows to
            # exclude.
            lo, _hi = window_bounds(pts)
            return build_result(None, night_ratio, pts[lo][0], len(interior_idx))

        day_slope, resolved_night_ratio, c1, has_night_evidence = fit
        lo, hi = window_bounds(pts)
        bracket_idx = [i for i in range(lo, hi + 1) if pts[i][1] in (9, 0)]
        check_idx = sorted(set(interior_idx) | set(bracket_idx))
        if not check_idx:
            return build_result(day_slope, resolved_night_ratio, c1, len(interior_idx), has_night_evidence)

        residuals = []
        for i in check_idx:
            h, v = pts[i]
            model_val = piecewise_model(h, c1, day_slope, resolved_night_ratio, departure_dt,
                                         night_start_hour, night_end_hour)
            residuals.append(v - model_val)

        interior_residuals = [r for r, i in zip(residuals, check_idx) if i in interior_idx]
        rmse_for_stopping = (
            float(np.sqrt(np.mean(np.square(interior_residuals)))) if interior_residuals else 0.0
        )
        if rmse_for_stopping <= rmse_threshold or iterations >= max_iterations:
            return build_result(day_slope, resolved_night_ratio, c1, len(interior_idx), has_night_evidence)

        worst_local = int(np.argmax(np.abs(residuals)))
        worst_i = check_idx[worst_local]
        rounded = round(residuals[worst_local])
        if rounded == 0:
            # Worst point is already within half a seat of the curve -
            # nothing left worth correcting. Stop rather than burn
            # iterations relabeling the same near-zero residual.
            return build_result(day_slope, resolved_night_ratio, c1, len(interior_idx), has_night_evidence)

        corrections[worst_i] += rounded
        h, v = pts[worst_i]
        pts[worst_i] = (h, v - rounded)  # unclamped - see module docstring
        iterations += 1


# Target hours for the slope-pooling objective (see pool_slope docstring)
# - T-4 is his real operational checkpoint, the point his own workflow
# already tries to get a reading near before committing to game day.
T4_TARGET_HOURS_FOR_POOLING = 4.0
# No longer used by pool_slope/pool_night_ratio as of 2026-09-17 (they
# now score against each instance's own last reading - see their
# docstrings) - kept because T4T1Backtest.py still imports and uses it
# directly for its own leave-one-out ground truth, which was not part
# of this round's fix and so still scores against nearest-to-T-1 the
# old way. Flagged, not touched.
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


def predict_t1_via_slide(readings, slope, t4_hours, t1_hours,
                          night_ratio=1.0, departure_dt=None,
                          night_start_hour=22, night_end_hour=7):
    """What the live sliding estimator would predict at t1_hours, using
    only readings available at/around t4_hours and the given candidate
    slope - the core of the slope-pooling objective (see pool_slope).
    Brackets readings at t4_hours; for each bracketing reading, slides
    the curve through it (solve_c1_from_reading - the C1 that makes the
    frozen curve at this slope, day/night split included, pass through
    that reading) and predicts forward to t1_hours; if there were two
    bracketing readings, interpolates the two resulting T1 PREDICTIONS
    using the bracket's own distance weighting (his explicit call - not
    interpolating the readings before sliding; the two aren't quite
    identical near a 0/9 clamp, and he wants the version that matches
    what the live estimator would actually have shown at each moment).
    Returns None if readings is empty (nothing to anchor on this side).

    night_ratio/departure_dt/night_start_hour/night_end_hour: this
    instance's resolved night split and real departure timestamp -
    same fixed context fit_instance's own curve_fit used, so the
    slide-and-predict here stays consistent with how the instance was
    originally fit."""
    bracket = bracket_with_weight(readings, t4_hours)
    if bracket is None:
        return None

    def slide_predict(reading):
        hbd_r, val_r = reading
        c1_prime = solve_c1_from_reading(
            hbd_r, val_r, slope, night_ratio, departure_dt,
            night_start_hour, night_end_hour,
        )
        return float(piecewise_model(
            t1_hours, c1_prime, slope, night_ratio, departure_dt,
            night_start_hour, night_end_hour,
        ))

    if bracket[0] == "single":
        return slide_predict(bracket[1])

    _, b, a, weight = bracket
    b_pred = slide_predict(b)
    a_pred = slide_predict(a)
    return b_pred + (a_pred - b_pred) * weight


def pool_slope(fits_by_instance, night_start_hour=22, night_end_hour=7,
                diagnostics_out=None):
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
    bracket at T-4, slide-and-predict from each bracketing reading
    (using this instance's own step-change-CORRECTED readings, the
    same data its own curve_fit used) to THIS INSTANCE'S OWN LAST
    reading's hour, compare directly against that reading. Fixed
    2026-09-17 (see module docstring for the bug this replaced): every
    instance has a last reading, so every slope-identified instance now
    contributes a term - there's no separate "does it have usable data
    near T-1" eligibility bar left to check. Summed across every
    qualifying instance, minimized via scipy.optimize.minimize_scalar
    bounded to [min, max] of that SERVICE's own already-fitted
    per-instance slopes (not curve_fit's generic physical bound) - his
    call: an optimum landing on either edge means the fit itself is
    suspect (grouping is no longer a possible cause, now that pooling
    never crosses a service boundary), so that case falls back to the
    plain mean instead of trusting a rail-slammed answer.

    A resolved slope (fit["slope"] is not None) is the only eligibility
    bar now, for the min/max bound, the plain-mean fallback, AND the
    optimization sum alike - this only requires two usable readings
    with elapsed time between them, down to a boundary-only (last-9,
    first-0) pair; there's no additional requirement for an interior
    (1-8) reading on top of that. (A single-reading instance would
    still qualify in principle, but its own prediction target and
    ground truth collapse to the same point, so it contributes exactly
    0 to the objective for any candidate slope - inert, not excluded,
    and not biasing anything.)

    Returns dict serviceId -> (slope, gap_hours, n_instances) - same
    shape as the old groupId-keyed version, just keyed by service now.
    gap_hours (9.0 / slope) is the daytime-rate crossing time only -
    it's a reported/console-summary number, not persisted or used
    anywhere else, so it doesn't attempt to account for night_ratio.

    night_start_hour/night_end_hour: passed straight through to every
    predict_t1_via_slide call in the optimization objective, alongside
    each instance's OWN resolved night_ratio/departureDt (stashed on its
    fit dict by compute_all_fits - see that function) - a service's
    night_ratio is constant across its own instances, but each instance
    still needs its own real calendar departure_dt (different
    flightDate).

    diagnostics_out: optional dict, populated in place with serviceId
    -> {"iterations", "rmse"} for every service whose pooled slope
    actually came from a real optimization (not skipped-via-agreement
    or landed-on-an-edge-and-fell-back-to-the-mean, where there's no
    optimizer result worth reporting) - purely additive; existing
    callers that don't pass this see no behavior change at all. .rmse
    is sqrt(the optimizer's own final objective value / instance
    count), i.e. RMS prediction error in seats, not a separate
    computation."""
    by_service = defaultdict(list)
    for (service_id, flight_date), fit in fits_by_instance.items():
        if fit is None or fit["slope"] is None:
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
            def objective(candidate_slope, _fits=fits):
                total = 0.0
                for f in _fits:
                    readings = f["corrected_readings"]
                    target_hours = readings[-1][0]
                    pred = predict_t1_via_slide(
                        readings, candidate_slope,
                        T4_TARGET_HOURS_FOR_POOLING, target_hours,
                        night_ratio=f.get("nightRatio", 1.0) or 1.0,
                        departure_dt=f.get("departureDt"),
                        night_start_hour=night_start_hour, night_end_hour=night_end_hour,
                    )
                    truth = readings[-1][1]
                    total += (pred - truth) ** 2
                return total

            res = minimize_scalar(objective, bounds=(slope_lo, slope_hi), method="bounded")
            slope = float(res.x)
            # Rail check (see docstring) - a small relative
            # tolerance, not exact equality, since the bounded
            # optimizer can land a hair off the true edge.
            edge_tol = (slope_hi - slope_lo) * 1e-6
            if slope <= slope_lo + edge_tol or slope >= slope_hi - edge_tol:
                slope = fallback_slope
            elif diagnostics_out is not None:
                diagnostics_out[sid] = {
                    "iterations": res.nit,
                    "rmse": float(np.sqrt(res.fun / len(fits))),
                }

        if slope <= 0:
            continue
        pooled[sid] = (slope, 9.0 / slope, len(fits))
    return pooled


def pool_night_ratio(fits_by_instance, night_start_hour=21, night_end_hour=7,
                      diagnostics_out=None):
    """Pooled per-(day-of-week-specific) SERVICE night ratio, mirroring
    pool_slope exactly: direct optimization against real predictive
    accuracy via predict_t1_via_slide, rather than a plain mean of
    per-instance night ratios. Candidate night ratios are scored with
    each instance's own SLOPE held fixed (the reverse of pool_slope,
    which holds each instance's night ratio fixed while slope varies) -
    the two pooled quantities are optimized independently of each other,
    same as they're independently fitted per instance in stage 1. Same
    2026-09-17 target fix as pool_slope: predicts to and scores against
    each instance's own last reading, not a fixed T-1 (see pool_slope's
    docstring and the module docstring for why).

    Only instances with has_night_evidence=True (a real two-unknown
    solve, not a day-slope-only fallback that just echoed the caller's
    default back) count here - an instance that never actually saw
    night hours has nothing to say about the night ratio at all, unlike
    slope, which every instance identifies whether or not it has an
    opinion on the night split.

    Same NO MINIMUM instance count as pool_slope: a service with exactly
    one qualifying instance uses that instance's own solved ratio
    directly. Same rail-check-falls-back-to-mean logic too.

    diagnostics_out: same meaning as pool_slope's - optional dict,
    populated in place with serviceId -> {"iterations", "rmse"} for
    services whose ratio came from a real (non-fallback) optimization.

    Returns dict serviceId -> (night_ratio, n_instances)."""
    by_service = defaultdict(list)
    for (service_id, flight_date), fit in fits_by_instance.items():
        if fit is None or fit["slope"] is None or not fit.get("nightRatioResolved"):
            continue
        by_service[service_id].append(fit)

    pooled = {}
    for sid, fits in by_service.items():
        per_instance_ratios = [f["nightRatio"] for f in fits]
        ratio_lo, ratio_hi = min(per_instance_ratios), max(per_instance_ratios)
        fallback_ratio = float(np.mean(per_instance_ratios))

        if ratio_lo >= ratio_hi:
            ratio = ratio_lo
        else:
            def objective(candidate_ratio, _fits=fits):
                total = 0.0
                for f in _fits:
                    readings = f["corrected_readings"]
                    target_hours = readings[-1][0]
                    pred = predict_t1_via_slide(
                        readings, f["slope"],
                        T4_TARGET_HOURS_FOR_POOLING, target_hours,
                        night_ratio=candidate_ratio,
                        departure_dt=f.get("departureDt"),
                        night_start_hour=night_start_hour, night_end_hour=night_end_hour,
                    )
                    truth = readings[-1][1]
                    total += (pred - truth) ** 2
                return total

            res = minimize_scalar(objective, bounds=(ratio_lo, ratio_hi), method="bounded")
            ratio = float(res.x)
            edge_tol = (ratio_hi - ratio_lo) * 1e-6
            if ratio <= ratio_lo + edge_tol or ratio >= ratio_hi - edge_tol:
                ratio = fallback_ratio
            elif diagnostics_out is not None:
                diagnostics_out[sid] = {
                    "iterations": res.nit,
                    "rmse": float(np.sqrt(res.fun / len(fits))),
                }

        pooled[sid] = (ratio, len(fits))
    return pooled


def pool_late_step_changes(fits_by_instance, golden_ticket_hours,
                            low_hours=0.75, high_hours=4.0):
    """Game-day-relevant step-change stats (his idea, 2026-09-17): per
    instance, sum every correction fit_instance applied within
    [low_hours, high_hours] of departure into ONE scalar per instance -
    "how much did this flight's count net-move late" - rather than
    counting or averaging individual step events, so an instance with
    two smaller opposite-direction late jumps isn't conflated with one
    that took a single big jump of the same net size.

    Only instances that reached a real golden-ticket reading (a
    reading at or inside golden_ticket_hours - reusing the existing
    settings.py threshold everywhere else in this project already
    trusts for "close enough to departure to count as ground truth",
    not a separate cutoff invented for this report) are included - an
    instance that hasn't lived through its own late window yet has
    nothing to say about it.

    No fitting here, deliberately (his explicit call) - just the plain
    mean and RMS of the per-instance summed values, per service.

    Returns dict serviceId -> (mean, rms, n_instances)."""
    by_service = defaultdict(list)
    for (service_id, flight_date), fit in fits_by_instance.items():
        if fit is None:
            continue
        readings = fit["corrected_readings"]
        if not any(h <= golden_ticket_hours for h, v in readings):
            continue
        late_sum = sum(
            correction for (h, original, correction) in fit["step_changes"]
            if low_hours <= h <= high_hours
        )
        by_service[service_id].append(late_sum)

    pooled = {}
    for sid, sums in by_service.items():
        arr = np.array(sums, dtype=float)
        pooled[sid] = (float(np.mean(arr)), float(np.sqrt(np.mean(arr ** 2))), len(sums))
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


def compute_all_fits(conn, rmse_threshold, max_iterations,
                      night_start_hour=22, night_end_hour=7):
    """Shared fitting work for all four cabins - the expensive part
    (curve_fit per instance), factored out so both the console report
    (main()) and the persistence step (refresh_decline_curve_coefficients)
    run it exactly once rather than twice.

    Each (day-of-week-specific) SERVICE's night_ratio is resolved once,
    via the real coefficients hierarchy (DeclineCurveHierarchy.
    resolve_coefficients) - at the SAME granularity slope/C1 are pooled
    at (per service, using its representative depTime), not per raw
    depTime, matching how pool_slope/pool_c1 already treat a service as
    one unit despite declineCurveCoefficients persisting one row per raw
    depTime afterward. Currently this always resolves to the global
    default (tier 4 derivation for night_ratio isn't built yet - see
    module docstring), but it's wired through the real hierarchy now so
    a hand-set route/service override already works the moment one
    exists. Each instance's own departure_dt is reconstructed from its
    gathered depTime + flightDate and stashed on its fit dict, so
    downstream consumers (pool_slope's objective) don't need to
    reconstruct it themselves.

    Returns a dict: dep_time_to_service, service_info, and by_cabin:
    {cabin: {instances, fits_by_instance, service_slopes,
    slope_diagnostics, night_ratio_by_service_pooled,
    night_ratio_diagnostics, c1_by_service}}."""
    matched_rows, dropped_count = load_observations(conn)
    dep_time_to_service, service_info = build_service_map(matched_rows)

    by_cabin = {}
    for cabin in ["y", "cPlus", "firstOrPS", "d1"]:
        night_ratio_by_service = {}
        for sid, (org, dest, dow, rep_time, _cluster_size) in service_info.items():
            resolved = resolve_coefficients(conn, org, dest, dow, rep_time, cabin)
            night_ratio_by_service[sid] = resolved.get("nightRatio") or 1.0

        instances = gather_instances(matched_rows, dep_time_to_service, cabin)
        fits_by_instance = {}
        for key, inst in instances.items():
            sid, flight_date = key
            night_ratio = night_ratio_by_service.get(sid, 1.0)
            departure_dt = None
            if inst["depTime"] is not None:
                try:
                    # depTime is minutes-since-midnight ORIGIN-local (see
                    # timezones.et_equivalent_datetime) - NOT HHMM digits,
                    # a bug that lived here from 2026-09-14 to 2026-09-15
                    # and corrupted every night/day classification the
                    # actual curve fitting made in that window (this is
                    # THE fitting call site - the one place this bug
                    # mattered most). ET, not origin-local, because
                    # that's the zone checkTimestamp is actually logged
                    # in (eastern_now(), see SeatLoggingDialog.py) - what
                    # the night-ratio default was calibrated against.
                    flight_date_obj = datetime.strptime(flight_date, "%Y-%m-%d").date()
                    org = service_info[sid][0]
                    departure_dt = et_equivalent_datetime(conn, inst["depTime"], org, flight_date_obj).replace(tzinfo=None)
                except (ValueError, TypeError, UnconfirmedAirportError):
                    departure_dt = None
            fit = fit_instance(
                inst["readings"], rmse_threshold, max_iterations,
                night_ratio=night_ratio, departure_dt=departure_dt,
                night_start_hour=night_start_hour, night_end_hour=night_end_hour,
            )
            if fit is not None:
                fit["departureDt"] = departure_dt
            fits_by_instance[key] = fit

        slope_diagnostics = {}
        night_ratio_diagnostics = {}
        by_cabin[cabin] = {
            "instances": instances,
            "fits_by_instance": fits_by_instance,
            "service_slopes": pool_slope(fits_by_instance, night_start_hour, night_end_hour,
                                          diagnostics_out=slope_diagnostics),
            "slope_diagnostics": slope_diagnostics,
            "night_ratio_by_service_pooled": pool_night_ratio(fits_by_instance, night_start_hour, night_end_hour,
                                                                diagnostics_out=night_ratio_diagnostics),
            "night_ratio_diagnostics": night_ratio_diagnostics,
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
    recompute-from-scratch/manual-launcher-button/startup-refresh
    pattern FloorEstimates.refresh_floor_estimates used to (that module
    was retired 2026-09-14 along with all glance/cheap-derived
    estimation - see resolved_cabin_value). One row per (org, dest,
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
    result = compute_all_fits(
        conn, settings["stepChangeRmseThreshold"], settings["stepChangeMaxIterations"],
        night_start_hour=settings["nightStartHour"], night_end_hour=settings["nightEndHour"],
    )

    conn.execute("DELETE FROM declineCurveCoefficients")
    conn.execute("DELETE FROM declineCurveInstanceFits")

    # declineCurveCoefficients is keyed by every raw depTime observed
    # (not a cluster-representative time - see its own docstring), and
    # a wobbly day can put more than one raw depTime under the same
    # service. declineCurveInstanceFits needs to join against it by
    # exact (org, dest, dayOfWeek, depTime), so an instance's fit gets
    # written once per raw depTime its service actually spans, same
    # duplication declineCurveCoefficients itself already does - not a
    # new inconsistency, just matching the existing convention.
    service_to_dep_times = defaultdict(set)
    for (org, dest, dow, dep_time), sid in result["dep_time_to_service"].items():
        service_to_dep_times[sid].add((org, dest, dow, dep_time))

    rows_written = 0
    instance_rows_written = 0
    summary_by_cabin = {}
    for cabin, cabin_data in result["by_cabin"].items():
        fits_by_instance = cabin_data["fits_by_instance"]
        service_slopes = cabin_data["service_slopes"]
        night_ratios = cabin_data["night_ratio_by_service_pooled"]
        c1_by_service = cabin_data["c1_by_service"]

        for (org, dest, dow, dep_time), sid in result["dep_time_to_service"].items():
            c1_entry = c1_by_service.get(sid)
            slope_entry = service_slopes.get(sid)
            night_entry = night_ratios.get(sid)
            if c1_entry is None and slope_entry is None and night_entry is None:
                continue
            c1_val, n_c1 = c1_entry if c1_entry else (None, 0)
            slope_val, _gap_val, n_slope = slope_entry if slope_entry else (None, None, 0)
            night_val, n_night = night_entry if night_entry else (None, 0)
            conn.execute(
                """INSERT INTO declineCurveCoefficients
                   (org, dest, dayOfWeek, depTime, cabin, c1Hours, slopeSeatsPerHour,
                    nightSlopeRatio, nInstancesC1, nInstancesSlope, nInstancesNightSlope)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (org, dest, dow, dep_time, cabin, c1_val, slope_val, night_val,
                 n_c1, n_slope, n_night),
            )
            rows_written += 1

        # Visibility table: one row per instance that actually fit,
        # regardless of whether its service ever resolves a pooled
        # aggregate - this is the "let me see the numbers populate"
        # fix, so it deliberately doesn't gate on anything above.
        for (sid, flight_date), fit in fits_by_instance.items():
            if fit is None:
                continue
            for (org, dest, dow, dep_time) in service_to_dep_times.get(sid, ()):
                conn.execute(
                    """INSERT OR REPLACE INTO declineCurveInstanceFits
                       (org, dest, dayOfWeek, depTime, flightDate, cabin,
                        c1Hours, slopeSeatsPerHour, nInterior, nPoints, nStepChanges)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (org, dest, dow, dep_time, flight_date, cabin,
                     fit["c1"], fit["slope"],
                     fit["n_interior"], fit["n_points"], len(fit["step_changes"])),
                )
                instance_rows_written += 1

        n_fit = sum(1 for f in fits_by_instance.values() if f is not None)
        n_step_changes = sum(len(f["step_changes"]) for f in fits_by_instance.values() if f)
        n_with_step_changes = sum(1 for f in fits_by_instance.values() if f and f["step_changes"])
        summary_by_cabin[cabin] = {
            "nInstances": len(cabin_data["instances"]),
            "nFit": n_fit,
            "nInstancesWithStepChanges": n_with_step_changes,
            "nStepChangesTotal": n_step_changes,
            "nServicesWithSlope": len(service_slopes),
            "nServicesWithNightRatio": len(night_ratios),
            "nServicesResolved": len(c1_by_service),
            "nServicesTotal": len(result["service_info"]),
        }

    conn.commit()
    return {"rowsWritten": rows_written, "instanceRowsWritten": instance_rows_written,
            "byCabin": summary_by_cabin, "raw": result}


def main():
    conn = sqlite3.connect(DB_PATH)

    refreshed = refresh_decline_curve_coefficients(conn)
    result = refreshed["raw"]
    service_info = result["service_info"]
    golden_ticket_hours = load_settings(conn).get('goldenTicketHours', 1.5)

    print(f"Loaded {result['n_rows']} observations "
          f"({result['dropped_count']} dropped - no depTime or unparsable flightDate).")
    print(f"Built {len(service_info)} day-of-week-specific services (C1 and slope both pooled at this "
          f"granularity now - no separate cross-day grouping).")
    print(f"Wrote {refreshed['rowsWritten']} rows to declineCurveCoefficients, "
          f"{refreshed['instanceRowsWritten']} rows to declineCurveInstanceFits.")
    print()

    for cabin, cabin_data in result["by_cabin"].items():
        summary = refreshed["byCabin"][cabin]
        service_slopes = cabin_data["service_slopes"]
        slope_diagnostics = cabin_data["slope_diagnostics"]
        night_ratios = cabin_data["night_ratio_by_service_pooled"]
        night_ratio_diagnostics = cabin_data["night_ratio_diagnostics"]
        c1_by_service = cabin_data["c1_by_service"]
        fits_by_instance = cabin_data["fits_by_instance"]
        late_step = pool_late_step_changes(fits_by_instance, golden_ticket_hours)

        print(f"=== Cabin: {cabin} ===")
        print(f"  {summary['nInstances']} flight-day instances, {summary['nFit']} fit successfully.")
        instance_iterations = [f["iterations"] for f in fits_by_instance.values() if f is not None]
        if instance_iterations:
            print(f"  per-instance correction iterations: avg {np.mean(instance_iterations):.1f}, "
                  f"max {max(instance_iterations)}.")
        print(f"  {summary['nInstancesWithStepChanges']} instances had at least one step change corrected "
              f"({summary['nStepChangesTotal']} total corrections applied).")
        print(f"  {summary['nServicesWithSlope']}/{summary['nServicesTotal']} services got a resolvable slope, "
              f"{summary['nServicesWithNightRatio']}/{summary['nServicesTotal']} got a resolvable night ratio, "
              f"{summary['nServicesResolved']}/{summary['nServicesTotal']} services got a resolvable C1.")

        ranked = sorted(c1_by_service.items(), key=lambda kv: -kv[1][1])
        shown = 0
        for sid, (median_c1, n_instances) in ranked:
            if sid not in service_slopes:
                continue
            slope, gap, n_slope_instances = service_slopes[sid]
            org, dest, dow, rep_time, _ = service_info[sid]
            hh, mm = divmod(rep_time, 60)
            line = (f"  service {sid} ({org}-{dest} {dow} ~{hh:02d}{mm:02d}): "
                    f"slope={slope:.2f} seats/h (from {n_slope_instances} instances), "
                    f"gap={gap:.2f}h, C1 median={median_c1:.2f}h (from {n_instances} instances)")
            diag = slope_diagnostics.get(sid)
            if diag:
                line += f", slope-pooling: {diag['iterations']} iterations, RMSE={diag['rmse']:.2f}"
            night_entry = night_ratios.get(sid)
            if night_entry:
                night_val, n_night = night_entry
                line += f", nightRatio={night_val:.2f} (from {n_night} instances)"
                night_diag = night_ratio_diagnostics.get(sid)
                if night_diag:
                    line += f" [{night_diag['iterations']} iterations, RMSE={night_diag['rmse']:.2f}]"
            late_entry = late_step.get(sid)
            if late_entry:
                late_mean, late_rms, n_late = late_entry
                line += (f", late steps T-4..T-0.75: mean={late_mean:+.2f}, "
                         f"RMS={late_rms:.2f} (from {n_late} golden-ticket instances)")
            print(line)
            shown += 1
            if shown >= 10:
                break
        print()


if __name__ == "__main__":
    main()
