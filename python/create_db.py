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
