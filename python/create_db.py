import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'nonrev.db')

SCHEMA = """
CREATE TABLE flightSchedule (
    carrier                              TEXT NOT NULL,
    carriersFltNum_notStable_DO_NOT_USE  TEXT NOT NULL,
    org                                  TEXT NOT NULL,
    dest                                 TEXT NOT NULL,
    dayOfWeek                           TEXT NOT NULL,
    depTime                             INTEGER NOT NULL,
    aircraftConfig                      TEXT NOT NULL,
    verdict                             TEXT,
    ignore                              INTEGER NOT NULL DEFAULT 0,
    humanReviewed                       INTEGER NOT NULL DEFAULT 0,
    verdictType                         TEXT NOT NULL DEFAULT 'info'
);

CREATE TABLE aircraftConfigs (
    configKey    TEXT PRIMARY KEY,
    aircraft     TEXT NOT NULL,
    d1           INTEGER NOT NULL DEFAULT 0,
    first        INTEGER NOT NULL DEFAULT 0,
    comfortPlus  INTEGER NOT NULL DEFAULT 0,
    main         INTEGER NOT NULL DEFAULT 0,
    total        INTEGER NOT NULL DEFAULT 0,
    status       TEXT,
    note         TEXT,
    source       TEXT
);

CREATE TABLE routeSettings (
    org               TEXT NOT NULL,
    dest              TEXT NOT NULL,
    durationMinutes   INTEGER,
    studyThisRoute    INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (org, dest)
);

-- Global, not per-route - a date range to leave out of every pooling
-- consumer (verdict/classification, decline-curve fitting, the weekday
-- chart, dayGroupings stats), not just one graphing tool. Ranges, not
-- individual dates, since that's how the real cases show up (a
-- three-day-weekend window, an early-August anomaly window) - a range
-- collapsing to one day is just startDate=endDate.
CREATE TABLE excludedDateRanges (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    startDate   TEXT NOT NULL,
    endDate     TEXT NOT NULL,
    reason      TEXT
);

-- Free-text grouping label per (org, dest, dayOfWeek), his own word "day
-- grouping" - deliberately not "category" or "bucket". Two rows for the
-- same route sharing the same string are pooled together when computing
-- open/full counts; different strings mean different pools. The string
-- carries no meaning to the software, only to him - no fixed vocabulary,
-- nothing enumerated. No confirmed flag here (routeDurations, the old
-- table this superseded, had one) - tried that once, decided it wasn't earning its keep
-- for this particular table and dropped it; every row always holds a
-- real, meaningful label (starts as one of a few sensible defaults, not
-- a placeholder), so there's no "unreviewed vs reviewed" state worth
-- tracking.
CREATE TABLE dayGroupings (
    org           TEXT NOT NULL,
    dest          TEXT NOT NULL,
    dayOfWeek     TEXT NOT NULL,
    dayGrouping   TEXT NOT NULL,
    PRIMARY KEY (org, dest, dayOfWeek)
);

-- y/cPlus/firstOrPS/d1 are the actual/resolved cabin values (unchanged
-- meaning - a real binary-search result, or a locked-in 9/0 auto-filled
-- from the matching cheap* column). cheapY/cheapCPlus/cheapFirstOrPS/
-- cheapD1 hold whatever was free to glance off Delta's all-flights page
-- before the binary search: a 9 (ceiling - industry-wide hard cap, Delta
-- never displays past 9) or a confirmed floor (0, or a genuine "at least
-- N" for N up to 5 in practice, sometimes higher). NULL in a cheap*
-- column means no glance was taken that reading, not "floor of zero" -
-- a genuine confirmed zero is written directly as 0, same in both the
-- cheap and actual column for that cabin, never left implicit.
CREATE TABLE observations (
    observationId   INTEGER PRIMARY KEY AUTOINCREMENT,
    carrier         TEXT NOT NULL,
    carriersFltNum_notStable_DO_NOT_USE  TEXT,
    org             TEXT NOT NULL,
    dest            TEXT NOT NULL,
    flightDate      TEXT NOT NULL,
    checkTimestamp  TEXT NOT NULL,
    hoursBeforeDep  REAL,
    depTime         INTEGER,
    readingType     TEXT NOT NULL CHECK (readingType IN ('avail', 'soloSelect', 'pairSelect')),
    y               INTEGER,
    cPlus           INTEGER,
    firstOrPS       INTEGER,
    d1              INTEGER,
    cheapY          INTEGER,
    cheapCPlus      INTEGER,
    cheapFirstOrPS  INTEGER,
    cheapD1         INTEGER,
    nextDesiredLog  TEXT
);

CREATE INDEX idxObservationsFlightDay
    ON observations (carrier, org, dest, depTime, flightDate);

-- Both flag tables below are the free-text "hey, look here" mechanism -
-- deliberately NOT the same thing as flightSchedule.verdict (a
-- multi-week (org, dest, depTime, dayOfWeek) pattern judgment). These are
-- single mutable fields scoped to one specific flight-date instance:
-- writing over one replaces whatever was there, no history kept.
-- No fixed vocabulary - plain text, searchable later the same
-- substring-filter way ObservationsBrowser already works.

CREATE TABLE flightDayFlag (
    carrier       TEXT NOT NULL,
    carriersFltNum_notStable_DO_NOT_USE  TEXT,
    org           TEXT NOT NULL,
    dest          TEXT NOT NULL,
    flightDate    TEXT NOT NULL,
    depTime       INTEGER,
    flag          TEXT,
    PRIMARY KEY (carrier, org, dest, depTime, flightDate)
);

CREATE TABLE routeDayFlag (
    carrier     TEXT NOT NULL,
    org         TEXT NOT NULL,
    dest        TEXT NOT NULL,
    flightDate  TEXT NOT NULL,
    flag        TEXT,
    PRIMARY KEY (carrier, org, dest, flightDate)
);

-- Cached per-(cabin, cheap-floor-value) mean of the actual (binary-search-
-- confirmed) value, computed from every historical row where both a cheap
-- glance and a real actual value are present for that cabin on the same
-- row/session. Recomputed from scratch (never maintained incrementally)
-- whenever refreshed - see python/FloorEstimates.py for the math and the
-- refresh triggers. Powers the decimal-valued "estimate" shown wherever
-- only a glance is available (Previous readings, the live T1 column, and
-- GraphObservations' t1New) - deliberately distinct from a real actual
-- value, which is always a whole number; the decimal point alone is the
-- signal a consumer needs to tell the two apart, no separate flag/column
-- required.
CREATE TABLE floorEstimateCoefficients (
    cabin        TEXT NOT NULL,
    floorValue   INTEGER NOT NULL,
    meanActual   REAL NOT NULL,
    sampleCount  INTEGER NOT NULL,
    PRIMARY KEY (cabin, floorValue)
);

-- Pooled per-(org, dest, dayOfWeek, depTime, cabin) decline-curve
-- coefficients - the frozen slope + C1 the live T1 estimator slides to
-- match today's actual readings. See python/DeclineCurveFit.py for the
-- fitting/pooling math and python/settings.py's declineCurveSettings for
-- the tunables. Keyed by every individual depTime actually observed in
-- the data (not a cluster-representative time), so a read-time consumer
-- does a plain exact-match lookup - no live reclustering needed to use
-- these numbers. c1Hours and slopeSeatsPerHour are independently
-- nullable: a cabin/depTime combination can have a resolvable C1 with no
-- resolvable slope or vice versa (see refresh_decline_curve_coefficients
-- docstring) - a NULL means no fit available yet, not zero.
-- slopeSeatsPerHour is always seats/hour - no other unit is ever used
-- anywhere in this project for this quantity.
-- Recomputed from scratch on every refresh (deleted and rewritten
-- wholesale), never maintained incrementally - same pattern as
-- floorEstimateCoefficients above.
CREATE TABLE declineCurveCoefficients (
    org                 TEXT NOT NULL,
    dest                TEXT NOT NULL,
    dayOfWeek           TEXT NOT NULL,
    depTime             INTEGER NOT NULL,
    cabin               TEXT NOT NULL,
    c1Hours             REAL,
    slopeSeatsPerHour   REAL,
    nInstancesC1        INTEGER NOT NULL DEFAULT 0,
    nInstancesSlope     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (org, dest, dayOfWeek, depTime, cabin)
);

-- Coefficients hierarchy, tiers 2-4 (tier 1, the global default, is a
-- settings blob - see settings.DEFAULT_DECLINE_CURVE_GLOBAL_DEFAULTS).
-- See DeclineCurveFit.py's "COEFFICIENTS HIERARCHY" docstring section
-- for the full design.

-- Tier 2: optional hand-set override for a whole route (org, dest),
-- applying to every dayOfWeek/depTime on it that has nothing more
-- specific. Sparse by design - a route with no real reason to differ
-- from the global default simply has no row here. c1Hours/
-- slopeSeatsPerHour independently nullable, same convention as
-- declineCurveCoefficients: a NULL means "no override for this
-- quantity", falls through to the next tier down rather than a literal
-- zero.
CREATE TABLE declineCurveRouteOverrides (
    org                 TEXT NOT NULL,
    dest                TEXT NOT NULL,
    cabin               TEXT NOT NULL,
    c1Hours             REAL,
    slopeSeatsPerHour   REAL,
    PRIMARY KEY (org, dest, cabin)
);

-- Tier 3: optional hand-set override for one specific service (org,
-- dest, dayOfWeek, depTime) - supersedes a route override where both
-- exist. Same sparse/nullable convention as the route-override table
-- above.
CREATE TABLE declineCurveServiceOverrides (
    org                 TEXT NOT NULL,
    dest                TEXT NOT NULL,
    dayOfWeek           TEXT NOT NULL,
    depTime             INTEGER NOT NULL,
    cabin               TEXT NOT NULL,
    c1Hours             REAL,
    slopeSeatsPerHour   REAL,
    PRIMARY KEY (org, dest, dayOfWeek, depTime, cabin)
);

-- Minimum instance count required before tier 4 (derived, see
-- declineCurveCoefficients above) is trusted over tiers 1-3, per
-- (org, dest, dayOfWeek, depTime, cabin). Default is 1 (his homage to
-- Bayesian updating - trust derived data starting from a single
-- instance) when no row exists here; setting a row's value very high
-- (e.g. 1000) is the deliberate escape hatch for a service whose
-- derived coefficients look unreasonable - it effectively pins that
-- service to tiers 1-3 while derivation keeps computing and logging
-- into declineCurveInstanceFits/declineCurveCoefficients regardless,
-- so it can be watched and the threshold dropped back down once it
-- looks reasonable. c1Hours and slopeSeatsPerHour use separate
-- thresholds since one can be well-constrained (C1, even from an
-- all-9/all-0 instance) while the other isn't (slope, which needs a
-- real interior reading) - see DeclineCurveFit.py.
CREATE TABLE declineCurveThresholds (
    org                   TEXT NOT NULL,
    dest                  TEXT NOT NULL,
    dayOfWeek             TEXT NOT NULL,
    depTime               INTEGER NOT NULL,
    cabin                 TEXT NOT NULL,
    minInstancesC1        INTEGER NOT NULL DEFAULT 1,
    minInstancesSlope     INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (org, dest, dayOfWeek, depTime, cabin)
);

-- Visibility table: one row per (service, cabin, flight-day instance)'s
-- OWN independently-fitted coefficients - fit_instance's output,
-- persisted rather than living only in memory during a refresh run.
-- Sits alongside the aggregated declineCurveCoefficients row for the
-- same service/cabin so he can see, at a glance, how the individual
-- instances that feed a derived aggregate actually look - the direct
-- fix for "I can't see it work" (his words). Recomputed from scratch on
-- every refresh, same pattern as declineCurveCoefficients.
CREATE TABLE declineCurveInstanceFits (
    org                 TEXT NOT NULL,
    dest                TEXT NOT NULL,
    dayOfWeek           TEXT NOT NULL,
    depTime             INTEGER NOT NULL,
    flightDate          TEXT NOT NULL,
    cabin               TEXT NOT NULL,
    c1Hours             REAL NOT NULL,
    slopeSeatsPerHour   REAL,
    nInterior           INTEGER NOT NULL,
    nPoints             INTEGER NOT NULL,
    nStepChanges        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (org, dest, dayOfWeek, depTime, flightDate, cabin)
);
"""


def create_db():
    if os.path.exists(DB_PATH):
        raise SystemExit(
            f"{DB_PATH} already exists - create_db.py is only for a from-scratch "
            f"setup. Delete it first if you really mean to start over."
        )
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    print(f"Created {DB_PATH}")


if __name__ == "__main__":
    create_db()
