"""
DeclineCurveHierarchy.py

Resolves the ACTUAL (c1Hours, slopeSeatsPerHour) to use for one
(org, dest, dayOfWeek, depTime, cabin), given everything
DeclineCurveFit.py and the hand-set override tables know. Kept as its
own module, separate from DeclineCurveFit.py's fitting work and from
T1Estimator.py's live-slide logic - resolving "which tier wins" is a
genuinely different operation from either of those, same reasoning as
DeclineCurveFit.py's own split from GraphObservations.py.

THE FOUR TIERS, most general to most specific:

  1. GLOBAL DEFAULT - one (c1, slope) pair per cabin, settings-driven
     (settings.DEFAULT_DECLINE_CURVE_GLOBAL_DEFAULTS / the
     declineCurveGlobalDefaults settings key), applies to any service
     with nothing more specific. Never route/service-specific, so
     every route/service automatically inherits it with zero setup.

  2. ROUTE OVERRIDE - optional, hand-set, declineCurveRouteOverrides
     (org, dest, cabin). Only exists where he's decided a whole route
     is different enough to be worth a manual number. Most services
     will never have one.

  3. SERVICE OVERRIDE - optional, hand-set,
     declineCurveServiceOverrides (org, dest, dayOfWeek, depTime,
     cabin). Supersedes a route override for that one specific
     service. Also expected to stay rare.

  4. DERIVED - DeclineCurveFit.py's actual fitted-from-data output,
     declineCurveCoefficients. Supersedes every tier above it, but
     ONLY once there's enough data to trust it: a service/cabin's
     nInstancesC1 (or nInstancesSlope) must meet or exceed
     declineCurveThresholds' minInstancesC1 (or minInstancesSlope) for
     that exact (org, dest, dayOfWeek, depTime, cabin) - default
     threshold is 1 (his homage to Bayesian updating: trust derived
     data from a single instance) when no threshold row exists.
     Setting a threshold very high (e.g. 1000) is the deliberate
     escape hatch for a service whose derived numbers look
     unreasonable - pins that service to tiers 1-3 without touching
     derivation itself, which keeps computing and logging into
     declineCurveInstanceFits/declineCurveCoefficients regardless (see
     DeclineCurveFit.refresh_decline_curve_coefficients), so it can be
     watched and the threshold dropped back down once it looks
     reasonable.

  c1Hours, slopeSeatsPerHour, and nightSlopeRatio (the night/day decline-
  rate ratio - see settings.DEFAULT_DECLINE_CURVE_GLOBAL_DEFAULTS and
  DeclineCurveFit.py's piecewise_model) are resolved INDEPENDENTLY
  through these tiers, not as a group - matches how declineCurveCoefficients
  already treats c1/slope (a service/cabin can have a resolvable C1 with
  no resolvable slope or vice versa) and how the override tables are
  shaped (all three columns independently nullable). It's entirely
  possible for c1 to come from tier 4 while slope or nightSlopeRatio for
  the same cabin comes from tier 1, if only some of the three have
  enough instances yet. nightSlopeRatio's tier-4 derivation isn't built
  yet as of this writing (no pooling function populates it), so it
  currently always resolves through tiers 1-3 - the plumbing is real,
  just waiting on data.

  A tier is "available" for a given quantity when it has a non-NULL
  value for that quantity - the resolver walks tiers 4 -> 3 -> 2 -> 1
  per quantity (checking tier 4's threshold first) and uses the first
  one with something to offer. The global default (tier 1) always has
  something (it's a plain dict, not nullable), so resolution never
  fails to produce a value - unlike the raw declineCurveCoefficients
  lookup, which can come back NULL/missing entirely for a
  never-observed service.
"""

from settings import (
    load_settings,
    DECLINE_CURVE_GLOBAL_DEFAULTS_KEY,
    DEFAULT_DECLINE_CURVE_GLOBAL_DEFAULTS,
)

DEFAULT_MIN_INSTANCES = 1

# Maps each resolvable quantity to (global-default settings key, derived/
# override column name, derived instance-count column name, threshold
# column name) - one place that knows how the three quantities line up
# across every table, so adding a fourth quantity later is one entry
# here instead of a new set of parallel functions.
QUANTITY_COLUMNS = {
    "c1":         ("c1Hours", "c1Hours", "nInstancesC1", "minInstancesC1"),
    "slope":      ("slopeSeatsPerHour", "slopeSeatsPerHour", "nInstancesSlope", "minInstancesSlope"),
    "nightRatio": ("nightSlopeRatio", "nightSlopeRatio", "nInstancesNightSlope", "minInstancesNightSlope"),
}


