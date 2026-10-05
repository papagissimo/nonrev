# Findings

Dated results of one-off analyses of the logged data. Each entry is a
snapshot: true of the schedule and readings as of its date, not a
permanent fact (those go in domainKnowledge.md) and not planned work
(that goes in TODO.md). Newest first. Re-run an analysis before relying
on an old entry.

## 2026-10-05 — Drops and rises hidden in the average move

**Question**: the average move from a reading to T1 is small. Does it hide
flights that drop a lot and flights that rise a lot, can those be told
apart ahead of time, and does adding the average move to the held reading
call T1 better?

**Answer**: it hides them; they can't be told apart yet; adding the move
helps from T-24 and is a wash from T-12 and T-8, and one fleet-wide move
does as well as one per trust pool. The live T1 estimate now adds it
(python/DriftCurve.py).

Change from the reading to T1, summed cabins, Triangle left out:

| From | Flights | Avg | Within 2 | Drop 3+ | Rise 3+ |
|---|---|---|---|---|---|
| T-24 | 226 | -2.7 | 46% | 44%, about 6 seats | 7% |
| T-12 | 324 | -1.0 | 60% | 27%, about 6 | 13%, about 6 |
| T-8 | 310 | -1.1 | 60% | 27%, about 6 | 12%, about 6 |
| T-4 | 346 | 0.0 | 76% | 13%, about 5 | 11%, about 6 |

- A drop already seen between two early readings doesn't predict more:
  21% of those flights dropped another 3+ and 25% rose 3+, against 19%
  and 9% for flights that had held flat.
- A service's own track record (python/DirectionTrackRecord.py,
  leave-one-out, readings at 3+ seats): labeling services decliner,
  steady or riser by their other flights never beat shuffled labels, by
  weekly or by daily service, at T-24, T-12 or T-8. Weekly services are
  too thin: 310 of them, 9 with 5 finished flights and none with more, and
  at most 2 with 5 flights read near any one horizon.
- Trust pools on this date's data are two, split by weekday only (A: Wed,
  Fri, Sat; B: Mon, Tue, Thu, Sun). Pool B drops more than A only from
  T-12 (38% against 21% of flights at 3+ seats). Within B, riser track
  records cover 7-10 flights at any horizon.

Adding the move to the held reading (python/DriftCurveBacktest.py,
leave-one-out, all finished flights, full <= 2, open >= 8):

| From | Days | Miss: hold / fleet / pool | Box right: hold / fleet / pool | Called open, was full: hold / fleet / pool |
|---|---|---|---|---|
| T-24 | 286 | 3.73 / 3.36 / 3.37 | 72.4% / 69.9% / 69.9% | 22 / 13 / 13 |
| T-12 | 461 | 2.92 / 2.89 / 2.90 | 75.7% / 74.6% / 73.5% | 19 / 17 / 13 |
| T-8 | 484 | 2.93 / 2.93 / 2.94 | 71.7% / 72.5% / 71.3% | 19 / 16 / 14 |

The fleet move is -2.5 seats at T-24, -1.0 at T-12 and -1.0 at T-8. Inside
T-6 the median move is 0, so the shift is zero there. The shift trades a
little box accuracy from T-12 and T-24 for fewer flights called open that
ended full.

## 2026-10-04 — Drift corrections by cabin and by service

**Question**: judged on seats rather than open/full boxes, does a drift
correction beat hold, and does doing it per cabin or per daily service
help?

**Answer**: only from a day out, and not yet per service. Average miss
in summed seats at T-1, leave-one-out:

| From | Hold | Shift on the sum | Per-cabin shift | Per-cabin, per-daily-service shift |
|---|---|---|---|---|
| T-8 | 2.71 | 2.74 | 2.74 | 2.86 |
| T-12 | 2.85 | 2.81 | 2.81 | 2.92 |
| T-24 | 3.73 | 3.47 | 3.47 | 3.50 |

