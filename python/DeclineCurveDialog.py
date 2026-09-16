"""
DeclineCurveDialog backend: the UI for everything DeclineCurveHierarchy.py
resolves - global defaults (tier 1), route/service overrides (tiers 2-3),
per-service thresholds (gate on tier 4), and the visibility lookup
(declineCurveInstanceFits next to declineCurveCoefficients, same data
ShowServiceDetail.py's console report shows, just rendered as a page
instead of printed).

Kept as its own dialog rather than folded into PoolingSettingsDialog:
that page's declineCurveSettings block is about HOW fitting works
(the step-change loop's tunables); this page is about WHAT VALUE gets
used once fitting is done - genuinely a different concern, and this
one was always going to need more surface (four tables, a lookup) than
a couple of number fields.

Route/service overrides and thresholds all use each table's own SQLite
rowid as their id for the add/edit/delete pattern (same shape as
PoolingSettingsDialog.save_excluded_date_ranges) - simpler than
inventing a synthetic key when rowid already does the job.
"""

from settings import (
    load_settings, save_settings,
    DECLINE_CURVE_GLOBAL_DEFAULTS_KEY, DEFAULT_DECLINE_CURVE_GLOBAL_DEFAULTS,
)
from DeclineCurveHierarchy import resolve_coefficients, DEFAULT_MIN_INSTANCES
from clustering import cluster_services, service_representative

CABINS = ["y", "cPlus", "firstOrPS", "d1"]


# ---------- Tier 1: global defaults ----------

def get_global_defaults(conn):
    return load_settings(conn, key=DECLINE_CURVE_GLOBAL_DEFAULTS_KEY, defaults=DEFAULT_DECLINE_CURVE_GLOBAL_DEFAULTS)


def save_global_defaults(conn, payload):
    """payload: {cabin: {c1Hours, slopeSeatsPerHour, nightSlopeRatio}, ...}
    - one entry per cabin, all three required (this tier has no NULLs;
    it's the fallback of last resort, so it always needs a real
    number). nightSlopeRatio added 2026-09-16 - previously silently
    dropped on every save here (a real bug: saving global defaults
    through this dialog would have erased the night-ratio default,
    since it round-tripped only c1Hours/slopeSeatsPerHour)."""
    cleaned = {}
    for cabin in CABINS:
        entry = payload.get(cabin) or {}
        cleaned[cabin] = {
            'c1Hours': float(entry['c1Hours']),
            'slopeSeatsPerHour': float(entry['slopeSeatsPerHour']),
            'nightSlopeRatio': float(entry['nightSlopeRatio']),
        }
    save_settings(conn, cleaned, key=DECLINE_CURVE_GLOBAL_DEFAULTS_KEY)
    return cleaned


# ---------- Tier 2: route overrides ----------

def get_route_overrides(conn):
    rows = conn.execute(
        "SELECT rowid, org, dest, cabin, c1Hours, slopeSeatsPerHour, nightSlopeRatio FROM declineCurveRouteOverrides ORDER BY org, dest, cabin"
    ).fetchall()
    return {'overrides': [
        {'id': rid, 'org': org, 'dest': dest, 'cabin': cabin, 'c1Hours': c1, 'slopeSeatsPerHour': slope, 'nightSlopeRatio': night}
        for rid, org, dest, cabin, c1, slope, night in rows
    ]}


def save_route_overrides(conn, payload):
    """payload: { overrides: [{ id (existing rowid, or null for new),
    org, dest, cabin, c1Hours, slopeSeatsPerHour, nightSlopeRatio (each
    independently null/blank - falls through to the next tier down),
    deleted (bool) }] }"""
    for entry in payload.get('overrides', []):
        if not entry.get('id') or entry.get('deleted'):
            continue
        conn.execute(
            "UPDATE declineCurveRouteOverrides SET org=?, dest=?, cabin=?, c1Hours=?, slopeSeatsPerHour=?, nightSlopeRatio=? WHERE rowid=?",
            (entry['org'], entry['dest'], entry['cabin'],
             _blank_to_none(entry.get('c1Hours')), _blank_to_none(entry.get('slopeSeatsPerHour')),
             _blank_to_none(entry.get('nightSlopeRatio')),
             entry['id']),
        )
    for entry in payload.get('overrides', []):
        if entry.get('id') and entry.get('deleted'):
            conn.execute("DELETE FROM declineCurveRouteOverrides WHERE rowid=?", (entry['id'],))
    for entry in payload.get('overrides', []):
        if not entry.get('id') and not entry.get('deleted'):
            conn.execute(
                "INSERT INTO declineCurveRouteOverrides (org, dest, cabin, c1Hours, slopeSeatsPerHour, nightSlopeRatio) VALUES (?,?,?,?,?,?)",
                (entry['org'], entry['dest'], entry['cabin'],
                 _blank_to_none(entry.get('c1Hours')), _blank_to_none(entry.get('slopeSeatsPerHour')),
                 _blank_to_none(entry.get('nightSlopeRatio'))),
            )
    conn.commit()
    return {'savedCount': len(payload.get('overrides', []))}


