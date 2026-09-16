# TODO

Backlog of decided-but-not-built work. Edit in place, don't just append — when
something's done, delete it (git history has the record). When an idea gets
killed, move the one-liner to "Dead ideas" instead of just deleting it, so it
stops getting re-proposed. This doc reflects current thinking only; if
something here doesn't match your current thinking, cut or fix it rather than
working around it.

## Core dialogs / SQLite rewrite

- **hoursBeforeDep orphan-fix — revive.** Real priority - keeps turning up
  during actual logging and is misleading when it does. The shipped design
  (each observation freezes its own depTime/hoursBeforeDep at logging time,
  schedule edits never touch already-logged observations) doesn't hold up
  in practice — confirmed 2026-09-10 when editing a schedule row ~2.5 min
  after logging against it produced a real orphaned reading. Accepted-risk
  call is retracted. Build the original parked design instead: on
  schedule-edit save, diff the route+day's depTimes before vs. after the
  edit; any depTime present before but missing after is an unambiguous
  orphan, surfaced (count + old time) for delete/manual reassign — no
  fuzzy matching needed. See
  `HANDOFF_rationalwaytoeditfltschedandfixorphans.md` for the original
  design (whether hoursBeforeDep itself should also move to computed-live
  rather than stored is part of that same doc — worth revisiting the full
  scope, not just the orphan-surfacing piece).
- **Delayed-flight departure time** — very low priority. Doesn't come up
  often enough in practice to be worth designing for. Leave alone until it
  actually becomes a problem.

## Service identity (raw depTime vs. clustered service) — NOT dead, keep pushing

This keeps resurfacing session after session and he's explicit: raw
departure time and flight number have both proven unreliable as identity
anchors; a clustered SERVICE (org, dest, dayOfWeek, banded depTime) is the
one thing that's held up rock-solid every time it's been checked. The goal
is for every consumer to key on service, not raw depTime, consistently -
this is NOT about adding a persisted `services` table (that idea stays
killed, see Dead ideas below) - service identity stays a live, computed
value, recomputed via clustering.py whenever needed. What's actually wanted:
- Raw depTime keeps getting logged on every observation, unchanged - he
  needs it to cross-check against Delta's own site while logging, and
  T-45min cutoff math needs the real scheduled time, not a rounded band
  representative.
- But every consumer that currently keys lookups/aggregation on exact
  depTime should key on the clustered service instead, wherever that's not
  already true.
- Fixed this session (2026-09-16): DeclineCurveHierarchy.resolve_coefficients
  now falls back to nearest-depTime-in-cluster on an exact-match miss;
  DeclineCurveDialog's visibility view now groups by clustered service
  instead of listing raw depTime rows separately.
- Still NOT done: cadence (`get_next_batch`) doesn't have per-service
  earliest-checked/earliest-crossing lookups yet - undecided whether that's
  live clustering inline or something else. Audit remaining consumers for
  the same raw-depTime-vs-clustered-service gap before considering this
  closed - don't assume anywhere is fixed without checking.

## Decline curve / predictor

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

## Verdicts / classification / logging UI

- Second glyph for "stop looking, this one's a lock" (distinct from
  gold-star's "open so far" and green-check's "looking good, keep
  watching") — verdictType still only has info/warning/axed/starred in the
  live schema; not yet designed or added. Not ready to work on this yet.

## Graphing — low priority, not actively working this area right now

- T1 weekday bar chart (Mon-Sun per service, gray banding, chevrons for
  ceiling reads, ghost bar for no-observation days) — mockup approved,
  still not built. GraphObservations.html currently has three other charts
  (heat map, per-date curves, seats-vs-hours-to-departure) but not this
  one.
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

## Dead ideas — do not re-propose

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
