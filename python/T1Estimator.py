"""
The live, curve-slide T1 estimator - the one T1 value shown anywhere in
the app (his call: he never wants to see two different T1 numbers side
by side). GraphObservations used to keep the old two-point method alive
as a separate t1Old/t1New comparison column - that was killed outright
once he confirmed he'd never actually used the comparison; GraphObservations
now calls this same module rather than its own copy of the math.

Per cabin, independently: take the pooled (c1, slope) for this exact
(org, dest, dayOfWeek, depTime, cabin) from declineCurveCoefficients
(see DeclineCurveFit.py). Find the most recent KNOWN reading for that
specific cabin - not necessarily the most recent reading overall, since
a "9, blank, blank" partial entry deliberately leaves other cabins
unlogged rather than implying zero (his call, carried through
consistently here rather than zero-filling the way the old two-point
method does).

If that reading is interior (1-8): well-determined, no ambiguity -
re-anchor (horizontal slide) the frozen curve through it exactly and
predict forward to T1_TARGET_HOURS.

If it's still at a rail (9 or 0): "still 9" only means the corner hasn't
happened YET, not where it actually is - it's genuinely ambiguous
whether this is unremarkable (matches what the pooled curve already
expected at this point) or a real surprise (the pooled curve expected
this cabin to already be moving/already empty by now, and it isn't/is).
His call: compare the reading against what the pooled curve ALONE
(no live data) would already predict at this same hoursBeforeDep. If
they're close, there's nothing new here - use the pooled curve as-is,
no slide (this is what fixes the original bug: an early "still 9"
reading that the pooled curve also expects to still be 9 shouldn't
trigger a slide that then wildly overshoots on a long extrapolation).
If they disagree by more than the configured threshold, it's a genuine
surprise ("unexpectedly early zero" or "unexpectedly late nine", his
terms, treated symmetrically) - slide anyway, using the same naive
anchor-at-reading-time assumption the interior case effectively uses
(c1 solves out to exactly the reading's own hoursBeforeDep for a
surprising 9, or hoursBeforeDep + 9/slope - i.e. the OTHER corner, c2,
lands right at the reading - for a surprising 0): his phrasing, "take
it out too as if it'll start declining just after that reading" /
"slides that c2 corner out to that first zero observation."

The "how much disagreement counts as a surprise" threshold reuses
declineCurveSettings' stepChangeRmseThreshold (same settings panel,
/pooling) rather than introducing a second tunable - same rough
statistical job (how many seats is a meaningful deviation), though a
different exact use (a single-point comparison here, an RMSE over many
points there). Worth revisiting if that turns out not to be the right
number for this particular purpose.

A cabin with no resolvable slope yet (brand-new service, or a schedule
change since the last refresh), never logged in the pool at all, or
stuck at a rail with no resolvable pooled C1 either (nothing to compare
against, so nothing to judge "expected" against - defaults to sliding,
since that's the only usable information left) contributes NOTHING to
the total, not zero, if truly nothing is resolvable. A row where every
cabin is unresolvable has no estimate at all (None), rather than a
misleading 0.

Two entry points: compute_t1_replay_column (above) needs at least one
reading to replay against; compute_t1_baseline (below) needs none at
all - the pooled curve alone, for a flight that hasn't been checked yet
today. Same coefficients lookup, same cabins, just no anchor to slide
on in the baseline case.
"""

from datetime import datetime

from DeclineCurveFit import piecewise_model, CABIN_COLUMNS
from DeclineCurveHierarchy import resolve_coefficients
from settings import load_settings, DECLINE_CURVE_SETTINGS_KEY, DEFAULT_DECLINE_CURVE_SETTINGS

T1_TARGET_HOURS = 1.0

CABIN_KEY_TO_COLUMN = {'y': 'y', 'cplus': 'cPlus', 'onePS': 'firstOrPS', 'd1': 'd1'}


def resolved_actual_or_raw_cheap(actual, cheap):
    """
    The one per-cabin reading-resolution rule for anchoring this
    estimator (his call, superseding the earlier FloorEstimates-based
    substitution): real actual (binary-search-confirmed) value if
    present, else the raw cheap-glance floor value itself, unmassaged,
    if that's all there is - NOT run through FloorEstimates' conditional-
    mean substitution. A confirmed "at least 3" floor glance is fed in
    as a plain 3, which will understate the true value whenever the real
    count is higher - an accepted, deliberate trade (real numbers matter
    more to the fit than a laundered decimal guess the fit never actually
    observed), not an oversight. None if neither was logged - the caller
    treats that cabin as unresolved, not zero (see this module's own
    docstring). Shared by every caller that constructs readings for this
    estimator (SeatLoggingDialog, GraphObservations) - the resolution
    rule itself, not just the slide math, only gets to exist in one
    place.
    """
    if actual is not None:
        return actual
    if cheap is not None:
        return cheap
    return None


