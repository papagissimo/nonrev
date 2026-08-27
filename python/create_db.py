import sqlite3

DB_PATH = "nonrev.db"

SCHEMA = """
CREATE TABLE flightSchedule (
    carrier         TEXT NOT NULL,
    flightNumber    TEXT NOT NULL,
    org             TEXT NOT NULL,
    dest            TEXT NOT NULL,
    dayOfWeek       TEXT NOT NULL,
    depTime         INTEGER NOT NULL,
    aircraftConfig  TEXT NOT NULL,
    confirmed       INTEGER NOT NULL DEFAULT 0,
    verdict         TEXT,
    ignore          INTEGER NOT NULL DEFAULT 0,
    humanReviewed   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (carrier, flightNumber, org, dest, dayOfWeek)
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

CREATE TABLE routeDurations (
    org               TEXT NOT NULL,
    dest              TEXT NOT NULL,
    durationMinutes   INTEGER NOT NULL,
    confirmed         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (org, dest)
);

CREATE TABLE observations (
    observationId   INTEGER PRIMARY KEY AUTOINCREMENT,
    carrier         TEXT NOT NULL,
    flightNumber    TEXT,
    org             TEXT NOT NULL,
    dest            TEXT NOT NULL,
    flightDate      TEXT NOT NULL,
    checkTimestamp  TEXT NOT NULL,
    hoursBeforeDep  REAL,
    readingType     TEXT NOT NULL CHECK (readingType IN ('avail', 'soloSelect', 'pairSelect')),
    y               INTEGER,
    cPlus           INTEGER,
    firstOrPS       INTEGER,
    d1              INTEGER,
    nextDesiredLog  TEXT
);

CREATE INDEX idxObservationsFlightDay
    ON observations (carrier, flightNumber, org, dest, flightDate);

-- Both flag tables below are the free-text "hey, look here" mechanism -
-- deliberately NOT the same thing as flightSchedule.verdict (a
-- multi-week (flightNumber, dayOfWeek) pattern judgment). These are
-- single mutable fields scoped to one specific flight-date instance:
-- writing over one replaces whatever was there, no history kept.
-- No fixed vocabulary - plain text, searchable later the same
-- substring-filter way ObservationsBrowser already works.

CREATE TABLE flightDayFlag (
    carrier       TEXT NOT NULL,
    flightNumber  TEXT NOT NULL,
    org           TEXT NOT NULL,
    dest          TEXT NOT NULL,
    flightDate    TEXT NOT NULL,
    flag          TEXT,
    PRIMARY KEY (carrier, flightNumber, org, dest, flightDate)
);

CREATE TABLE routeDayFlag (
    carrier     TEXT NOT NULL,
    org         TEXT NOT NULL,
    dest        TEXT NOT NULL,
    flightDate  TEXT NOT NULL,
    flag        TEXT,
    PRIMARY KEY (carrier, org, dest, flightDate)
);
"""


def create_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    print(f"Created {DB_PATH}")


if __name__ == "__main__":
    create_db()