- Each cabin's median drift is 0 at every horizon: on a typical day a
  cabin doesn't move, and decline comes in occasional chunks, mostly in
  coach (mean -0.8 seats from T-12, -1.4 from T-24). Comfort+ and First
  barely move until a day out.
- Per-cabin mean shifts sum to the same total as one shift on the sum.
  Per cabin can only differ through something cabin-specific, such as a
  cabin already at 0.
- Per-service drift isn't visible yet: the spread of service medians
  (services with 5+ days) is no wider than with days shuffled among
  services at T-6 to T-12. Per-service shifts are pulled toward the
  fleet value when a service has few days, and still do worse.
- Revisit when weekly services have roughly 10 or more weeks each.

## 2026-10-04 — Shifting the held reading by typical drift

**Question**: seats drift down between a far-out reading and T-1. Does
shifting the held reading by that typical drift call the T-1 box better
than plain hold?

**Answer**: only modestly, and only from T-8 out. Summed seats change
from the horizon to T-1 by a median of 0 from T-4 and T-6, -0.3 from
T-8, -1.0 from T-12 and -1.9 from T-24. Shifting by that median
(python/DriftShiftBacktest.py, leave-one-out, three cabins summed, full
<= 2, open >= 8):

| From | Days | Hold right | Shifted right | Hold: open->full | Shifted: open->full |
|---|---|---|---|---|---|
| T-4 | 588 | 82.3% | 82.3% | 7 | 7 |
| T-6 | 558 | 77.8% | 77.8% | 16 | 16 |
| T-8 | 497 | 74.0% | 75.3% | 18 | 15 |
| T-12 | 463 | 75.6% | 74.9% | 20 | 16 |
| T-24 | 226 | 69.5% | 66.4% | 19 | 13 |

- From T-12 and T-24 the shift trades overall accuracy for fewer
  called-open-was-full misses: it moves near-8 readings to between, and
  most of those flights ended open.
- Shifting by the mean, or by the median within the held value's band
  (0-2, 3-7, 8-15, 16+), lands in the same place. Seat error barely moves
  under any shift.

## 2026-10-04 — SLC routes: SLC→CVG sells out most

**Question**: which routes into and out of SLC sell out most?

**Answer**: SLC→CVG, by a wide margin. Going in, PDX→SLC is fullest.
The CMH legs are the roomiest both ways. Each flight counts once, at its
last reading within 6h of departure, as summed can-buy across cabins.

| Route | Flights | Avg seats left |
|---|---|---|
| SLC→CVG | 57 | 8.7 |
| SLC→PDX | 126 | 13.1 |
| SLC→LAX | 95 | 13.4 |
| SLC→CMH | 27 | 14.8 |
| PDX→SLC | 63 | 11.0 |
| LAX→SLC | 79 | 14.4 |
| CVG→SLC | 45 | 15.0 |
| CMH→SLC | 18 | 16.2 |

SLC→CVG is lopsided: the same pair coming in (CVG→SLC) is among the
roomiest, so the demand runs one way.

**Assumptions / caveats**: can-buy counts only paying passengers; it
says nothing about standby list length. No DTW↔SLC or MSP↔SLC routes are
logged. BUR left out. The charts were one dot per flight on a
seats-left axis, one row per route, with the average marked; this
reproduces their data:

```python
import os, sqlite3, statistics, sys
sys.path.insert(0, os.path.expanduser('~/nonrev/python'))
from Pools import logged_flights
conn = sqlite3.connect(os.path.expanduser('~/nonrev/nonrev.db'))
routes = {}
for f in logged_flights(conn, 999):
    if 'slc' in (f['org'], f['dest']) and 'bur' not in (f['org'], f['dest']):
        routes.setdefault(f"{f['org']}-{f['dest']}", []).append(f['cappedT1'])
for route, seats in sorted(routes.items(), key=lambda kv: statistics.mean(kv[1])):
    print(route, len(seats), round(statistics.mean(seats), 1), sorted(seats))
```

