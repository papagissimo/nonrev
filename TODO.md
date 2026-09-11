# TODO

Backlog of decided-but-not-built work. Edit in place, don't just append — when
something's done, delete it (git history has the record). When an idea gets
killed, move the one-liner to "Dead ideas" instead of just deleting it, so it
stops getting re-proposed. This doc reflects current thinking only; if
something here doesn't match your current thinking, cut or fix it rather than
working around it.

## Core dialogs / SQLite rewrite

- **hoursBeforeDep orphan-fix — revive.** The shipped design (each
  observation freezes its own depTime/hoursBeforeDep at logging time,
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
- **Cache-busting** for dialog HTML/JS — edits currently require an incognito
  window to see changes on reload. Confirmed still absent from server.py.
- **Delayed-flight departure time, open question**: how Delta's own site
  shows a delayed flight's departure time (original scheduled vs.
  updated/delayed), and how that should interact with treating departure
  time as part of a flight's identity. Not designed yet.

## Decline curve / predictor

- **Wire the new curve-fit estimator into the live T1 display.**
  `DeclineCurveFit.py` already does the real work — per-instance
  `scipy.optimize.curve_fit`, pooled into per-service C1 and per-group
  slope — but it's a standalone script that only prints to console.
  `GraphObservations.compute_t1_estimate` (what the dialogs actually show)
  still runs the old two-point trajectory extrapolation and has no
  connection to this at all. This is the actual next step, not a from-
  scratch build.
- C1 right-censoring / golden-ticket window: analyze historically how far
  out from departure the estimator still reliably predicts the eventual T1
  value — i.e. at what hours-before-departure does it stop being
  trustworthy. That threshold, not a flat 1.5h guess, should set the
  golden-ticket window; a flight with a good/stable estimator might only
  need checking 3h out, an erratic one might still need the tight window.
  Delta's own cutoff is T-45min (not T-30) — the hard floor either way.
  Needs the live estimator wired in (above) before this can be run.
- Cadence window-start for previously-observed C1: probably moot — once
  cadence is driven by the nonlinear fit instead of needing to catch a live
  C1 crossing, there's no reason to search for where C1 happened at all.
  Leaving open pending confirmation from real data, expecting to just
  delete it once that's checked.
- "Service" isn't a persisted/queryable entity yet — only exists via
  ad-hoc clustering in DeclineCurveFit.py. Cadence needs per-service
  earliest-checked/earliest-crossing lookups; undecided whether that's live
  clustering inside `get_next_batch` or a first-class schema entity.
- Step-change handling, open question: when correcting a flagged
  step-change point by an integer seat delta and refitting, should the
  correction propagate forward to every later reading in that instance, or
  apply only to the single flagged point? Deferred.
- Future graph: sequence of detected step changes over time (seats up/down,
  when each occurred) — also a way to quantify how "jumpy" a service is,
  comparable across day-of-week/season/service.
- Floor-plug-in coefficients table (per-floor-level empirical conditional
  mean of actual-given-floor, per-cabin) — design settled, not built.
  Refresh via manual launcher button + incidental refresh at server
  startup.
- Asymmetric Beta-distribution refinement of the floor plug-in (mean +
  concentration via scipy) — designed, deferred further out than the
  coefficients table above.
- **Watch item, not a task**: does the new floor-plug-in predictor make
  T12/T4/T1 estimates correlate better than they have historically? Worth
  noticing once the coefficients table is live, not something to build
  toward directly.

## Verdicts / classification / logging UI

- Second glyph for "stop looking, this one's a lock" (distinct from
  gold-star's "open so far" and green-check's "looking good, keep
  watching") — verdictType still only has info/warning/axed/starred in the
  live schema; not yet designed or added.
- Second "previous readings" block showing last few times a flight was
  read with final resolved values *across days*, not just today — the
  existing Prev column (`previous_readings_for`) is scoped to one
  flightDate; a cross-date version for seeding the binary search doesn't
  exist yet.
- Three route clusters (dtw-cvg, cvg-dtw, slc-pdx) showed unusually wide
  36-65 min intra-cluster spread in the service-clustering validation
  query — needs an eyeball check to confirm these aren't actually two
  merged services before locking in a clustering threshold. Unclear if
  you've already looked at this since — flag if so.

## Graphing

- T1 weekday bar chart (Mon-Sun per service, gray banding, chevrons for
  ceiling reads, ghost bar for no-observation days) — mockup approved,
  still not built. GraphObservations.html currently has three other charts
  (heat map, per-date curves, seats-vs-hours-to-departure) but not this
  one.

## Excluded date ranges

- The two real ranges (three-day-weekend period, early-August anomaly
  window) are entered.
- Still open: wiring `is_date_excluded`/`excluded_date_where_clause` into
  actual pooling consumers — verdict/classification, decline-curve
  fitting, the weekday chart, dayGroupings. Nothing calls them yet.

## Forward-looking schedule import (not started, on hold)

- **Goal**: replace manual FlightSchedule/AircraftConfigs entry (clicking
  into Delta's site per flight) with a script pulling scheduled dep time,
  day-of-week, and aircraft type from an external source. Only wants this
  1-3 days out, ~90% accuracy is fine — does not care about last-minute
  equipment swaps or delays, only what's scheduled.
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
  or gotten a key (has to be him, not Claude). No fetch/parse code written
  yet. Exact API-unit cost per call for the specific schedule endpoint is
  unconfirmed — check once a key exists.
- **Status note (2026-09-11)**: he's deliberately pausing manual aircraft
  data entry in the meantime, expecting this script to eventually take
  over that part.

## Someday / not started

- Long-haul Delta One analysis (Hawaii, Tokyo, New Zealand, Australia) —
  distinct from the West Coast commuter focus so far; needs per-cabin
  breakdowns tied to actual aircraft config, since a ceiling of 9 means
  something very different on a small commuter Comfort+ cabin vs. a
  wide-body one.

## Dead ideas — do not re-propose

- Automating overnight Delta.com checks (scripted page loads, VPN/bot
  variant) — ToS/detection risk.
- Cadence-from-slope, curve-shape decimation, T-4-predicts-T-1 modeling —
  real oversampled data showed unpredictable discrete jumps, not noise
  around a fittable curve.
- 3-unanimous-readings verdict threshold — repeatedly blocked acting on
  visible patterns, killed outright.
- Skip-a-route-to-avoid-rechecking-it-because-it's-full — obsolete, he
  watches full flights himself.
- Persisted `services` table — live clustering via reconstructed
  departure time is cheap enough to redo on the fly instead.
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
