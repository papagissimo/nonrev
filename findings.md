# Findings

Dated results of one-off analyses of the logged data. Each entry is a
snapshot: true of the schedule and readings as of its date, not a
permanent fact (those go in domainKnowledge.md) and not planned work
(that goes in TODO.md). Newest first. Re-run an analysis before relying
on an old entry.

## 2026-09-23 — Scrunch!: which hub to go through to PDX

**Question**: for Scrunch! (Thu/Fri, CMH or CVG → DTW/MSP/SLC → PDX),
which hub gives the best chance of reaching Portland the same day?

**Answer**: MSP, clearly. SLC is good once you're there but has a thin
way in. DTW is poor.

The measure that separates them is fallback depth — how many more
same-day tries you get after being bumped, either at the origin (take a
later feeder) or at the hub (take a later PDX flight). Thursday; Friday's
schedule is identical.

| Hub | PDX flights | Ohio feeders that connect | Typical layover | Later PDX flights if bumped at hub |
|---|---|---|---|---|
| MSP | 5 (9:10 – 21:35) | 7 of 8 | 1h – 4h | 4 from morning feeders, 2 from midday |
| SLC | 5 (7:45 – 22:45) | 3 of 3 (one from CMH) | 1.5h – 2h | 3 from morning feeders, 0 from the evening one |
| DTW | 2 (8:45, 20:25) | 10 of 12 | 1h if leaving by 6:30, else 3h – 11h | 0, except 1 from the pre-dawn feeders |

- **MSP**: CMH feeders at 7:00, 10:50 and 13:44 all reach PDX the same
  day, so there are retries at both ends. MSP→PDX was never full in any
  counted week. Weak spot is the evening: CMH 19:17 and CVG 16:24 were
  full most counted weeks and have little or no fallback.
- **SLC**: CMH has one SLC flight a day (7:45). If it's full, the only
  other way through SLC is CVG 19:30, which lands with one PDX flight
  left. One shot getting in, cushion after.
- **DTW**: nearly every feeder lands for the single 20:25, with a 3–11h
  wait and no fallback if it's full. Avoiding the wait means a 6:17 or
  6:30 departure for the 8:45.

**Assumptions**: 40-minute minimum connection; timezone offsets for
2026-09-24. Fallback counts come from the schedule alone, so they don't
depend on openness data.

## 2026-09-23 — Scrunch!: CMH vs CVG feeder fullness

**Question**: is CMH better than CVG apart from the drive?

**Answer**: yes. CVG feeders run full far more often. The two airports
have about the same number of feeders into the three hubs (CMH 11,
CVG 12), so flight count isn't the difference.

Counted weeks for feeders that can connect to PDX, Thu and Fri pooled:

| | Counted weeks | Open | Iffy | Full |
|---|---|---|---|---|
| CMH | 41 | 31 | 5 | 5 |
| CVG | 38 | 16 | 9 | 13 |

Full about 1 week in 8 from CMH versus 1 in 3 from CVG. Most of the gap
is at DTW (CVG feeders full 8 of 18 weeks, CMH 2 of 19); at MSP it's
mostly CVG 16:24 (full 5 of 6). SLC is even — both morning flights open
every counted week.

**Assumptions / caveats**: only a few calendar weeks of data, so one busy
week counts against many flights at once. Pooling gives ~40 weeks per
airport versus 1–3 per flight, so this is firmer than any single
flight's tally, but still thin. The one-letter CVG grade penalty is
backed by both the drive and this data.

## 2026-09-23 — Scrunch! grade consistency check

**Question**: are the hand-entered Scrunch! grades consistent?

**Answer**: mostly. Clear rules held: departures at 6:30 or earlier got
D; 7:00–7:45 with good connections got B; any flight with no PDX
connection got F; grades improved steadily as layovers shortened.
Two exceptions:

- The two sides of one connection were graded differently (DTW→PDX 8:45
  is F while its only feeders are D; CVG→SLC 19:30 is B while its only
  onward, SLC→PDX 22:45, is C).
- "Often full" was applied unevenly (CMH→MSP 19:17 full 2 of 2 weeks got
  B; CMH→MSP 13:44, never full, got C).

MSP→PDX had no grades at the time of the check.