# ---------- Tier 3: service overrides ----------

def get_service_overrides(conn):
    rows = conn.execute(
        """SELECT rowid, org, dest, dayOfWeek, depTime, cabin, c1Hours, slopeSeatsPerHour, nightSlopeRatio
           FROM declineCurveServiceOverrides ORDER BY org, dest, dayOfWeek, depTime, cabin"""
    ).fetchall()
    return {'overrides': [
        {'id': rid, 'org': org, 'dest': dest, 'dayOfWeek': dow, 'depTime': dep_time,
         'cabin': cabin, 'c1Hours': c1, 'slopeSeatsPerHour': slope, 'nightSlopeRatio': night}
        for rid, org, dest, dow, dep_time, cabin, c1, slope, night in rows
    ]}


def save_service_overrides(conn, payload):
    """Same shape/pattern as save_route_overrides, plus dayOfWeek/depTime."""
    for entry in payload.get('overrides', []):
        if not entry.get('id') or entry.get('deleted'):
            continue
        conn.execute(
            """UPDATE declineCurveServiceOverrides
               SET org=?, dest=?, dayOfWeek=?, depTime=?, cabin=?, c1Hours=?, slopeSeatsPerHour=?, nightSlopeRatio=?
               WHERE rowid=?""",
            (entry['org'], entry['dest'], entry['dayOfWeek'], int(entry['depTime']), entry['cabin'],
             _blank_to_none(entry.get('c1Hours')), _blank_to_none(entry.get('slopeSeatsPerHour')),
             _blank_to_none(entry.get('nightSlopeRatio')),
             entry['id']),
        )
    for entry in payload.get('overrides', []):
        if entry.get('id') and entry.get('deleted'):
            conn.execute("DELETE FROM declineCurveServiceOverrides WHERE rowid=?", (entry['id'],))
    for entry in payload.get('overrides', []):
        if not entry.get('id') and not entry.get('deleted'):
            conn.execute(
                """INSERT INTO declineCurveServiceOverrides
                   (org, dest, dayOfWeek, depTime, cabin, c1Hours, slopeSeatsPerHour, nightSlopeRatio) VALUES (?,?,?,?,?,?,?,?)""",
                (entry['org'], entry['dest'], entry['dayOfWeek'], int(entry['depTime']), entry['cabin'],
                 _blank_to_none(entry.get('c1Hours')), _blank_to_none(entry.get('slopeSeatsPerHour')),
                 _blank_to_none(entry.get('nightSlopeRatio'))),
            )
    conn.commit()
    return {'savedCount': len(payload.get('overrides', []))}


# ---------- Tier 4 gate: thresholds ----------

def get_thresholds(conn):
    rows = conn.execute(
        """SELECT rowid, org, dest, dayOfWeek, depTime, cabin, minInstancesC1, minInstancesSlope, minInstancesNightSlope
           FROM declineCurveThresholds ORDER BY org, dest, dayOfWeek, depTime, cabin"""
    ).fetchall()
    return {'thresholds': [
        {'id': rid, 'org': org, 'dest': dest, 'dayOfWeek': dow, 'depTime': dep_time,
         'cabin': cabin, 'minInstancesC1': min_c1, 'minInstancesSlope': min_slope, 'minInstancesNightSlope': min_night}
        for rid, org, dest, dow, dep_time, cabin, min_c1, min_slope, min_night in rows
    ], 'defaultMinInstances': DEFAULT_MIN_INSTANCES}


