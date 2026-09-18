# TODO

Backlog of decided-but-not-built work. Edit in place, don't just append — when
something's done, delete it (git history has the record). When an idea gets
killed, move the one-liner to "Dead ideas" instead of just deleting it, so it
stops getting re-proposed. This doc reflects current thinking only; if
something here doesn't match your current thinking, cut or fix it rather than
working around it.

## Core dialogs / SQLite rewrite

- **hoursBeforeDep computed-live for TODAY only (not stored, or blindly
  always-live either) — refined 2026-09-16, still fully open.** Orphan
  DETECTION itself is built (schedule-edit save diffs the route+day's
  depTimes before vs. after; any depTime present before but missing
  after is surfaced as an orphan) - this is the separate, bigger piece.
  The recovered handoff doc's original design said "stop storing
  hoursBeforeDep, always compute live from checkTimestamp + current
  depTime" - discussed further and that's WRONG as stated: a service's
  schedule genuinely wobbles week to week (a ~9:30am departure might be a
  few minutes different next week), and he's explicit he has no interest
  in reconstructing Delta's schedule history - so recomputing an OLD
  reading's hoursBeforeDep against TODAY's schedule would silently
  falsify history, not heal it. The actual rule, confirmed with him:
  - flightDate == today: compute live, from checkTimestamp + CURRENT
    depTime (same-day schedule corrections are real and should be
    reflected in same-day Prev-column lookups).
  - flightDate in the past: NEVER touch live flightSchedule. The value
    frozen at logging time already IS the correct historical fact - it's
    what was true when that reading was taken, not "Delta's schedule
    that day." Recomputing it against today's schedule would be actively
    wrong, not a fix.
  - Practically: keep storing hoursBeforeDep exactly as today for
    already-logged rows (already correct, permanent once the flightDate
    passes). Add a live-computed path ONLY for today's-flightDate
    same-day lookups (Prev column). Curve fitting, GraphObservations.py, and
    ServiceGrouping.py's pooled open/full counts keep reading the stored
    value unchanged - they work across many past flightDates, exactly
    the case that must never touch live schedule. Not started.
- **New idea, not yet designed: scan HISTORY for orphans that predate
  the detection fix above.** Detection only catches a schedule edit
  going forward from whenever it runs - anything orphaned before that
  existed is still sitting wrong in already-logged data, uncorrected,
  and nothing has ever gone looking for it. Separate piece of work from
  live detection - independent of everything else on this list.
- **confirmedAirports gate not yet in FlightScheduleDialog** — the
  confirm-before-trusting-a-timezone check (confirm_airports.py) only
  runs from the logging-entry flow. FlightScheduleDialog is the more
  natural place a genuinely new route/airport gets added, but doesn't
  run the check yet. Not urgent.
- **Delayed-flight departure time** — very low priority. Doesn't come up
  often enough in practice to be worth designing for. Leave alone until it
  actually becomes a problem.

## Service identity (raw depTime vs. clustered service) — CLOSED 2026-09-16

This resurfaced across multiple sessions as a real, recurring frustration.
Closed properly this session, not just patched again - audited all 14
files in python/ touching depTime, found and fixed the two real remaining
gaps (DeclineCurveHierarchy's tier-3 service override and tier-4
threshold gate, same exact-match-only pattern tier 4 itself had before
this session - all three now share one helper, `_exact_or_nearest_row`),
regrouped both visibility views (DeclineCurveDialog, ShowServiceDetail.py)
by clustered service. Checked `get_next_batch` specifically - it does NOT
need a per-service lookup; the belief that it did was carried over from
the OLD corner-chasing cadence design (already dead, see Dead ideas
below) - get_next_batch itself is fully self-contained per flight-day.

Raw depTime correctly stays exact and untouched by any of this in
FlightScheduleDialog.py (schedule entry) and ObservationsBrowser.py
(browsing real logged rows) - those are legitimately per-exact-flight
tools, not pooling consumers, and he needs the raw value there to
cross-check against Delta's site and for T-45min cutoff math.

Don't re-raise this topic on general principle just because it's come up
before - a NEW, specific symptom is the bar for reopening it.

## Decline curve / predictor