def _load_threshold(conn, org, dest, day_of_week, dep_time, cabin):
    row = conn.execute(
        """SELECT minInstancesC1, minInstancesSlope, minInstancesNightSlope
           FROM declineCurveThresholds
           WHERE org=? AND dest=? AND dayOfWeek=? AND depTime=? AND cabin=?""",
        (org, dest, day_of_week, dep_time, cabin),
    ).fetchone()
    if row is None:
        return {"c1": DEFAULT_MIN_INSTANCES, "slope": DEFAULT_MIN_INSTANCES,
                "nightRatio": DEFAULT_MIN_INSTANCES}
    min_c1, min_slope, min_night = row
    return {
        "c1": min_c1 if min_c1 is not None else DEFAULT_MIN_INSTANCES,
        "slope": min_slope if min_slope is not None else DEFAULT_MIN_INSTANCES,
        "nightRatio": min_night if min_night is not None else DEFAULT_MIN_INSTANCES,
    }


def _load_derived(conn, org, dest, day_of_week, dep_time, cabin):
    row = conn.execute(
        """SELECT c1Hours, slopeSeatsPerHour, nightSlopeRatio,
                  nInstancesC1, nInstancesSlope, nInstancesNightSlope
           FROM declineCurveCoefficients
           WHERE org=? AND dest=? AND dayOfWeek=? AND depTime=? AND cabin=?""",
        (org, dest, day_of_week, dep_time, cabin),
    ).fetchone()
    if row is None:
        return None
    c1, slope, night, n_c1, n_slope, n_night = row
    return {"c1": c1, "slope": slope, "nightRatio": night,
            "nC1": n_c1, "nSlope": n_slope, "nNightRatio": n_night}


def _load_service_override(conn, org, dest, day_of_week, dep_time, cabin):
    row = conn.execute(
        """SELECT c1Hours, slopeSeatsPerHour, nightSlopeRatio
           FROM declineCurveServiceOverrides
           WHERE org=? AND dest=? AND dayOfWeek=? AND depTime=? AND cabin=?""",
        (org, dest, day_of_week, dep_time, cabin),
    ).fetchone()
    return {"c1": row[0], "slope": row[1], "nightRatio": row[2]} if row else None


def _load_route_override(conn, org, dest, cabin):
    row = conn.execute(
        """SELECT c1Hours, slopeSeatsPerHour, nightSlopeRatio
           FROM declineCurveRouteOverrides
           WHERE org=? AND dest=? AND cabin=?""",
        (org, dest, cabin),
    ).fetchone()
    return {"c1": row[0], "slope": row[1], "nightRatio": row[2]} if row else None


def resolve_coefficients(conn, org, dest, day_of_week, dep_time, cabin):
    """Returns {'c1': float, 'slope': float, 'nightRatio': float,
    'c1Tier': str, 'slopeTier': str, 'nightRatioTier': str} - each tier
    string is one of 'derived', 'serviceOverride', 'routeOverride',
    'global', purely informational (e.g. for a future UI badge showing
    which tier is live for a service) and not required by callers that
    just want the numbers.

    slope/nightRatio can legitimately come back None if every tier
    including the global default has a null value for this cabin -
    practically shouldn't happen once the global default is filled in
    with real starting numbers, but not assumed away here."""
    global_defaults = load_settings(
        conn, key=DECLINE_CURVE_GLOBAL_DEFAULTS_KEY, defaults=DEFAULT_DECLINE_CURVE_GLOBAL_DEFAULTS
    )
    global_entry = global_defaults.get(cabin, {})

    derived = _load_derived(conn, org, dest, day_of_week, dep_time, cabin)
    min_n = _load_threshold(conn, org, dest, day_of_week, dep_time, cabin)
    service_ov = _load_service_override(conn, org, dest, day_of_week, dep_time, cabin)
    route_ov = _load_route_override(conn, org, dest, cabin)

    derived_n_key = {"c1": "nC1", "slope": "nSlope", "nightRatio": "nNightRatio"}

    def resolve_one(quantity):
        global_key = QUANTITY_COLUMNS[quantity][0]
        if derived and derived[quantity] is not None and derived[derived_n_key[quantity]] >= min_n[quantity]:
            return derived[quantity], "derived"
        if service_ov and service_ov[quantity] is not None:
            return service_ov[quantity], "serviceOverride"
        if route_ov and route_ov[quantity] is not None:
            return route_ov[quantity], "routeOverride"
        return global_entry.get(global_key), "global"

    c1_val, c1_tier = resolve_one("c1")
    slope_val, slope_tier = resolve_one("slope")
    night_val, night_tier = resolve_one("nightRatio")

    return {"c1": c1_val, "slope": slope_val, "nightRatio": night_val,
            "c1Tier": c1_tier, "slopeTier": slope_tier, "nightRatioTier": night_tier}