def save_thresholds(conn, payload):
    """Same pattern again. minInstancesC1/minInstancesSlope/
    minInstancesNightSlope default to DEFAULT_MIN_INSTANCES (1) if left
    blank - a row only needs to exist at all when he wants something
    OTHER than the default, e.g. cranked way up (1000) as the
    kill-switch escape hatch."""
    for entry in payload.get('thresholds', []):
        if not entry.get('id') or entry.get('deleted'):
            continue
        conn.execute(
            """UPDATE declineCurveThresholds
               SET org=?, dest=?, dayOfWeek=?, depTime=?, cabin=?, minInstancesC1=?, minInstancesSlope=?, minInstancesNightSlope=?
               WHERE rowid=?""",
            (entry['org'], entry['dest'], entry['dayOfWeek'], int(entry['depTime']), entry['cabin'],
             int(entry.get('minInstancesC1') or DEFAULT_MIN_INSTANCES),
             int(entry.get('minInstancesSlope') or DEFAULT_MIN_INSTANCES),
             int(entry.get('minInstancesNightSlope') or DEFAULT_MIN_INSTANCES),
             entry['id']),
        )
    for entry in payload.get('thresholds', []):
        if entry.get('id') and entry.get('deleted'):
            conn.execute("DELETE FROM declineCurveThresholds WHERE rowid=?", (entry['id'],))
    for entry in payload.get('thresholds', []):
        if not entry.get('id') and not entry.get('deleted'):
            conn.execute(
                """INSERT INTO declineCurveThresholds
                   (org, dest, dayOfWeek, depTime, cabin, minInstancesC1, minInstancesSlope, minInstancesNightSlope)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (entry['org'], entry['dest'], entry['dayOfWeek'], int(entry['depTime']), entry['cabin'],
                 int(entry.get('minInstancesC1') or DEFAULT_MIN_INSTANCES),
                 int(entry.get('minInstancesSlope') or DEFAULT_MIN_INSTANCES),
                 int(entry.get('minInstancesNightSlope') or DEFAULT_MIN_INSTANCES)),
            )
    conn.commit()
    return {'savedCount': len(payload.get('thresholds', []))}


# ---------- Visibility lookup ----------

def get_route_day_options(conn):
    """Distinct (org, dest, dayOfWeek) combinations that actually have
    derived data - populates the lookup page's picker so he's never
    guessing at a route/day that has nothing to show."""
    rows = conn.execute(
        "SELECT DISTINCT org, dest, dayOfWeek FROM declineCurveCoefficients ORDER BY org, dest, dayOfWeek"
    ).fetchall()
    return {'options': [{'org': o, 'dest': d, 'dayOfWeek': w} for o, d, w in rows]}


def _blank_to_none(v):
    if v is None or v == '':
        return None
    return float(v)


def get_service_detail(conn, org, dest, day_of_week):
    """The visibility view: every SERVICE (clustered depTimes, same
    SERVICE_GAP_MINUTES grouping DeclineCurveFit.py itself already uses
    to identify services - see clustering.py) for this (org, dest,
    dayOfWeek), and per cabin, the derived aggregate next to every
    instance fit that fed it, plus which tier the hierarchy currently
    resolves to for that service's representative depTime.

    Grouped rather than one row per raw depTime (as this used to be):
    declineCurveInstanceFits duplicates every instance fit under EVERY
    raw depTime its service has ever spanned (see
    refresh_decline_curve_coefficients), so a naive per-depTime instance
    count triple-counts the same real flights whenever a service's
    history has drifted across a few nearby depTimes - deduped here by
    flightDate (a real flight only has one), so nInstances reflects
    distinct flights, not duplicated rows. derivedC1/derivedSlope/etc.
    are read from any one member depTime's row (they're identical
    across a service's members by construction - same pooled fit,
    written once per raw depTime it spans) rather than re-aggregated."""
    dep_times = [
        row[0] for row in conn.execute(
            """SELECT DISTINCT depTime FROM declineCurveCoefficients
               WHERE org=? AND dest=? AND dayOfWeek=? ORDER BY depTime""",
            (org, dest, day_of_week),
        ).fetchall()
    ]

    services = []
    for cluster in cluster_services(dep_times):
        rep_time = service_representative(cluster)
        cabins_out = []
        for cabin in CABINS:
            agg = None
            instances_by_flight_date = {}
            for dep_time in cluster:
                if agg is None:
                    row = conn.execute(
                        """SELECT c1Hours, slopeSeatsPerHour, nightSlopeRatio,
                                  nInstancesC1, nInstancesSlope, nInstancesNightSlope
                           FROM declineCurveCoefficients
                           WHERE org=? AND dest=? AND dayOfWeek=? AND depTime=? AND cabin=?""",
                        (org, dest, day_of_week, dep_time, cabin),
                    ).fetchone()
                    if row is not None:
                        agg = row
                for flight_date, ic1, islope, n_int, n_step in conn.execute(
                    """SELECT flightDate, c1Hours, slopeSeatsPerHour, nInterior, nStepChanges
                       FROM declineCurveInstanceFits
                       WHERE org=? AND dest=? AND dayOfWeek=? AND depTime=? AND cabin=?""",
                    (org, dest, day_of_week, dep_time, cabin),
                ).fetchall():
                    instances_by_flight_date[flight_date] = (flight_date, ic1, islope, n_int, n_step)

            if agg is None:
                continue
            c1, slope, night, n_c1, n_slope, n_night = agg
            resolved = resolve_coefficients(conn, org, dest, day_of_week, rep_time, cabin)
            instances = sorted(instances_by_flight_date.values(), key=lambda t: t[0])

            cabins_out.append({
                'cabin': cabin,
                'derivedC1': c1, 'derivedSlope': slope, 'derivedNightSlopeRatio': night,
                'nInstancesC1': n_c1, 'nInstancesSlope': n_slope, 'nInstancesNightSlope': n_night,
                'nInstances': len(instances),
                'liveC1': resolved['c1'], 'liveC1Tier': resolved['c1Tier'],
                'liveSlope': resolved['slope'], 'liveSlopeTier': resolved['slopeTier'],
                'liveNightSlopeRatio': resolved.get('nightRatio'), 'liveNightSlopeRatioTier': resolved.get('nightRatioTier'),
                'instances': [
                    {'flightDate': fd, 'c1Hours': ic1, 'slopeSeatsPerHour': islope,
                     'nInterior': n_int, 'nStepChanges': n_step}
                    for fd, ic1, islope, n_int, n_step in instances
                ],
            })
        services.append({'depTime': rep_time, 'memberDepTimes': cluster, 'cabins': cabins_out})

    return {'org': org, 'dest': dest, 'dayOfWeek': day_of_week, 'services': services}