## 2026-10-03 — Trust pools: four ways readings settle

**Question**: grouped by how readings settle rather than how flights end,
how many distinct cadences do the flights need?

**Answer**: four pools. Each curve is the percent of flights whose
reading at that distance still has a move that matters to come, by the
yardstick `3 3, 10 3.5, 20 10` (summed cabins, either direction).

| Pool | Flights | T-48 | T-24 | T-12 | T-8 | T-6 | T-4 | T-3 | T-2 |
|---|---|---|---|---|---|---|---|---|---|
| A | 308 | 8 | 7 | 8 | 7 | 5 | 3 | 3 | 1 |
| B | 131 | 28 | 31 | 18 | 15 | 13 | 11 | 10 | 8 |
| C | 270 | 61 | 40 | 28 | 21 | 20 | 19 | 16 | 14 |
| D | 269 | 58 | 51 | 43 | 36 | 34 | 35 | 24 | 21 |

The first split is by daily service, then by weekday on each side. All
three splits clear 0.01 (0.0006-0.003) against 9,999 shuffles, and the
same four pools come out on every shuffle seed tried.

**Assumptions / caveats**: about 20 flights per pool were read as far
out as T-48, so the left end of each curve is rough; T-8 inward rests on
95-180 flights per pool. Most weekly services have 1-3 flights, so a
single service's placement is thin. Glance readings enter at the
dialog's floor estimates.

## 2026-10-03 — Settling measures that don't separate flights

**Question**: which ways of saying "this flight's readings have settled"
tell steady flights from jumpy ones?

**Answer**: only a yardstick that shrinks the move that matters at low
totals and widens it at high ones. A fixed seat band (2-6 seats), a
percent band (30%) and changes of open/between/full box each put every
group's settling around T-3 to T-5, because high-count wobble (25 to 15)
or edge jitter (8 to 7) counts as unsettled. Counting only flips between
the extremes separates the groups but drops every flight that ends
between.

## 2026-10-03 — Holding the last reading beats the decline curve in every pool

**Question**: does the decline curve forecast T1 better than holding the
last reading within any pool?

**Answer**: no. Per-cabin error and open/full calls favor holding at T-4,
T-8 and T-12 in all four trust pools, as in the old pools. Pool D is the
one exception worth noting: the fleet-slope curve halves the open-to-full
misses (T-8: 12 to 6) but adds as many full-to-open false alarms (1 to 6).

## 2026-10-03 — How far out a leadoff still means something

**Question**: how far out does an early reading's open or full call hold?

**Answer**: by the old pools, open readings ended full:

| Open reading at | Golden | BadBoy | Never Know |
|---|---|---|---|
| T-3 to 8h | 1 of 340 | 21 of 202 | 0 of 36 |
| ~1 day | 2 of 175 | 24 of 99 | 2 of 22 |
| ~2 days | 1 of 98 | 11 of 48 | 3 of 15 |
| ~3 days | 2 of 58 | 28 of 45 | 7 of 12 |

No full BadBoy reading out to two days ended open; 17 of 161 ended
between. Counts are readings, not flights; past three days too few
flights to say.

## 2026-10-03 — Seat maps add little beyond the golden ticket

**Answer**: while can-buy reads 9, a seat map with 5 or fewer open
singles failed to end open 8 of 21 flights versus 7 of 41 otherwise
(about 1-in-15 odds of luck). Late, when can-buy reads full, about a
quarter of seat maps still show 6-17 open singles: the plane isn't full,
the standby list decides. Open seats plus blocked seats tracked the
insider standby count on one flight (MSP-PDX 3:55pm, 2026-10-02) and
nowhere else yet.

## 2026-10-02 — Trust horizon by per-hour risk

**Question**: how likely is an open reading taken at a given hour to end
full, per pool, and from how far out is that within 1 in 50?