- **New idea, not yet designed: time-to-full is its own signal, separate
  from decline slope/C1/night-ratio.** A service that crosses the "full"
  threshold (currently 2, not literally 0) very early versus one that
  crosses it very late are different animals worth telling apart -
  currently nowhere calculated or captured. This is squarely an
  open/full-count question (ServiceGrouping.py), not a decline-curve
  one. Would need its own corner-solving math aimed at the full
  threshold rather than 9/0, similar in spirit to the existing C1 solve.
  Precision/exact-threshold quibbling between "full" and "really quite
  thoroughly full" is explicitly NOT worth resolving - a rough distinction
  is all that's wanted here.
- **Backtest should report the SUMMED-across-cabins number, not just
  per-cabin.** The per-cabin leave-one-out T4→T1 backtest (T4T1Backtest.py,
  built 2026-09-15) is structured correctly, but the real number he'll
  actually use on game day is the sum across cabins (Y + C+ + 1/PS, +D1
  where relevant) - that's the actual quantity a go/no-go decision is made
  on. Needs a summed-residual version alongside (or instead of) the
  per-cabin breakdown.
- **New feature idea, not yet designed**: some services are "usually open"
  but not always - if the T-4 backtest can distinguish the specific
  anomalous closed instance from the normal-open ones (rather than just
  reporting an aggregate spread), that's a real, valuable signal for
  exactly those flights where the aggregate historical spread alone
  wouldn't tell him today is different. Worth thinking through how to
  surface this - flagged as "extremely useful" if it can be made to work.
- Golden-ticket window analysis: analyze historically how far out from
  departure the estimator still reliably predicts the eventual T1 value -
  i.e. at what hours-before-departure does it stop being trustworthy. That
  threshold, not a flat 1.5h guess, should set the golden-ticket window; a
  flight with a good/stable estimator might only need checking 3h out, an
  erratic one might still need the tight window. Delta's own cutoff is
  T-45min (not T-30) - the hard floor either way. Real interest, not
  today's task. (Explicitly NOT wanted: a separate "analyze how often C1
  comes back right-censored" study - a right-censored C1 just means the
  flight is open, full stop, no further analysis needed there.)
- Step-change sequence graph moved to the new Graphing section below.
- **Near-zero slope from thin data (e.g. a same-valued, short-elapsed
  gap) — decided 2026-09-17: NOT a bug, no fitting-logic change wanted.**
  Resolves to near-zero (not literally infinite - checked empirically),
  which is an honest low-confidence answer from thin data, consistent
  with his standing no-arbitrary-minimums rule elsewhere in this project -
  gating it would fake more certainty than the data supports. The ONE
  real fix: the console's `gap = 9.0/slope` diagnostic print can show a
  meaningless huge number when this happens - confirmed that value is
  display-only, never persisted or used in any live prediction - so
  either drop it from the print or floor/cap its display, cosmetic only.

## Verdicts / classification / logging UI

- Verdict-text granularity not settled: floated a scheme with "full so
  far"/"open so far" (fewer than 3 good observations, at least 1) versus
  plain "full"/"open" (3+, unanimous), plus a possible "middling"/
  "middling so far" tier for real spread. Wants to test against real
  service data before committing to any of it.
- Second glyph for "stop looking, this one's a lock" (distinct from
  gold-star's "open so far" and green-check's "looking good, keep
  watching") — verdictType still only has info/warning/axed/starred in the
  live schema; not yet designed or added. Not ready to work on this yet.
- Alternating row background bands in the logging dialog (one flight's
  batch vs. the next) for readability - currently all-white, hard to
  track where one flight ends and the next begins. Undecided between
  plain white/light-gray or white/light-green (he has a green already in
  the palette) - his call when it's picked up.

## Graphing — low priority, not actively working this area right now

- T1 weekday bar chart (Mon-Sun per service) — mockup approved, still not
  built. GraphObservations.html currently has three other charts (heat
  map, per-date curves, seats-vs-hours-to-departure) but not this one.
  Approved spec:
  - Gray banding between adjacent weekday groups; weeks aligned
    left-to-right consistently across every weekday's group (same week
    index = same calendar week no matter which day).
  - One shared fixed y-axis across all such charts, capped at 20.
  - Minimum bar size (~0.5 magnitude, straddling zero) so true-zero and
    near-zero readings stay visible instead of collapsing to an
    invisible sliver — doesn't matter whether the sliver sits slightly
    above or below zero.
  - Ceiling glyph: stacked chevrons above a bar, same color as the bar
    (not a separate color), one chevron per seat-class reading that hit
    the 9-seat ceiling feeding that estimate — scales to 2 or 3 stacked.
  - No-observation slot: dim/translucent ghost bar in the same color as
    real bars, height = that weekday's average across whichever OTHER
    weeks have data (not an average across other days within the
    missing week), with a gray "?" centered in it; the "?"'s opacity
    fades as its value approaches the top of the y-axis (never fully to
    zero), not fading within the bar's own height.
