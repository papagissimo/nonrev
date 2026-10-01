# TODO

Backlog of decided-but-not-built work. Edit in place, don't just append — when
something's done, delete it (git history has the record). When an idea gets
killed, move the one-liner to "Dead ideas" instead of just deleting it, so it
stops getting re-proposed. This doc reflects current thinking only; if
something here doesn't match your current thinking, cut or fix it rather than
working around it.

## Core dialogs / SQLite rewrite

- **Retire observations.hoursBeforeDep — not started.** checkTimestamp is
  a reading's time of record; hoursBeforeDep is a stored copy derived from
  it. Every row already carries what it takes to derive it (checkTimestamp,
  flightDate, org, and its own depTime, kept converged by
  deptime_convergence.py), so dropping the column loses nothing.
  - Derive from the row's OWN depTime, never today's flightSchedule. A
    service's time wobbles week to week (domainKnowledge.md, "Schedule
    volatility"), so measuring a past reading against today's schedule
    falsifies it.
  - Today's readings: CURRENT depTime, matched to the schedule row by
    clustering (clustering.cluster_services), not exact equality, so a
    same-day schedule correction doesn't drop them. Already built in
    SeatLoggingDialog.py's previous-readings lookup; the past-date branch
    right beside it still reads the stored column.
  - Still reading the stored column: DeclineCurveFit.py, SeatLoggingDialog.py,
    deptime_convergence.py, ServiceGrouping.py, T1Estimator.py,
    T1GridReport.py, ObservationsBrowser.py/.html, FlightScheduleDialog.py,
    backfill_deptime_convergence.py, GraphObservations.py (deprecated -
    decide whether to update it or let it break), create_db.py.
  - insiderReadings already stores checkTimestamp only.
- **Delayed-flight departure time** — very low priority. Leave alone until
  it actually becomes a problem.
- **Aircraft entry in FlightScheduleDialog — not yet discussed.** Its
  aircraft field is still free text with a suggestion list, which is how
  keys with no aircraftConfigs row got into flightSchedule (crj900, crj700,
  a321). The seat-map panel handles those (its "add sizes" fills the
  missing row); whether the schedule dialog should become a real pick list
  too is open.
- **Seat-map modeling** — when the selectable counts get modeled, key off
  time; a seat-map reading needn't share a can-buy reading's timestamp.

## Weekdays: what gets logged and what gets pooled

- **Scenarios screen does two jobs**: a scenario's days mean "days I might
  fly this trip," but they are also the only way to say "days I bother
  logging this route" (e.g. log Tuesday on some routes and Wednesday on
  others, since Tue and Wed look alike). Logging covers the union of all
  active scenarios, so any scenario can quietly re-add a skipped day, and
  getting it right means cross-checking every scenario that touches a route.
  Candidate direction, not agreed: scenarios keep only the days-I-might-fly
  job, and a separate per-route setting picks which of Tue/Wed gets logged
  (default both). Open: whether a Tue question on a Wed-logged route should
  then read Wed data.
- **Weekday pooling in the logging dialog — direction decided, not built.**
  dayGroupings pools the O/F counts and the history range per route. Its
  current labels (Tue/Wed/Thu together, Mon/Fri together) were an early
  guess and are wrong: Thursday is the fullest weekday on several services.
  Nothing else reads dayGroupings; decline-curve fitting, grading and the
  T1 grid all key on the exact weekday.
  - Direction: pool days by the fleet-wide weekday pattern (findings.md,
    2026-09-29), and break a service out only when it's glaringly
    different. Per-service data can only ever show glaring differences:
    about 12 weeks per day to see one, about 30 for a moderate one.
  - Grouping moves from route to service level: CMH-MSP 1:45pm differs on
    Fridays and that route's other services don't. Schema change.
  - O/F counts switch to the live T1 estimator at the same time; they
    currently read GraphObservations' deprecated t1Old.
  - Open: the pooled groups themselves, and whether Friday joins Tue/Wed.

## Decline curve / predictor

- **Logging hint is the history range, not the curve.** Under each cabin
  input: lowest–highest of what prior flights of the service read at this
  hour, with the count (ServiceGrouping.history_ranges_for_row). The
  decline fit's coefficients (C1, gap, r) still show beneath it. The
  curve's own predicted-value code is kept, unused, in case it comes back:
  SeatLoggingDialog.py's curve_estimates_for_row and the page's
  formatCurveEstimate.
- **"No net decline in window" instances — needs a design decision, not a
  bug fix.** These have real non-9 data but the window's net change is ≤0
  (flat, oscillating, or ends higher than it started), so fit_instance
  never gets a positive slope to correct; more iterations don't help.
  Possibly its own category (a resting value plus noise, not a decline to
  force-fit) - see the multi-model idea below.
- **2-3 distinct models by service behavior — idea, parked pending more
  Fridays.** Current: internal-shape decline fit (fit_instance). Candidate
  second model: "found the bottom, sits there with noise" - a settled value
  and a spread, no slope - for the no-net-decline instances. Long-haul
  likely needs its own treatment (a 9-seat ceiling means something very
  different on a wide-body cabin). As of 2026-09-18 a flat-level model
  looked plausible but rested on four Fridays. Judge it by total
  step-change corrections needed and the T4→T1 backtest, not leftover
  residual (a flat line with free corrections fits anything).
