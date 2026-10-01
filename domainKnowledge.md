# Domain Knowledge

Facts about the real world of non-rev standby travel — mostly on Delta —
that this project's code has to work around. These aren't decisions this
project made; they're true independent of any code, and wouldn't stop
being true if the code were rewritten from scratch. Written as current,
settled understanding — no history of how it was learned.

## Flight identity

Flight numbers are not stable, and that's a fact about how Delta
operates, not a data-quality complaint. Rotating a number week to week
and reusing one number for both directions of a same-day round-trip
pairing are two known examples of this, not an exhaustive list — Delta
does other things with numbering that would break identity-matching just
as badly if this project ever leaned on it. A flight's real identity
here is origin + destination + scheduled local departure time — corrected
in place through the day as needed, never matched by flight number.

Flight numbers still have real, narrow uses outside this project's own
logic: looking a flight up externally (e.g. "is Delta 1234 running
tonight?"), and tracing a specific fat-fingered entry back to what it was
probably supposed to be. Useful for talking to the outside world; never
useful as a key or join inside this project. In the schema, the column
holding it is named `carriersFltNum_notStable_DO_NOT_USE` for exactly
this reason — the name itself is the enforcement, since a comment here
would go stale.

## Service vs. route vs. flight number

A *route* is just org+dest. A *service* is a specific recurring departure
at roughly a given time of day — a route can carry several distinct
services. Delta's flight number is neither, and is disregarded entirely.

## Schedule volatility

Delta's published schedule is a live, wobbling thing, not a durable
record. It's reliably accurate for the current day, but a service's
departure time can drift from one week to the next — a few minutes,
occasionally more — and there's no way to reconstruct what the schedule
actually said on some past date; it isn't recorded anywhere once it's
gone. This is part of why a service is identified by clustered
departure time rather than by looking up "the" scheduled time for a
route: there is no single fixed value to look up. A logged
observation's own recorded departure time is the only trustworthy
record of what the schedule said at the moment it was taken.

## The seat-count ceiling

Delta's site (and every airline's) caps displayed seat counts at 9 — the
displayed value means "9 or more," not literally 9. The real count could
be 9, 20, or 30. Never treat a displayed 9 as an exact value, and never
assume an upper bound above it.

## Seat map vs. can-buy

The seat map's selectable seats and the can-buy count measure different
things, and neither bounds the other. A seat can show as selectable yet
already be sold, so a map with 12 selectable seats may allow buying only
one or two. The likely cause, unconfirmed, is passengers (mostly basic
economy) who hold a ticket but get no seat until check-in. The reverse
happens too: can-buy can exceed the number of selectable seats. Both
directions occur at interior can-buy readings (1-8), not just at the 9
ceiling. The seat map is loose, independent evidence, never a truer
count than can-buy.

## Whose clock

The person logging and the flight departing are usually in different
time zones. A flight's departure, and what day it counts as, has to be
evaluated against its own origin airport's local clock, not wherever the
observer happens to be. Anything that buckets or filters by date has to
ask "today, according to which airport" explicitly.

## Seating standbys together

Conjecture, not confirmed: when gate agents seat standbys, they try to
keep couples together if they have the time and room to, and their room
to do it grows with the total open seats across all cabins. So the summed
open-seat count bears on sitting together as well as on clearing at all.
The seat map's selectable solo and pair counts are not evidence of the
chance of sitting together; what the seat map shows isn't understood well
enough to read that way (see Seat map vs. can-buy).

In roughly the last hour before departure, gate agents clear upgrades and
reshuffle seat assignments, and standbys are seated during or after that
reshuffle. Where a standby ends up, which cabin and whether together, is
decided then, not predicted from earlier readings. Openness across all
cabins is the only earlier signal that bears on it.
