"""
ShowServiceDetail.py

Console report for one (org, dest, dayOfWeek) - his direct "let me see
it" tool. Shows, per cabin, every raw depTime's derived aggregate
(declineCurveCoefficients) side by side with the individual instance
fits that fed it (declineCurveInstanceFits), plus what the coefficients
hierarchy currently resolves to for that exact depTime/cabin (which
tier is actually live - see DeclineCurveHierarchy.py).

Run:
    python3 ShowServiceDetail.py <org> <dest> <dayOfWeek>
    e.g. python3 ShowServiceDetail.py pdx lax Tue
"""

import sqlite3
import sys
import os

from DeclineCurveHierarchy import resolve_coefficients

# Anchored to this script's own location, not cwd - see the same fix
# in DeclineCurveFit.py's DB_PATH for why a bare "nonrev.db" is unsafe.
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'nonrev.db')


def show(conn, org, dest, day_of_week):
    dep_times = [
        row[0] for row in conn.execute(
            """SELECT DISTINCT depTime FROM declineCurveCoefficients
               WHERE org=? AND dest=? AND dayOfWeek=? ORDER BY depTime""",
            (org, dest, day_of_week),
        ).fetchall()
    ]
    if not dep_times:
        print(f"No derived data yet for {org}-{dest} {day_of_week}.")
        return

    for dep_time in dep_times:
        hh, mm = divmod(dep_time, 60)
        print(f"=== {org}-{dest} {day_of_week} ~{hh:02d}{mm:02d} ===")
        for cabin in ["y", "cPlus", "firstOrPS", "d1"]:
            agg = conn.execute(
                """SELECT c1Hours, slopeSeatsPerHour, nightSlopeRatio,
                          nInstancesC1, nInstancesSlope, nInstancesNightSlope
                   FROM declineCurveCoefficients
                   WHERE org=? AND dest=? AND dayOfWeek=? AND depTime=? AND cabin=?""",
                (org, dest, day_of_week, dep_time, cabin),
            ).fetchone()
            if agg is None:
                continue
            c1, slope, night, n_c1, n_slope, n_night = agg
            resolved = resolve_coefficients(conn, org, dest, day_of_week, dep_time, cabin)

            c1_str = f"{c1:.2f}h" if c1 is not None else "—"
            slope_str = f"{slope:.2f}/h" if slope is not None else "—"
            night_str = f"{night:.2f}" if night is not None else "—"
            live_slope_str = (
                f"slope={resolved['slope']:.2f}/h [{resolved['slopeTier']}]"
                if resolved['slope'] is not None else "no slope resolvable"
            )
            live_night_str = (
                f"nightRatio={resolved['nightRatio']:.2f} [{resolved['nightRatioTier']}]"
                if resolved['nightRatio'] is not None else "no night ratio resolvable"
            )
            print(f"  [{cabin}] derived: C1={c1_str} (n={n_c1})  slope={slope_str} (n={n_slope})  "
                  f"nightRatio={night_str} (n={n_night})  |  LIVE: C1={resolved['c1']:.2f}h "
                  f"[{resolved['c1Tier']}]  {live_slope_str}  {live_night_str}")

            instances = conn.execute(
                """SELECT flightDate, c1Hours, slopeSeatsPerHour, nInterior, nStepChanges
                   FROM declineCurveInstanceFits
                   WHERE org=? AND dest=? AND dayOfWeek=? AND depTime=? AND cabin=?
                   ORDER BY flightDate""",
                (org, dest, day_of_week, dep_time, cabin),
            ).fetchall()
            for flight_date, inst_c1, inst_slope, n_interior, n_step in instances:
                inst_slope_str = f"{inst_slope:.2f}/h" if inst_slope is not None else "— (unidentifiable, e.g. never left 9)"
                step_str = f", {n_step} step change(s) corrected" if n_step else ""
                print(f"      {flight_date}: C1={inst_c1:.2f}h  slope={inst_slope_str}{step_str}")
        print()


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: python3 ShowServiceDetail.py <org> <dest> <dayOfWeek>")
        print("  e.g. python3 ShowServiceDetail.py pdx lax Tue")
        sys.exit(1)
    conn = sqlite3.connect(DB_PATH)
    show(conn, sys.argv[1], sys.argv[2], sys.argv[3])
