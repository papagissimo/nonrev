# Working Conventions

How this project is worked on: how he works, how Claude works with him,
and standing decisions that aren't code. Read this at the start of every
session, right after cloning or pulling. Current truth only - when
something here changes, edit it in place. How the airline industry and
non-revving work belongs in domainKnowledge.md; planned work belongs in
TODO.md; dated analysis results belong in findings.md.

## Start of every session

- Clone or pull github.com/papagissimo/nonrev before reading or writing
  anything. He often pushes his own edits and fixes; never write code
  against a stale copy. No need to ask permission to clone or pull.

## How we collaborate

- Discuss and align before building. No changes without a clear go-ahead.
- Claude writes essentially all the code. He adds data and proposes
  features.
- Before renaming a component or file, list every reference across the
  codebase first. Renames are especially error-prone in this stack.
- Before hand-rolling nontrivial logic (offset/timezone math, say), check
  whether a standard library or tool already solves it.
- Trust only what the code does. Verify behavior against the code, never
  against a comment or docstring.
- Tiny anomalies wait until the end. Mid-discussion, raise one only if it
  could change the answer to the question at hand. Others get fixed
  quietly in the next zip, or, if they touch nonrev.db, go on a short
  list of one-liners delivered at the end of the session or when he asks.
  A real anomaly still gets fixed; it just doesn't get discussed. Example:
  a reading entered under Y that belonged under Total doesn't change a
  go/no-go verdict, so it waits for the end-of-session list.
- Don't flag things that aren't problems. Example: a seat map and a
  can-buy reading from one sitting landing in separate rows a minute or
  two apart is just two clicks instead of one, and is fine.
- He is easily confused. Deliverables and instructions contain exactly
  what the change needs - nothing bundled "just in case."
- Don't suggest pulling or pushing just because a piece is finished. Wait
  for him to say he's done iterating.

## Vocabulary

- Flight: one dated departure - the 10:00 on Wednesday, 2026-09-30.
- Weekly service: one departure time on one weekday - the 10:00 on
  Wednesdays.
- Daily service: one departure time on whatever days it runs - the 10:00.
  In the code, `service` means daily service.
- Pool: a set of weekly services grouped because their readings settle
  alike (python/TrustPools.py), recomputed from the data, never stored.
  Lettered A onward from the steadiest. How flights end is not a pooling
  question: a service's open/full record is its own, shown as counts.
- Yardstick: how big a move in the summed can-buy total matters, by the
  current total, in either direction. A setting, as total/move pairs.
- Never say bare "service" in docs or discussion; say weekly or daily.

## Delivering changes

- Whole files, never fragments to splice in.
- Delivered as a zip, files at their repo-relative paths (python/foo.py),
  applied locally with his `nrupdate` script.
- Never include nonrev.db in a zip.
- Accumulate one cumulative zip across a session until he says he's
  applied it; then start a fresh one.
- Give each zip a descriptive, distinctive name.

## Running things

- Nothing may require being run from a particular directory. Scripts find
  nonrev.db from their own location
  (`os.path.join(os.path.dirname(__file__), '..', 'nonrev.db')`, as
  server.py does). One-liners use the absolute path ~/nonrev/nonrev.db.
- Anything that opens the db refuses to run if the file is missing, so
  sqlite can never silently create an empty one.
- The project runs in a virtual environment on his Chromebook. pip
  installs never use --break-system-packages.
- Schema changes to his existing nonrev.db ship as a one-off
  python/migrate_<what>.py. He commits, runs it once, then deletes it;
  it is never git-added. No one-time migration code stays in the app.
  create_db.py gets the same change for a from-scratch setup.

## Git

- nonrev.db is tracked in git deliberately; it's Claude's only view of
  the live data.
- His backup habit is committing right before anything risky (such as
  applying a zip). That's sufficient. Explicitly flag any git operation
  that would overwrite nonrev.db, and prompt a commit first.
- Avoid branches.
- Always give `git commit` a `-m`, never leave him dropped into Vim. His
  `ggc` alias is `git commit -am "$*"`; new files get a separate explicit
  `git add`.

## Data conventions

- NULL (didn't observe) and 0 (observed, none) stay distinct everywhere,
  in every observation field.
- Everything gathered in one sitting is one observation, not split
  across a type tag. No organizational layers that serve no purpose.
- Flight number never identifies a flight across dates. Identity is
  org + destination + scheduled local departure time. Settled; don't
  re-propose anything flightNumber-based.
- The can-buy count he logs is the most seats Delta would sell in one
  purchase at any price - total sellable, not a per-fare "seats left at
  this price" figure.

## Logging workflow

- The dialog shows one route-day at a time, its flights in departure
  order, and he logs or blank-skips each route. The next route is the
  most pressing by cadence (TrustPools.cadence_reading), re-ranked after
  every route, one 24-hour window at a time (departing within 24h, then
  24-48h, and so on). Within a window: Last first (never-read by soonest
  departure, then by points, then no pool), then Now! (unread
  golden-ticket windows by soonest departure, then by points, then
  leadoffs by soonest departure), then Meh by points (no pool last). Skip routes come after
  every window, by departure. A route ranks by its most pressing flight.
  The dialog never skips flights for him.
- After a submit, the dialog reshows the same route with the new values;
  a blank submit skips the route. Keep this confirm-then-skip two-step.
- His logging tab stays open for days. Refreshing deliberately resets the
  session and revisits skipped flights. Keep this.
- Cadence practice: the leadoff (first reading of a flight) is a full
  count; readings in between are usually glances; the golden ticket, or
  the last reading he knows he'll get, is a full count. Seat maps only at
  the golden ticket. This is practice, not code.
- Cadence states (TrustPools.cadence_state): Now! for the leadoff and for
  an unread golden-ticket window; Skip once the golden ticket is in;
  otherwise by the points a reading now would buy - the pool's curve at
  the last reading's distance less the curve now - against the Now! and
  Skip settings. Last, only while a Back-at time is set: any flight not
  yet golden-ticketed that departs before Back-at plus the away margin -
  the reading now is its last, so a full count. Rows show Last darker
  green, bold, with a left bar; Now! green and bold; Meh light gray; Skip
  dark gray, striped within each. Back-at clears itself once it passes.
- Under each row's hours is when it next turns Now!, on his clock,
  rounded up to 5 minutes. Hours under 1 show as minutes.
- Overnight flights whose T-1 falls while he's asleep cap out at T-4.
  Extrapolating T-4 to T-1 and automating overnight site checks were
  both rejected.

## Scope

- Don't raise Delta One analysis until he's studying a route with a D1
  cabin.
- GraphObservations is deprecated and not for decisions. Don't explore it
  or raise its mismatches until he says he's graphing again.

## Trip constraints that shape the study

- Nonstops preferred. No added legs or backtracking just to make a
  routing work - nothing through ATL or JFK to reach the west coast.
- No itineraries needing a 3-4am wake-up; he'd rather buy.
- CMH is strongly preferred over CVG as the Ohio origin.
- The Western Triangle (any flight among SLC, LAX/BUR and PDX) gets
  bought, not flown nonrev, and is no longer studied. Nonrev is for Ohio
  to the west coast and back.

## Charts and documents

- Hours-before-departure charts put departure at the right, time
  remaining decreasing left to right.
- Documents read as current settled truth. No narrative of wrong turns or
  how a decision was reached.

