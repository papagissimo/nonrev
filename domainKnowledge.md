# Domain Knowledge

Facts about the real world of non-rev standby travel — mostly on Delta —
that this project's code has to work around. These aren't decisions this
project made; they're true independent of any code, and wouldn't stop
being true if the code were rewritten from scratch. Written as current,
settled understanding — no history of how it was learned.

## Flight identity

Flight numbers are worthless as identity within this project. Delta
rotates flight numbers week to week, and even reuses the same number for
both directions of a round-trip pairing on the same day. A flight's real
identity here is origin + destination + scheduled local departure time —
corrected in place through the day as needed, never matched by flight
number.

Flight numbers still have real, narrow uses outside this project's own
logic: looking a flight up externally (e.g. "is Delta 1234 running
tonight?"), and tracing a specific fat-fingered entry back to what it was
probably supposed to be. Useful for talking to the outside world; never
useful as a key or join inside this project.

## Service vs. route vs. flight number

A *route* is just org+dest. A *service* is a specific recurring departure
at roughly a given time of day — a route can carry several distinct
services. Delta's flight number is neither, and is disregarded entirely.

## The seat-count ceiling

Delta's site (and every airline's) caps displayed seat counts at 9 — the
displayed value means "9 or more," not literally 9. The real count could
be 9, 20, or 30. Never treat a displayed 9 as an exact value, and never
assume an upper bound above it.

## Whose clock

The person logging and the flight departing are usually in different
time zones. A flight's departure, and what day it counts as, has to be
evaluated against its own origin airport's local clock, not wherever the
observer happens to be. Anything that buckets or filters by date has to
ask "today, according to which airport" explicitly.