def load_decline_curve_coefficients_for_flight(conn, org, dest, day_of_week, dep_time):
    """dict cabin_column -> (c1Hours, slopeSeatsPerHour), one entry per
    cabin - now resolved through the full coefficients hierarchy (see
    DeclineCurveHierarchy.resolve_coefficients): derived data from
    declineCurveCoefficients when there's enough of it, else a hand-set
    service or route override, else the global default. A cabin only
    ever comes back missing from this dict if EVERY tier including the
    global default has no slope for it - practically shouldn't happen
    once the global default is filled in, but not assumed away."""
    result = {}
    for cabin_col in CABIN_COLUMNS:
        resolved = resolve_coefficients(conn, org, dest, day_of_week, dep_time, cabin_col)
        if resolved['slope'] is not None:
            result[cabin_col] = (resolved['c1'], resolved['slope'])
    return result


def compute_t1_replay_column(conn, org, dest, flight_date, dep_time, readings):
    """readings: as returned by SeatLoggingDialog.previous_readings_for -
    most-recent-first (ascending hrs), each {'hrs':, 'y':, 'cplus':,
    'onePS':, 'd1':}, values already resolved (real actual, or a
    FloorEstimates-derived decimal glance estimate) or None if that
    cabin wasn't touched on that particular check.

    Returns a list the same length as readings - one curve-slide T1
    estimate per row, replay-style: computed using only that row and
    everything CHRONOLOGICALLY EARLIER (readings[idx:], since the list
    counts down in hrs) - matches the existing two-point T1 column's own
    replay semantics (see SeatLoggingDialog.html's now-removed
    formatT1Column/computeT1EstimateForPool, which this supersedes as
    the one T1 shown in the dialog).

    See module docstring for the per-cabin carry-forward, the rail
    expected-vs-unexpected slide decision, and the missing-means-omit-
    not-zero rule."""
    day_of_week = datetime.strptime(flight_date, "%Y-%m-%d").strftime("%a")
    coeffs = load_decline_curve_coefficients_for_flight(conn, org, dest, day_of_week, dep_time)
    if not coeffs:
        return [None] * len(readings)

    settings = load_settings(conn, key=DECLINE_CURVE_SETTINGS_KEY, defaults=DEFAULT_DECLINE_CURVE_SETTINGS)
    unexpected_threshold = settings['stepChangeRmseThreshold']

    results = []
    for idx in range(len(readings)):
        pool = readings[idx:]  # this row + everything chronologically earlier
        total = 0.0
        any_resolved = False
        for cabin_key, cabin_col in CABIN_KEY_TO_COLUMN.items():
            c1_slope = coeffs.get(cabin_col)
            if c1_slope is None:
                continue
            pooled_c1, slope = c1_slope

            anchor = None
            for row in pool:
                v = row.get(cabin_key)
                if v is not None:
                    anchor = (row['hrs'], v)
                    break
            if anchor is None:
                continue

            hbd_r, val_r = anchor
            if 1 <= val_r <= 8:
                # Interior reading - well-determined, solve for the C1
                # that makes the curve pass through it exactly.
                c1_prime = hbd_r + (9.0 - val_r) / slope
            elif pooled_c1 is None:
                # Rail reading, but nothing to compare it against -
                # slide anyway, it's the only information available.
                c1_prime = hbd_r + (9.0 - val_r) / slope
            else:
                pooled_val_at_hbd_r = float(piecewise_model(hbd_r, pooled_c1, slope))
                if abs(val_r - pooled_val_at_hbd_r) > unexpected_threshold:
                    # Unexpected - genuinely surprising given the pooled
                    # curve, slide on it (his terms: "unexpectedly early
                    # zero" / "unexpectedly late nine").
                    c1_prime = hbd_r + (9.0 - val_r) / slope
                else:
                    # Expected - the pooled curve already explains this
                    # reading, nothing new to slide on.
                    c1_prime = pooled_c1

            total += float(piecewise_model(T1_TARGET_HOURS, c1_prime, slope))
            any_resolved = True

        results.append(total if any_resolved else None)
    return results


def compute_t1_baseline(conn, org, dest, flight_date, dep_time):
    """
    The T1 estimate before a single reading has been logged today for
    this flight - the pooled curve alone, evaluated at T1_TARGET_HOURS,
    with nothing to anchor/slide against yet (his call: he wants to see
    where a flight is expected to land the moment it shows up as a
    candidate, not only once he's checked it at least once - the pooled
    coefficients don't care whether today has any data).

    Same coefficients lookup as compute_t1_replay_column (same
    hierarchy, same cabins), just with no reading to anchor on - every
    cabin that resolves both a c1 and a slope contributes
    piecewise_model(T1_TARGET_HOURS, c1, slope) directly, summed.
    Returns None only if no cabin resolves both - should essentially
    never happen once the global default tier is filled in (see
    DeclineCurveHierarchy), same edge case compute_t1_replay_column
    already accepts.
    """
    day_of_week = datetime.strptime(flight_date, "%Y-%m-%d").strftime("%a")
    coeffs = load_decline_curve_coefficients_for_flight(conn, org, dest, day_of_week, dep_time)
    if not coeffs:
        return None
    total = 0.0
    any_resolved = False
    for c1, slope in coeffs.values():
        if c1 is None:
            continue
        total += float(piecewise_model(T1_TARGET_HOURS, c1, slope))
        any_resolved = True
    return total if any_resolved else None