- Step-change sequence graph: sequence of detected step changes over time
  (seats up/down, when each occurred) - also a way to quantify how "jumpy"
  a service is, comparable across day-of-week/season/service.

## Forward-looking schedule import (blocked on him, not stuck)

- **Goal**: replace manual FlightSchedule/AircraftConfigs entry (clicking
  into Delta's site per flight) with a script pulling scheduled dep time,
  day-of-week, and aircraft type from an external source. Only wants this
  1-3 days out, ~90% accuracy is fine — does not care about last-minute
  equipment swaps or delays, only what's scheduled. Matters to him - an
  ongoing logging headache and a good candidate for automation.
- **Ruled out, with reasons** (don't re-propose):
  - OAG/Cirium (the real schedule databases) — enterprise-priced, not
    self-serve.
  - BTS Airline On-Time Performance (free government data) — rejected:
    it's historical/actuals with a ~3-month reporting lag, not
    forward-looking. Fails the actual requirement even though it's free
    and ToS-clean.
  - Scraping a schedule-display site (tested FlightAware — bot-blocked
    immediately; FlightConnections worked for one manual fetch but is a
    commercial site with its own ToS) — same risk profile already
    rejected for automating Delta.com checks. Confirmed data of the right
    shape (dep time + aircraft type, forward-looking) exists there, just
    not legally automatable.
- **Chosen direction**: AeroDataBox via RapidAPI, free "Basic" tier — 600
  API units/mo, 2,400 requests/mo, 1 req/sec, claims 100% US schedule
  coverage. Schedule endpoint returns up to 7 days of an airport's flights
  per call, so plan is to query **per origin airport** (~6-8 airports),
  not per route (~26), to stay well inside the free quota even run daily.
  Filter results client-side to Delta + his tracked destinations, map
  into FlightSchedule (depTime, dayOfWeek) and AircraftConfigs (aircraft
  type).
- **Not yet done**: he hasn't signed up for a RapidAPI/AeroDataBox account
  or gotten a key (has to be him, not Claude) — the one and only blocker,
  not a design or feasibility problem. No fetch/parse code written yet.
  Exact API-unit cost per call for the specific schedule endpoint is
  unconfirmed — check once a key exists.
- **Status note (2026-09-11)**: he's deliberately pausing manual aircraft
  data entry in the meantime, expecting this script to eventually take
  over that part.

## Someday / not started, low priority

- Long-haul Delta One analysis (Hawaii, Tokyo, New Zealand, Australia) —
  distinct from the West Coast commuter focus so far; needs per-cabin
  breakdowns tied to actual aircraft config, since a ceiling of 9 means
  something very different on a small commuter Comfort+ cabin vs. a
  wide-body one.
- Two-directional night ratio (a separate overnight-at-origin vs.
  overnight-at-destination rate, instead of one shared night ratio) —
  his own idea, his own assessment: collapses to the same thing on
  north-south domestic routes, would only actually diverge on true
  east-west long-haul (LA-Australia was his example). Not worth building
  now - not enough data, and he doubts it moves the needle much even with
  more. Worth a backtest comparison someday to check whether it's real
  before doing anything else with it.

## Dead ideas — do not re-propose

- Bounce-back-to-9 handling via splitting affected readings into
  separate (service, flightDate, excursion) instance keys before
  fit_instance — considered, then superseded 2026-09-17 by widening
  window_bounds and bracket_idx (every 9/0-valued point in the window,
  not just the edges) so the existing correction loop handles a
  bounce-back in place, without a second algorithm or a pre-fit
  splitting pass. Splitting itself is still not being reconsidered. The
  2026-09-17 widening (first-seen-9 to last-seen-0) was found incomplete
  2026-09-18 — mis-anchored when no 9 preceded a dip, and diluted the
  fitted slope with redundant flat-rail time — and fixed the same day
  with first-edge/last-edge anchoring instead (see
  DECLINE_CURVE_DESIGN.md); fully dead now.
- Automated tiered cadence/eligibility engine in get_next_batch
  (settings.py's tiers, recheckGapHours, evaluate_eligibility) — removed
  2026-09-16. His real workflow for the past month has been walking every
  scheduled flight in departure order each session, logging or blank-
  skipping each in turn; the tier engine wasn't doing that job, and its
  near-zero recheck gap let an already-logged flight silently cut back in
  line ahead of ones not yet reached, starving several routes for a whole
  evening. Replaced with a plain session-scoped handled-set (logged or
  blank-skipped this session, cleared on reload) — no eligibility math at
  all. goldenTicketHours (a display flag, not a gate) is untouched.
- GraphObservations' t1Old/t1New side-by-side (old two-point method vs.
  floor-substituted variant) comparison feature — never actually used it;
  GraphObservations now calls the same curve-slide estimator
  (T1Estimator.compute_t1_replay_column) SeatLoggingDialog already uses,
  one estimator only.
- Feeding cheap-glance data through FloorEstimates' conditional-mean
  substitution to anchor the T1 estimator — real actual value or the raw
  cheap-glance value itself now, never a laundered decimal guess.
- Floor-plug-in coefficients table (per-floor conditional mean of
  actual-given-floor) and its Beta-distribution refinement — superseded;
  FloorEstimates.py and all glance-derived estimation fully retired
  2026-09-14, and the cheap-glancing habit it depended on is already
  dead below.
- Cheap-glancing (as a cadence habit) for the purpose of catching C1/C2
  corners — the curve fit doesn't need the corner observed at all
  (left/right-censored instances fit fine from interior data alone);
  precise interior (1-8) readings are what it actually needs, and cheap
  glances were displacing the time to get those. Cadence window-start for
  previously-observed C1 (searching for where a service's corner
  happened, to aim cadence at it) is dead for the same reason — nothing
  needs to search for a corner anymore.
- Automating overnight Delta.com checks (scripted page loads, VPN/bot
  variant) — ToS/detection risk.
- Cadence-from-slope, curve-shape decimation — real oversampled data showed
  unpredictable discrete jumps, not noise around a fittable curve.
  (T-4-predicts-T-1 modeling specifically is NOT dead - revived
  2026-09-15/16, real usable signal found via leave-one-out backtesting;
  removed from this kill list accordingly.)
- Step-change correction propagate-forward-vs-single-point question —
  moot, superseded by the two-stage fit rewrite (2026-09-15/16), which
  refits slope/night-ratio/C1 from scratch each correction pass rather
  than patching a correction forward through later readings.
- Three wide-spread route clusters (dtw-cvg, cvg-dtw, slc-pdx) needing an
  eyeball check before locking in a clustering threshold — not worth the
  attention given ~7000 observations; clustering has held up reliably
  every time it's actually been checked.
- Automated full/open/iffy classification via fixed thresholds (sum of
  4 cabins, needs >=3 qualifying dates, unanimous <=2 full />=8 open,
  writing into a `classification` column keyed on flightNumber+dayOfWeek)
  — superseded by the free-text `verdict` + `verdictType` glyph system
  (info/warning/axed/starred) actually in the live schema. Confirmed no
  `classification` column exists anymore. Classification is a human
  judgment call now, not an automated write.
- 3-unanimous-readings verdict threshold — repeatedly blocked acting on
  visible patterns, killed outright.
- Skip-a-route-to-avoid-rechecking-it-because-it's-full — obsolete, he
  watches full flights himself.
- Persisted `services` table — live clustering via reconstructed
  departure time is cheap enough to redo on the fly instead. (This is
  about NOT persisting a services table - it does not mean service-based
  clustering itself is deprioritized; see the "Service identity" section
  above, which is the opposite of dead.)
- Corridor graph for the T1 curve — no clear shape for it yet, not wanted
  now.
- `scheduleRowId` surrogate-key linking observations to flightSchedule
  rowid — superseded by the hoursBeforeDep-hardening design; also fragile
  against real schedule churn.
- Value-mangling a stored flightNumber value (e.g. trailing
  `__SEE_COLUMN_NAME_DO_NOT_USE` suffix) — the renamed column alone is
  sufficient warning.
- `cascade_flight_number_rename` / `recompute_hours_before_dep` — already
  fully removed from the codebase (found deleted in a subsequent refactor,
  no replacement); superseded by the hoursBeforeDep-hardening design
  above.
