# Doctor directory search

`GET /v1/doctors` exposes only active, listed doctors and supports bounded
pagination (`limit` up to 100), case-insensitive specialty and location matching,
language matching, minimum rating, maximum fee, availability filtering, and
stable sorting by rating, fee, or next availability. `available_only=true`
requires an upcoming available slot; `available_from` and `available_to` narrow
that slot-start window and must contain timezone offsets.

The query uses SQL filters and correlated `EXISTS` checks rather than loading
candidate rows into application memory. Directory visibility includes both the
doctor's listing flag and active, non-deleted account/profile checks, excluding
soft-deleted doctors. A partial index supports upcoming available slot lookup;
the existing specialty/listing/rating and PostgreSQL JSONB language indexes
support the main directory predicates.

No Redis cache is added: listing and availability change immediately on
booking, cancellation, and schedule updates, and introducing invalidation
semantics before measuring query load risks stale booking information. Start
with indexed PostgreSQL queries and bounded responses; cache only if production
metrics show a read bottleneck, and keep availability authoritative in the
database regardless.
