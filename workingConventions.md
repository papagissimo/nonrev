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
- When a real data anomaly turns up (a duplicate row, a row that doesn't
  fit), fix or delete it rather than dismissing it as too rare to matter.
- Don't flag things that aren't problems. Example: a seat map and a
  can-buy reading from one sitting landing in separate rows a minute or
  two apart is just two clicks instead of one, and is fine.
- He is easily confused. Deliverables and instructions contain exactly
  what the change needs - nothing bundled "just in case."
- Don't suggest pulling or pushing just because a piece is finished. Wait
  for him to say he's done iterating.

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

- He walks every scheduled flight in departure order, logging or
  blank-skipping each. There is no automated cadence engine and there
  shouldn't be one.
- After a submit, the dialog reshows the same route with the new values;
  a blank submit skips the route. Keep this confirm-then-skip two-step.
- His logging tab stays open for days. Refreshing deliberately resets the
  session and revisits skipped flights. Keep this.
- Cadence floor: every flight gets an early reading, a T-4 and a
  golden-ticket reading. Grades only decide effort above that floor.
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
- Short western hops (PDX-LAX, LAX-SLC, SLC-BUR) get bought, not flown
  nonrev. Nonrev is for Ohio to the west coast and back.

## Charts and documents

- Hours-before-departure charts put departure at the right, time
  remaining decreasing left to right.
- Documents read as current settled truth. No narrative of wrong turns or
  how a decision was reached.

## Decline curve decisions not recorded elsewhere

- Slope is seats/hour everywhere, never minutes/seat.
- C1 accuracy matters little; slope accuracy matters most.
- The curve is not currently driving the logging hint (see TODO.md).