**Method** (python/PoolTrustHorizon.py): open readings as in the entry
below, risk by hour fitted with isotonic regression (risk never falls with
distance from departure). Horizon = farthest hour with fitted risk at most
the tolerance, kept only if that few ending full inside it would happen by
luck under 1% of the time at the pool's overall rate. Replaces the earlier
rule, which pooled every reading within N hours, so the safe T-2 readings
diluted the risky ones near N.

**Result**:

| Pool | 1 in 20 | 1 in 50 | Risk at T-3 / T-4 / T-6 |
|---|---|---|---|
| BadBoy | T-3.8 | T-2.8 | 0% / 7% / 12% |
| Never Know | T-12.7 | none | 4% / 4% / 4% |
| Golden SuMTh | any distance | any distance | 0% / 0% / 0% |
| Golden TuWFSa | any distance | any distance | 1% / 1% / 1% |

Under the old rule BadBoy's 1-in-50 horizon read T-4.1, but an open
reading taken between T-4 and T-5 ended full 4 times in 46. About 50
readings per hour can't by themselves show 1 in 50 (0 of 48 is consistent
with up to about 6%), and readings from one flight aren't independent.

## 2026-10-02 — How far out a reading can be trusted, by pool

**Question**: does correcting the held reading help, and does the time a
reading becomes trustworthy differ by pool?