- **Time-to-full as its own signal — idea, not designed.** A service that
  crosses the full threshold (currently 2) very early versus very late are
  different animals; nothing captures this now. It's an open/full-count
  question (ServiceGrouping.py), not a decline-curve one, and would need a
  corner solve aimed at the full threshold rather than 9/0. A rough
  distinction is all that's wanted - not "full" vs "thoroughly full".
- **Spot the anomalous closed day on a usually-open service — idea, not
  designed.** If the T-4 backtest can tell the specific closed instance
  from the normal-open ones (rather than an aggregate spread), that flags
  exactly the day the history alone wouldn't. Flagged as "extremely
  useful" if it can be made to work.
- **Golden-ticket window from data.** Find historically how far out the
  estimator still reliably predicts the eventual T1; that, not a flat 1.5h,
  should set the window per service. Delta's cutoff is T-45min, the hard
  floor either way. Real interest, not today's task.
- **Cosmetic: the console's `gap = 9.0/slope` print** can show a huge
  meaningless number when thin data gives a near-zero slope. Display-only;
  drop it from the print or cap its display. The near-zero slope itself is
  an honest low-confidence answer and stays.
- **Night from both ends' clocks — planned, live fit unchanged.** Night is
  currently the nightStartHour-nightEndHour window on the Eastern clock for
  every route. Planned replacement: the same window on each end's local
  clock, one shared night ratio, each hour weighted 1 when both ends are in
  night, 1/2 when only one is, 0 when neither. No new coefficient.
  python/NightZoneBacktest.py compares it against the current definition,
  origin-only, both-ends (AND) and either-end (OR). As of 2026-09-27 none
  beat the current one; rerun with more cross-zone data before changing
  the live fit.
- **Tell truly full from dip-and-recover — wanted, not designed.** A
  flight that declines and stays low versus one that dips early and
  recovers by departure.

## Graphing — deprecated, not being worked on

- GraphObservations (python/GraphObservations.py, GraphObservations.html,
  /graph) is deprecated: kept in the repo in case graphing comes back, not
  used for decisions, not maintained. It does not match the rest of the app
  and that is accepted - do not explore, fix, or compare it until he says
  he is working on graphing again.
- T1 weekday bar chart (Mon-Sun per service) — mockup approved, not built.
  Approved spec:
  - Gray banding between adjacent weekday groups; weeks aligned
    left-to-right consistently across every weekday's group (same week
    index = same calendar week no matter which day).
  - One shared fixed y-axis across all such charts, capped at 20.
  - Minimum bar size (~0.5 magnitude, straddling zero) so true-zero and
    near-zero readings stay visible.
  - Ceiling glyph: stacked chevrons above a bar, same color as the bar,
    one chevron per seat-class reading that hit the 9-seat ceiling feeding
    that estimate.
  - No-observation slot: dim/translucent ghost bar in the same color as
    real bars, height = that weekday's average across whichever OTHER
    weeks have data, with a gray "?" centered in it; the "?"'s opacity
    fades as its value approaches the top of the y-axis (never fully to
    zero).
- Step-change sequence graph: detected step changes over time (seats
  up/down, when each occurred) - also a way to quantify how "jumpy" a
  service is, comparable across day-of-week/season/service.

## T1 comparison page

- **T1Comparison.html (/t1-compare) — first version built, waiting on his
  feedback.** Looks like the T1 Grid Report's tables (services down, dates
  across) and reads the same /api/getT1Grid numbers, so it is the same
  estimator. Pick a scenario, narrow by route and day with checkboxes;
  routes run across, days run down, and date columns line up within a
  day's row so routes compare cell for cell. Any leg of any scenario,
  including a connecting leg out of Scrunch!, can be compared day to day.

## Forward-looking schedule import (blocked on him, not stuck)

- **Goal**: replace manual FlightSchedule/AircraftConfigs entry with a
  script pulling scheduled dep time, day-of-week, and aircraft type from an
  external source, 1-3 days out, ~90% accuracy. Scheduled only - not
  last-minute swaps or delays.
- **Chosen direction**: AeroDataBox via RapidAPI, free "Basic" tier — 600
  API units/mo, 2,400 requests/mo, 1 req/sec, claims 100% US schedule
  coverage. Schedule endpoint returns up to 7 days of an airport's flights
  per call; query **per origin airport** (~6-8), not per route, to stay
  inside the free quota even run daily. Filter client-side to Delta + his
  tracked destinations, map into FlightSchedule (depTime, dayOfWeek) and
  AircraftConfigs (aircraft type).
- **Blocker**: he hasn't signed up for a RapidAPI/AeroDataBox key (has to
  be him). No fetch/parse code written yet. API-unit cost per call for the
  schedule endpoint unconfirmed until a key exists. He's pausing manual
  aircraft entry meanwhile.

## Repo privacy

