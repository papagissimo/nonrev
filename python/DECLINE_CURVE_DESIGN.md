# Decline Curve Fitting — What It Does

Two genuinely different fits happen here, not one. Keep them separate when
thinking about this.

## 1. Per-instance fit (`fit_instance`)

One (service, cabin, flight-day) at a time.

- Model: flat at 9 (bounded — "9 or more," not exact), linear decline, flat
  at 0.
- Real data has jumps (bookings, cancellations), not smooth noise. Any
  point — interior (1-8), or a 9/0 rail point — can get corrected in place:
  nudged to what it'd have been on a clean decline, worst-residual-first,
  repeated up to a settings-driven cap. Nothing is ever removed, just
  corrected; every correction is recorded (`step_changes`) and fed back
  unclamped (an implied 10 or -3 is real signal, not an error).
- The fitting window spans the first-seen-9 through the last-seen-0. A
  bounce back to 9 partway through gets corrected in place like any other
  step, not treated as two separate flights.
- Output: one c1 (leaves-9 corner) and one slope per instance, the
  corrected readings, and the list of what got corrected.

## 2. Pooled fit (`pool_slope` / `pool_night_ratio` / `pool_c1`)

Combines many instances of the SAME service into one number per service.

- Slope and night ratio are optimized, not averaged: a candidate value is
  scored by how well it would have predicted each instance's own last real
  reading, and the best-scoring candidate wins.
- C1 is just a median across instances — it barely matters, since the live
  estimator overrides it the instant a real reading comes in.
- This is a different operation from #1, not a bigger version of it:
  per-instance is a curve fit against one flight-day's raw wobbly data;
  pooled is a search for the single best shared value across many
  flight-days of the same service.

## 3. Late-step-change report (`pool_late_step_changes`)

Separate from both fits above. For each instance, sums whatever step
corrections landed in the last stretch before departure (T-4h to T-0.75h)
into one number, then reports the plain mean and RMS of that across every
instance that reached a real near-departure ("golden ticket") reading.
Answers "how much does this flight typically jump around right before a
buy decision," on top of what the smooth curve alone says.

## Console output

Running `python3 DeclineCurveFit.py` prints, per cabin: a summary, then a
ranked per-service list showing slope, gap, C1, night ratio, the late-step
stats above, and — when a pooled value actually came from an optimization
rather than agreement or a fallback — how many iterations it took and its
RMSE.

## Open questions / backlog

Tracked in `TODO.md`'s "Decline curve / predictor" section, not duplicated
here — one place for what's not done yet.
