"""
The typical move of a flight's summed can-buy total from a reading to its
golden ticket, by how far out the reading was taken. The live T1 estimate
is the held reading plus this move (T1Estimator).

The move at each of HORIZON_BANDS is the mean over every finished flight
(TrustPools.settled_flights) with a reading within that horizon's band. It
is zero inside ZERO_INSIDE_HOURS, interpolated between points, and held at
the farthest horizon's value beyond it. One move serves the whole fleet;
per-pool moves did no better (findings.md, 2026-10-05). An estimate never
goes below zero seats.
"""
import numpy as np

HORIZON_BANDS = {24: 6.0, 12: 3.0, 8: 2.0}
ZERO_INSIDE_HOURS = 6.0


def reading_near(flight, hours, band):
    near = [(abs(reading_hours - hours), seats) for reading_hours, seats in flight['early']
            if abs(reading_hours - hours) <= band]
    return min(near)[1] if near else None


def drift_points(flights):
    points = [(ZERO_INSIDE_HOURS, 0.0)]
    for hours, band in sorted(HORIZON_BANDS.items()):
        moves = [flight['t1'] - held for flight in flights
                 if (held := reading_near(flight, hours, band)) is not None]
        if moves:
            points.append((float(hours), float(np.mean(moves))))
    return points


def drift_at(points, hours):
    if hours is None or hours <= ZERO_INSIDE_HOURS:
        return 0.0
    return float(np.interp(hours, [h for h, _ in points], [move for _, move in points]))


def shifted(held, points, hours):
    if held is None:
        return None
    return max(0.0, held + drift_at(points, hours))


def current_points(conn):
    # Imported here: TrustPools imports T1GridReport, which imports this module through T1Estimator.
    from TrustPools import snapshot
    return snapshot(conn).drift