- **Make the repo less public — decided, not started.** The routes and
  cities being checked reveal personal details on their own. Open
  question: Claude reads the repo and live data by cloning the public
  GitHub URL, so going private needs a replacement way for Claude to get
  read access before the switch, or Claude loses its only view of
  nonrev.db.

## Parked

- **When to stop logging a flight until the golden ticket.** An early
  reading far enough from the middle may settle the day on its own: high
  enough that it almost never ends full, or low enough that it almost
  never ends open. Starting point is findings.md, 2026-09-29 (early
  reading bands against T-1). Open: the threshold on each side, whether
  it varies by service or route, and how the dialog would show it.
- **White knuckle flights.** A flight that ended open (T-1 at or above the
  open threshold) but dipped more than one below that threshold on the way
  there. Show it beside the open/full counts, so "usually open, the odd
  white knuckle" reads differently from "never had one" - a dip on a
  service that has never had one means something is up today. Rough count
  (2026-09-29, summed cabins, dip to 6 or less before the T-1 window): 60 of
  956 days that ended open, on 42 of 146 services. A dip is only seen if a
  reading lands in it, so "never" means less on thinly read services.
- **Wide-open coach — parked.** A flight whose late seat map shows dozens
  of selectable coach seats is wide open in coach, a different animal from
  a can-buy of 9 with only a few selectable seats. Judge it per cabin,
  coach on its own: coach can be wide open while Comfort+ shows none and
  first shows two, and that combination is worth knowing (a coach row to
  yourself). The per-cabin selectable counts are already logged (soloY,
  soloCPlus, soloFirstOrPS). Open: the coach threshold, whether it scales
  with aircraft size, and where it shows.
- **Sharper glance-floor estimate.** He has ideas beyond the per-cabin
  mean (per service, a plus/minus band). Low value for now: the estimate
  only feeds the logging dialog's Prev and T-1 columns.

## Someday / not started, low priority

- Long-haul Delta One analysis (Hawaii, Tokyo, New Zealand, Australia) —
  needs per-cabin breakdowns tied to actual aircraft config, since a
  ceiling of 9 means something very different on a small commuter cabin
  vs. a wide-body one.
- Launcher daily tally (flights left / departed / golden tickets): if it
  comes back it belongs on the logging dialog and counts only studied
  flights.

## Dead ideas — do not re-propose

- Re-auditing service identity (raw depTime vs. clustered service) on
  general principle — closed 2026-09-16; a new, specific symptom is the
  bar for reopening. Raw depTime correctly stays exact in
  FlightScheduleDialog and ObservationsBrowser.
- Two-directional night ratio (separate origin/destination overnight
  rates) — replaced by one shared ratio with night counted from both ends'
  clocks (see Decline curve).
- Orphan detection by comparing past observations to the current
  flightSchedule — that table is this week's snapshot only; a reading's
  own depTime is its record.
- Verdict/classification-glyph UI work (verdict-text granularity, a second
  "lock it in" glyph) — not interested.
- Artificial future-zero injection to force a slope on a flat instance —
  circular: the slope would be set by the fake zero's arbitrary timing,
  and it would erase the real "usually open" signal.
- Per-instance two-point bracket rate as a replacement for fit_instance's
  multi-point fit — lost a leave-one-out backtest on both coverage and
  accuracy in every cabin.
- Splitting bounce-back-to-9 readings into separate instance keys before
  fitting — the correction loop handles a bounce-back in place.
- Automated tiered cadence/eligibility engine — he walks every flight in
  departure order; the engine let logged flights cut back in line and
  starved routes.
- GraphObservations' t1Old/t1New side-by-side comparison — never used.
- Aiming cadence at a service's C1/C2 corner — the fit doesn't need the
  corner observed, and the curve no longer drives the estimate.
- Automating overnight Delta.com checks (scripted loads, VPN/bot) —
  ToS/detection risk.
- Cadence-from-slope, curve-shape decimation — real data jumps in
  unpredictable discrete steps, not noise around a curve. (T-4-predicts-T-1
  modeling is NOT dead.)
- Step-change propagate-forward-vs-single-point question — moot; each
  correction pass refits from scratch.
- Eyeball-checking wide-spread route clusters before locking a clustering
  threshold — clustering has held up every time it's been checked.
- Automated full/open/iffy classification written to a `classification`
  column — classification is a human judgment, not an automated write.
- flightSchedule's free-text `verdict` + `verdictType` glyph — computed
  grades (python/Grading.py) do this job.
- 3-unanimous-readings verdict threshold — blocked acting on visible
  patterns.
- Skip-a-route-because-it's-full — he watches full flights himself.
- Persisted `services` table — live clustering is cheap enough to redo on
  the fly. (Not a statement against service-based clustering.)
- Corridor graph for the T1 curve — no clear shape for it.
- `scheduleRowId` surrogate key linking observations to flightSchedule —
  fragile against real schedule churn.
- Value-mangling a stored flightNumber value — the renamed column alone is
  sufficient warning.
- Ruled out for schedule import: OAG/Cirium (enterprise-priced), BTS
  on-time data (historical, ~3-month lag), scraping schedule-display sites
  like FlightAware or FlightConnections (bot-blocked / ToS).
