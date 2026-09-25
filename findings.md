# Findings

Dated results of one-off analyses of the logged data. Each entry is a
snapshot: true of the schedule and readings as of its date, not a
permanent fact (those go in domainKnowledge.md) and not planned work
(that goes in TODO.md). Newest first. Re-run an analysis before relying
on an old entry.

## 2026-09-25 — Scrunch!: Thursday vs Friday, last day-of T1 estimate

**Method**: each Scrunch! flight's last T1 estimate (T1Estimator via
previous_readings_for, all cabins summed) from a reading within 6h of
departure. Flights last read further out are excluded (26, mostly pre-dawn
flights read the night before, and 9/11's DTW and CVG flights). Full is
T1 ≤ 2, open is T1 ≥ 8.

| Date | Flights | Mean T1 | Full | Between | Open |
|---|---|---|---|---|---|
| 8/27 Thu | 32 | 10.4 | 6 | 4 | 22 |
| 8/28 Fri | 32 | 10.3 | 4 | 8 | 20 |
| 9/10 Thu | 28 | 10.5 | 5 | 4 | 19 |
| 9/11 Fri | 15 | 10.7 | 3 | 1 | 11 |
| 9/17 Thu | 27 | 7.1 | 6 | 6 | 15 |
| 9/18 Fri | 34 | 11.4 | 4 | 6 | 24 |
| 9/24 Thu | 29 | 7.7 | 10 | 4 | 15 |

- **Thursday has not been more open than Friday**: same service, same
  week, Thursday vs Friday means were 10.4 vs 10.3 (8/27-28), 10.4 vs
  10.7 (9/10-11), 7.1 vs 11.5 (9/17-18). Before 9/24, open on 56 of 87
  Thursday flights and 57 of 83 Friday flights.
- **9/24 is the worst day on record** by full flights: 10 of 29, against
  4-6 on every other day.
- **Thursday is more erratic**: per service across 3 weeks each (9/24
  excluded), 26 services per day:

| Per service, across weeks | Thursday | Friday |
|---|---|---|
| Swung between full and open | 6 | 1 |
| Same class (full/between/open) every week | 11 | 16 |
| Median swing, high week minus low week | 5.9 | 4.1 |
| Median standard deviation | 2.6 | 2.1 |

  With 9/24 included, Thursday rises to 12 full-open swings and a median
  standard deviation of 3.3.
- **Caveat**: 9/11 was thinly read, so some Friday services have only 2
  weeks, which leaves less room to look erratic.

## 2026-09-24 — Scrunch!: MSP went full on a Thursday

**What happened**: every MSP→PDX flight but the last one, and every
CMH→MSP feeder, read Y=0 by departure - flights whose prior weeks were
open at the same hours out. It held all day: no MSP→PDX flight that hit 0
came back.

| Service | Y at last reading | Prior weeks more open than that, same hours out |
|---|---|---|
| CMH→MSP 10:50 | 0 at 1.0h | 10 of 10 at 2.1h |
| CMH→MSP 13:44 | 0 at 1.1h | 5 of 5 at 5.0h |
| CMH→MSP 19:17 | 0 at 1.5h | 6 of 6 at 10.5h |
| MSP→PDX 11:00 | 0 at 1.3h | 5 of 5 at 2.9h |
| MSP→PDX 15:55 | 0 at 1.3h | 7 of 8 at 7.8h |
| MSP→PDX 18:36 | 0 at 1.3h | 8 of 8 at 10.5h (all prior weeks 7 or more) |
| MSP→PDX 21:35 | 9 at 1.1h | - (9 at every reading) |

- **The collapse happened unobserved**: all MSP→PDX flights read 9 at
  27-36h out (Wed 07:13 ET) and 0 at 8-10h out (Thu 09:06 ET). Nothing
  was read in between.
- **Feeders warned before that gap**: CMH→MSP 19:17 read 7 at 56h and 3
  at 37h; CMH→MSP 10:50 read 7 at 28h, its seat map down to 1 open single
  from 17; CVG→MSP 16:24 read 3 at 34h. CMH→MSP 10:50 then read 1 at 8.8h.
- **Not a first**: Thu 9/17 was a smaller version - MSP→PDX 15:55 at 0
  from 4.0h on, 18:36 at 1-3, 11:00 at 5-6, CMH→MSP 10:50 ending at 2.
- **Y=0 did not mean no seats**: at 17:30 ET, insider reading on MSP→PDX
  18:36 showed 21 standby seats, 25 listed, our position 24 - verdict no.
  The seat map at 2.0h showed 9-10 open singles. The flight had room; the
  standby list was longer than it. The 21:35 at the same time showed 16
  standby seats, our position 2 - probably yes.
- **The fallbacks failed together**: 11:00, 15:55 and 18:36 were all 0
  at once, and standbys not cleared on one roll to the next, so later
  flights inherit earlier flights' lists. Fallback depth at MSP was worth
  little that day.
- **Held-back seats**: insider reading on CMH→MSP 19:17 showed 0 standby
  seats and 1 listed, verdict yes - Delta holds back a few seats, and
  first in line gets one.
- **Normal the same day**: SLC→PDX (Y 9 on all but 17:20, which ended at
  5), CVG→MSP 10:49, and the DTW routes within their usual spread.
  CVG→MSP 19:47 dipped to 3 at 4.2h and ended at 9 at 1.4h.
- **Cause unknown**: no news found explaining an MSP disruption.
- **Friday 9/25**, as of Thursday 22:58 ET: every CMH→MSP and MSP→PDX
  flight read 9 at 8-24h out except MSP→PDX 11:00 at 7 (13.0h). At the
  same hours out, Thursday's feeders had already fallen and prior Fridays
  read 9. Seat maps have drained: MSP→PDX 15:55 at 5 open singles (17.9h),
  18:36 at 11 (20.6h), CMH→MSP 10:50 at 2 (12.2h). Low seat-map counts
  have also preceded Y=9 at departure (CMH→SLC 7:45 Thursday: 3 open
  singles at 24.8h). Prior Fridays tightened late on MSP→PDX 15:55 and
  18:36 in 2 of 3 weeks (8/28, 9/11).

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
  day, so there are retries at both ends. MSP→PDX read Y=0 at departure
  on 2 Thu/Fri flights out of about 30 (Fri 8/28 18:36, Thu 9/17 15:55).
  Weak spot is the evening: CMH 19:17 and CVG 16:24 were full most
  counted weeks and have little or no fallback.
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
