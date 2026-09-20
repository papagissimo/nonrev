import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'nonrev.db')

SCENARIO_TABLES = """
CREATE TABLE scenarios (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT NOT NULL UNIQUE COLLATE NOCASE,
    active  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE scenarioCells (
    scenarioId  INTEGER NOT NULL REFERENCES scenarios(id) ON DELETE CASCADE,
    org         TEXT NOT NULL,
    dest        TEXT NOT NULL,
    dayOfWeek   TEXT NOT NULL,
    PRIMARY KEY (scenarioId, org, dest, dayOfWeek)
);
"""

SCHEMA = """
-- flightSchedule is a snapshot of THIS WEEK's Delta schedule ONLY - not a
-- record of any other week, past or future. Delta doesn't publish anything
-- more durable than that: no flight-number stability, no guarantee a time
-- holds even one week out. Editing a row overwrites it in place with no
-- history kept - the OLD depTime is gone the moment you save, not archived
-- anywhere. Never compare an observations row's depTime against this table
-- to judge whether that reading was "correct" - a mismatch just means the
-- schedule moved since, which happens constantly and is not an error.
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

-- Cabin sizes: NULL = unknown, 0 = the aircraft has no such cabin.
CREATE TABLE aircraftConfigs (
    configKey    TEXT PRIMARY KEY,
    aircraft     TEXT NOT NULL,
    d1           INTEGER,
    first        INTEGER,
    comfortPlus  INTEGER,
    main         INTEGER,
    total        INTEGER,
    status       TEXT,
    note         TEXT,
    source       TEXT
);

CREATE TABLE routeSettings (
    org               TEXT NOT NULL,
    dest              TEXT NOT NULL,
    durationMinutes   INTEGER,
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

-- One row per sitting: every field observed in that sitting is its own
-- column, NULL for anything not observed, 0 only for "observed, none".
-- y/cPlus/firstOrPS/d1 are the can-buy counts (actual/resolved cabin values,
-- a real binary-search result). solo*/pair* are the seat-map selectable
-- counts (single seats / adjacent pairs); blockedTotal is the seat map's X
-- seats across all cabins.
-- cheapY/cheapCPlus/cheapFirstOrPS/cheapD1 are a FROZEN HISTORICAL ARTIFACT
-- as of 2026-09-14: the old glance-entry workflow (a free ceiling/floor
-- glance off Delta's all-flights page before the binary search) is fully
-- retired, "every whiff of it, gone" (his call) - nothing anywhere in this
-- codebase writes to these columns anymore, and no live code path reads them
-- either. They're kept, not dropped, purely to preserve rows logged before
-- this date - a genuine confirmed zero from back then is still 0 in both the
-- cheap and actual column for that cabin, never left implicit; NULL meant no
-- glance was taken that reading.
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
    y               INTEGER,
    cPlus           INTEGER,
    firstOrPS       INTEGER,
    d1              INTEGER,
    soloY           INTEGER,
    soloCPlus       INTEGER,
    soloFirstOrPS   INTEGER,
    soloD1          INTEGER,
    pairY           INTEGER,
    pairCPlus       INTEGER,
    pairFirstOrPS   INTEGER,
    pairD1          INTEGER,
    blockedTotal    INTEGER,
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

-- floorEstimateCoefficients (the cached glance-floor -> mean-actual
-- table) retired 2026-09-14 along with FloorEstimates.py itself and all
-- glance-derived estimation - "every whiff of it, gone" (his call).
-- Table dropped from this schema entirely; an existing live db from
-- before this date may still have the table sitting around unused -
-- harmless, nothing reads or writes it anymore.

-- Pooled per-(org, dest, dayOfWeek, depTime, cabin) decline-curve
-- coefficients - the frozen slope + C1 the live T1 estimator slides to
-- match today's actual readings. See python/DeclineCurveFit.py for the
-- fitting/pooling math and python/settings.py's declineCurveSettings for
-- the tunables. Keyed by every individual depTime actually observed in
-- the data (not a cluster-representative time), so a read-time consumer
-- does a plain exact-match lookup as the common case; DeclineCurveHierarchy.
-- resolve_coefficients falls back to a nearest-depTime match (within
-- clustering.py's SERVICE_GAP_MINUTES) on a miss, so a depTime that's
-- drifted a bit or never had its own observations still inherits its
-- service's derived fit rather than falling through to the global
-- default. c1Hours and slopeSeatsPerHour are independently
-- nullable: a cabin/depTime combination can have a resolvable C1 with no
-- resolvable slope or vice versa (see refresh_decline_curve_coefficients
-- docstring) - a NULL means no fit available yet, not zero.
-- slopeSeatsPerHour is always seats/hour - no other unit is ever used
-- anywhere in this project for this quantity.
-- Recomputed from scratch on every refresh (deleted and rewritten
-- wholesale), never maintained incrementally.
-- nightSlopeRatio/nInstancesNightSlope: tier-4 derived night/day slope
-- ratio (see settings.DEFAULT_DECLINE_CURVE_GLOBAL_DEFAULTS's
-- nightSlopeRatio for what this means). Independently nullable/counted
-- exactly like c1Hours/slopeSeatsPerHour - the pooling function that
-- would populate these (parallel to pool_slope) isn't built yet, so
-- these stay NULL/0 for now and resolution falls through to tiers 1-3;
-- the column exists so DeclineCurveHierarchy.resolve_coefficients
-- already checks it and picks it up the moment it's populated.
CREATE TABLE declineCurveCoefficients (
    org                     TEXT NOT NULL,
    dest                    TEXT NOT NULL,
    dayOfWeek               TEXT NOT NULL,
    depTime                 INTEGER NOT NULL,
    cabin                   TEXT NOT NULL,
    c1Hours                 REAL,
    slopeSeatsPerHour       REAL,
    nightSlopeRatio         REAL,
    nInstancesC1            INTEGER NOT NULL DEFAULT 0,
    nInstancesSlope         INTEGER NOT NULL DEFAULT 0,
    nInstancesNightSlope    INTEGER NOT NULL DEFAULT 0,
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
    nightSlopeRatio     REAL,
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
    nightSlopeRatio     REAL,
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
    org                     TEXT NOT NULL,
    dest                    TEXT NOT NULL,
    dayOfWeek               TEXT NOT NULL,
    depTime                 INTEGER NOT NULL,
    cabin                   TEXT NOT NULL,
    minInstancesC1          INTEGER NOT NULL DEFAULT 1,
    minInstancesSlope       INTEGER NOT NULL DEFAULT 1,
    minInstancesNightSlope  INTEGER NOT NULL DEFAULT 1,
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
    conn.executescript(SCHEMA + SCENARIO_TABLES)
    conn.commit()
    conn.close()
    print(f"Created {DB_PATH}")


if __name__ == "__main__":
    create_db()
