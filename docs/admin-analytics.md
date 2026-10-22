# Admin analytics

`GET /v1/admin/analytics/overview` reports consultation counts by lifecycle
status, captured/refunded/net amounts grouped by currency, and availability
utilization for a caller-supplied UTC-aware date interval.
`GET /v1/admin/analytics/doctor-performance` reports bounded per-doctor
consultation counts and payment totals for the same interval.

Both endpoints require an admin role and an access token with verified MFA.
Date intervals use `[from_date, to_date)` semantics and are capped at 366 days.
Responses contain only grouped operational/financial aggregates—no patient IDs
or clinical content—and queries aggregate in SQL with bounded pagination on
doctor results.

Revenue values are based on the persisted payment state: captured amount sums
payments in `succeeded` or `refunded` state; refunded amount is reported
separately; net is captured less refunded. Totals are never combined across
currencies. Utilization is minutes reserved for scheduled, in-progress, or
completed consultations divided by minutes in non-deleted slots that are
available, held, or booked. Pending-payment holds count as offered capacity but
not utilized time; unavailable slots are excluded.

These are synchronous read aggregates to keep this initial modular monolith
operationally simple. At higher event volume, use a refreshed reporting
projection/materialized view maintained asynchronously; do not place long
analytics scans on the booking transaction path.