**Pools** (python/PoolSplitReport.py on this date's data): bad boys = the
32 fuller daily services, any day; middle child = 27 daily services that
run fuller on Sun/Mon/Thu; golden boys = the other 50 on Sun/Mon/Thu, and
all 90 non-bad-boys on Tue/Wed/Fri/Sat. Pool membership was built with all
flights, held-out ones included. Three cabins summed, full <= 2,
open >= 8, truth a reading within 1.55h of departure.

**Hold with a correction** (python/PoolBacktest.py, T-4 anchors,
leave-one-out): the typical T-4 -> T-1 change is zero per cabin and summed
(median 0, mean 0.09 seats summed; a third of service-days unchanged).
Subtracting the fleet or pool median changes nothing. Subtracting the pool
mean gets 84.2% of 571 calls right against hold's 82.5%, but "called open,
was full" stays at 6 under every method. Not worth adopting.

**Hold from each horizon** (python/PoolResolveBacktest.py), right T-1 call:

| From | Bad boys | Middle child | Golden, Sun/Mon/Thu | Golden, Tue/Wed/Fri/Sat |
|---|---|---|---|---|
| T-24 | 47% | 68% | 89% | 84% |
| T-12 | 61% | 66% | 90% | 88% |
| T-8 | 64% | 59% | 86% | 90% |
| T-6 | 68% | 64% | 83% | 92% |
| T-4 | 73% | 70% | 91% | 92% |
| T-3 | 81% | 80% | 90% | 94% |

Each horizon counts only days with a reading near it, so rows aren't the
same flights.

**Trust horizon for open readings** (python/PoolTrustHorizon.py): every
logged reading that said open, scored on whether the flight ended full.

| Pool | Open readings | Ended full | Trust from, 1 in 20 | Trust from, 1 in 50 |
|---|---|---|---|---|
| Bad boys | 608 | 106 | T-5.5 | T-4.1 |
| Middle child | 303 | 26 | no stable answer | no stable answer |
| Golden, Sun/Mon/Thu | 400 | 2 | earliest logged | earliest logged |
| Golden, Tue/Wed/Fri/Sat | 1,022 | 14 | earliest logged | earliest logged |

- Bad boys: open readings within T-4 ended full 2% of the time, within
  T-12 10%, within T-48 14%.
- Golden boys: about 1% at any distance, out to readings several days
  ahead.
- Middle child: 4-7% at every distance from T-3 to T-48. Its risk doesn't
  shrink toward departure, so whether it passes depends only on the
  tolerance.

## 2026-10-01 — Pools by best split: daily service first, then weekday

**Question**: which weekly services behave alike, without assuming any
grouping of weekdays?

**Method** (python/PoolSplitReport.py): the 2026-09-29 flights (live T1,
last reading within 6h, excluded ranges left out), each capped at 8. All
flights start in one pool. For each of weekday, daily service and weekly
service, a Kruskal-Wallis test asks whether its values differ; the
strongest splits the pool if p < 0.01 after multiplying by the number of
attributes tried. The cut is the one that best separates the two sides
with values ordered by mean capped T1. Repeated inside each side.

| Pool | Weekly services | Flights | Open | Full |
|---|---|---|---|---|
| 31 fuller daily services, any day | 177 | 381 | 41% | 35% |
| The other 91, Tue/Wed/Fri/Sat | 269 | 601 | 89% | 4% |
| 28 of the 91, Sun/Mon/Thu | 82 | 185 | 52% | 22% |
| The other 50, Sun/Mon/Thu | 132 | 260 | 92% | 3% |
| All | 660 | 1,427 | 72% | 14% |

- The weekday split came out unprompted: Sun/Mon/Thu against
  Tue/Wed/Fri/Sat (p ≈ 2e-7), matching 2026-09-29, Friday included.
- The 31 fuller daily services: Detroit (10 of them), the Ohio-MSP short
  hops both ways, SLC-BUR, and MSP-PDX 4:00pm, PDX-MSP 1:30pm and 3:00pm,
  SLC-PDX 5:15pm, SLC-CVG 9:45am. CVG-MSP 4:30pm is the worst, about 80%
  full. The report lists every member.
- The 28/50 split clears the bar at about 1 in 150: probably real, still
  settling.
- Most weekly services have 1-3 flights, so for most of them the pool's
  numbers are a better guess than their own record.
- Testing one weekly service at a time against the pool found only 1-6
  outliers and missed the weekday pattern entirely; a broad pattern only
  shows when an attribute is tested across many flights.

## 2026-10-01 — Slope: borrowing from the fleet helps, hold still wins

**Question**: does a slope borrowed from the flight's pool predict T-1
better than its own weekly service's slope?

**Method** (python/PoolBacktest.py): BottomBacktest's leave-one-out scorer,
T-4 anchors, truth within 1.55h of departure. Slope is the median of
per-flight fitted slopes from the weekly service's other flights (own),
every other flight (fleet), or every other flight in its pool (pool); C1
is the weekly service's own in every column. Pool membership was built
with all flights, held-out one included. Mean absolute error in seats,
three cabins:

| Own other flights | Cases | Hold | Global default | Own | Fleet | Pool |
|---|---|---|---|---|---|---|
| 0 | 315 | 0.64 | 2.11 | 2.11 | 0.89 | 0.88 |
| 1 | 530 | 0.77 | 2.13 | 1.82 | 0.97 | 0.97 |
| 2 | 499 | 0.52 | 2.06 | 1.41 | 0.78 | 0.78 |
| 3 | 266 | 0.59 | 1.99 | 1.10 | 0.77 | 0.77 |
| 4+ | 176 | 0.57 | 2.20 | 1.00 | 0.82 | 0.84 |
| All | 1,786 | 0.63 | 2.09 | 1.57 | 0.86 | 0.86 |

- Hold beats every curve, as on 2026-09-28.
- Among curves, a borrowed slope roughly halves the error, even for weekly
  services with 4+ flights of their own.
- Pool and fleet tie: the pools sort by how full flights end up, but the
  decline rate is about the same fleet-wide.
- The global default, what a new weekly service gets today, is the worst.
- "Own" is the median of per-flight slopes, not the live fit (pool_slope).

## 2026-09-29 — Weekday pattern: Tue/Wed/Sat open, Sun/Mon/Thu full

**Question**: are some weekdays reliably more open or more full than others,
across all logged flights and service by service?

**Method**: the live T1 estimate (T1GridReport.last_t1_instances: each
cabin's latest known reading, three cabins summed), one per service-day,
only where the last reading is within 6h of departure. Open ≥ 8, full ≤ 2.
Excluded date ranges left out.

| Day | Flights | Dates | Open | Full |
|---|---|---|---|---|
| Sat | 96 | 2 | 88% | 7% |
| Tue | 203 | 5 | 78% | 13% |
| Wed | 273 | 4 | 77% | 8% |
| Fri | 236 | 6 | 74% | 13% |
| Thu | 278 | 5 | 67% | 18% |
| Mon | 169 | 5 | 64% | 22% |
| Sun | 134 | 5 | 60% | 19% |
| All | 1,389 | | 72% | 14% |

- Adjusting for which routes were logged on which day (Y at the last
  reading within 3h, relative to its route's norm) gives the same order,
  so route mix isn't driving it.
- Friday sits with the light days fleet-wide. Service by service on the
  studied routes it matches Tue/Wed except early afternoon out of Ohio on
  the short hops: CMH-MSP 1:45pm (Tue OO, Wed O, Fri iFi), CMH-DTW 2:15pm,
  CVG-DTW 4:00pm; PDX-MSP 1:45pm has one full Friday.
- Thursday is the fullest weekday on several services, e.g. CMH-MSP 7:15pm
  full all three Thursdays, open both Tuesdays.
- Tue and Wed don't always agree either: MSP-PDX 4:00pm (Tue iii, Wed
  OOiO), CVG-DTW 10:45am (Tue FOF, Wed FFOi).
- Per service, cells hold 2-6 weeks. Telling two days apart takes roughly
  12 weeks per day for a glaring difference (full 1 in 20 vs half the
  time) and 30 for a moderate one (1 in 10 vs 4 in 10), so only glaring
  per-service differences will ever show within a season.
- Saturday rests on two dates.

## 2026-09-29 — Early readings far from the middle mostly hold

**Question**: once a flight reads well open or well full hours out, how
often does T-1 end up somewhere else?

**Method**: all three cabins summed, only readings with all three logged.
Truth is the reading nearest T-1 (within 1.55h of departure). The anchor is
the raw reading nearest T-6 (within 1.5h), T-8 (within 1.5h) or T-12
(within 2h), never inside the truth window. The three horizons are pooled,
so one flight-day can count up to three times.

| Early reading | Days | Ended full (≤2) | Between | Ended open (≥8) |
|---|---|---|---|---|
| 16 or more | 364 | 10 (1 in 36) | 8 | 346 |
| 12–15 | 242 | 14 (1 in 17) | 26 | 202 |
| 8–11 | 471 | 49 (1 in 10) | 130 | 292 |
| 6–7 | 101 | 28 | 58 | 15 (1 in 7) |
| 3–5 | 197 | 89 | 74 | 34 (1 in 6) |
| 0–2 | 203 | 155 | 37 | 11 (1 in 18) |

- The band matters more than the horizon: no steady trend from T-12 to
  T-6 within a band.
- The top band is the most settled: 16 or more ended full about 1 time in
  36. The bottom band is less so: 0–2 ended open 1 time in 18, and a
  quarter of those days ended between.
- From 3 to 7, an early reading says little about where T-1 ends.

## 2026-09-28 — Bedtime reading on Ohio morning departures

**Question**: for Ohio departures before 8:00 local, where T-1 falls while
he's usually asleep, how well does the last reading the night before
predict T-1?

**Method**: all three cabins summed per service-day, full ≤ 2, open ≥ 8.
Truth is the reading nearest T-1 (within 1.55h of departure). Morning
anchor is the last reading checked 20:00-23:59 the evening before the
flight date, typically about T-8. Comparison anchors are the reading
nearest T-8 (within 1.5h) and nearest T-3 (within 1h) on every flight not
in the morning group. Raw readings, no estimator.

| Anchor | Days | Open at anchor | Open → full | Open → between |
|---|---|---|---|---|
| Ohio pre-8am, bedtime reading | 23 | 17 | 0 | 2 |
| All other flights, nearest T-8 | 495 | 329 | 26 (7.9%) | 54 |
| All other flights, nearest T-3 | 647 | 420 | 9 (2.1%) | 31 |

- An open bedtime reading on a morning departure has not yet ended full.
  At the daytime T-8 rate, 0 of 17 would still happen about 1 time in 4,
  so this leans toward the overnight hours adding little decline but does
  not establish it.
- Y alone agrees: 22 open at bedtime, 0 ended full, 3 ended between.
- The morning T-1 truths come from nights he woke early, which are
  unrelated to loads, so the sample is fair but grows slowly.
- Late collapses from ~T-3 to T-1 (Y alone, open to full) concentrate at
  LAX departures: 3 of 89 LAX days against 2 of 254 elsewhere. LAX→CVG
  also fell from Y=9 to Y=3 between 3.4h and 2.7h on 2026-08-06.

## 2026-09-28 — Holding the latest reading beats the decline curve

**Method**: leave-one-out, raw logged readings for both the anchor and the
truth. Truth is the reading nearest T-1 (within 1.55h of departure). The
anchor never uses a reading inside that window. Two readings straddling the
horizon, each within twice the tolerance of it, are interpolated between;
otherwise the anchor is the single reading nearest the horizon, within the
tolerance (1h at T-4, a quarter of the horizon further out). Hold predicts
T-1 equals the anchor. The curve is the estimator the live one replaced,
with leave-one-out coefficients.

**Game-day boxes** (python/HorizonDecisionBacktest.py): all three cabins
summed per service-day, full ≤ 2, open ≥ 8.

| From | Days | Only hold right | Only curve right | Both wrong | Hold: called open, was full | Curve: called open, was full | Curve: called full, was open |
|---|---|---|---|---|---|---|---|
| T-4 | 552 | 176 | 50 | 49 | 6 | 2 | 30 |
| T-6 | 519 | 179 | 61 | 54 | 16 | 6 | 40 |
| T-8 | 452 | 171 | 57 | 60 | 18 | 6 | 46 |
| T-12 | 409 | 155 | 42 | 58 | 17 | 5 | 55 |
| T-24 | 185 | 67 | 24 | 30 | 15 | 2 | 23 |

- Hold calls the right box more often at every horizon tested.
- Hold's misses are the costly kind (called open, was full), and they
  roughly triple from T-4 to T-6 and beyond. The curve's misses are almost
  all the other kind: it calls open flights full, often from a 20+ seat
  reading down to near zero.
- The script lists every called-open-was-full day by service and date, and
  breaks the T-4 calls down per service. Most services have 1-4 scored days.

**Seats, per cabin from T-4** (python/BottomBacktest.py): RMSE, then share
within 1 seat.

| Cabin | Days | Hold T-4 | Live curve | Curve with fitted bottom time |
|---|---|---|---|---|
| Coach | 221 | 2.01, 74% | 2.66, 45% | 2.04, 69% |
| Comfort+ | 170 | 1.50, 87% | 1.85, 74% | 1.50, 85% |
| First | 253 | 1.11, 91% | 1.27, 79% | 1.14, 87% |

- Bottom time: hours before departure at which seats stop dropping. Rules
  that hold by default and use the curve with a fitted bottom only when it
  beat hold by ½ or 1 seat on the service's other days, or on its day
  group's (Mon+Fri, Sat+Sun, Tue-Thu), chose the curve on at most 23 coach
  days and never beat hold.
- The curve predicts more decline than happens in every cabin (mean
  error +0.44 to +1.28 seats).
- T4T1Backtest.py's T-4 bracket interpolates toward the T-1 reading on 504
  of 1118 summed days with a solid T-1; its numbers are flattered by that.

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
