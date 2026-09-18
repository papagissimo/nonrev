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
  unclamped.
- **What "unclamped" means and why it's fine**: a heavily-corrected point
  can legitimately end up outside the 0-9 range (an implied 11, an implied
  -2) once enough step-change activity gets folded in. That's not an
  error — the 0/9 rails are limits on what gets *observed* (a plane
  physically can't show more than 9 or fewer than 0 seats), not limits on
  what a corrected fitting point is allowed to represent. The 9 rail in
  particular obscures more than the 0 rail does: Delta knows the exact
  seat count all the way up to a fully empty plane, but the tool only ever
  sees "9 or more" at that end, so a real decline can be well underway
  above 9 before it's visible at all. Corrected values above 9 are
  reconstructing some of that hidden range, not inventing it. Below 0 is
  a softer case — there's no real seat count below zero — but a corrected
  negative is still meaningful as *pressure past full*, i.e. how much
  demand kept building after the plane was already closed out, not an
  artifact to suppress.
- **The model is not trying to reproduce the true shape of the decline.**
  It's trying to predict T-1 from T-4 (or whatever the anchor point is)
  with something repeatable and algorithmic. A single (c1, slope) line is
  a deliberately simple proxy for that, not an attempt to trace what
  actually happened seat-by-seat — the fit is judged by predictive
  usefulness, not by resemblance to the real curve.
- **A bounce-back-and-redecline is two real segments, not one real
  segment with a second one forced onto it.** A cancellation reopening
  seats back toward 9 mid-decline interrupts one continuous erosion into
  two pieces; step-change correction is what reassembles them into one
  fittable line — same mechanism as any other interior jump, not a
  special case.
- **Transition window (first-edge/last-edge, 2026-09-18):** anchored
  independently at each end, not by scanning the whole sequence for rail
  values. Start: if the instance's very first reading is a 9, keep only
  the single 9 immediately before its first-ever departure from 9 (drop
  the redundant leading flat run). If the first reading isn't a 9
  (logging picked up mid-decline), there's no edge to trim — the window
  starts at the first reading, period. End: symmetric — keep only the
  single 0 immediately after the last real decline into 0; if the most
  recent reading isn't a 0 (still declining, unresolved — including an
  instance that already hit 0 once, bounced back to 9, and is declining
  again as of the last check), there's no edge yet, so the window ends at
  the latest reading available. Everything *between* the two anchors
  stays in as real fitting/correction data untouched — interior values, a
  full bounce back to 9, a second decline, plateaus. Only the redundant
  flat time at the two ends gets trimmed, because that flat time
  measurably dilutes the fitted slope toward zero without adding any
  information (superseded the 2026-09-17 first-seen-9/last-seen-0
  version, which kept the full flat run at both ends).
  This doesn't guarantee a genuine two-segment bounce-back converges to
  one clean (c1, slope) line every time — it doesn't, and that's accepted:
  a fit gained on the instances where it does converge is a net win
  regardless of the instances where it still doesn't.
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
