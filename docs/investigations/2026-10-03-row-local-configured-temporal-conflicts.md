# Row-local configured temporal evidence across sibling conflicts (#473)

## Production trigger

The 2026-10-03 production diagnostics showed RNE retaining `temporal_strategy=full_datetime`
while a pair of catalogue contradictions kept its shared temporal signature conflicted. As older
rows aged out, the refreshed catalogue degraded from several TITLE rows to a snapshot with zero
TITLE rows and almost everything on PUBLIC/FIRST_SEEN chronology. The user-visible playlist then
placed older RNE editions above a later exact SER bulletin.

This is separate from #459/#460. FIRST_SEEN ordering already uses one coherent physical timeline.
The remaining problem was that valid title chronology was being discarded before ordering.

## Previous behavior

`TemporalDetector` groups identical full date/clock layouts into a structural signature. A
parseable row whose calendar disagrees with its official Spotify release period marks that
signature conflicted. The detector correctly refuses to train AUTO from that structure.

The same conflict marker was also checked before configured capabilities. That meant one bad row
could force every sibling row with the same signature to `catalogue_date_conflict`, even when a
sibling had a complete calendar and clock that independently resolved in the source timezone and
matched its own official release period.

For RNE, an observed `01.10.2026 - 17.00H` row on official catalogue day `2026-10-02` therefore
poisoned otherwise valid `02.10`/`03.10` full-date/full-clock rows. Those rows then lost TITLE
priority and fell through to PUBLIC/FIRST_SEEN.

## Corrected contract

Signature conflict and row contradiction are distinct scopes:

- A contradiction anywhere in a signature still blocks AUTO learning for the whole structure.
- The contradictory row itself still fails closed and cannot be repaired by a configured
  capability.
- A different row may use an explicitly configured **complete-calendar** capability only when its
  own full date and clock resolve uniquely, survive source-IANA/DST validation, and agree with that
  row's own official catalogue period.
- Clock-only, partial-date recovery, damaged-date recovery and other routes that need catalogue-day
  reconstruction do not gain a new bypass from this change.

This preserves the existing principle that a demonstrated contradiction cannot be overridden: the
contradictory evidence on the row remains rejected. The change only prevents a sibling's bad
calendar from erasing independently validated row-local evidence under an operator-reviewed
reusable capability.

## Non-goals

This change does not preserve historical TITLE through persistence, change RNE `wall_time`, alter
TITLE -> PUBLIC -> FIRST_SEEN precedence, change FIRST_SEEN ordering/retention, add provider IDs or
requests, or relax destination/reconciliation safety.

The focused regression keeps AUTO fail-closed, restores a compatible configured `full_datetime`
row to TITLE, and proves the incompatible sibling remains `catalogue_date_conflict`.

## Independent audit correction: coarse order witnesses

The first fix incorrectly reused the day-only AUTO training rows to retain orders
for configured conflict recovery. A uniquely valid `28/09/2026` calendar with
official month `2026-09` or year `2026` therefore still lost TITLE when a contradictory
sibling appeared and no valid day-precision sibling remained.

The corrected implementation separates order witnesses from AUTO-valid rows.
Witnesses must resolve uniquely against their own day/month/year period through
`_resolve(..., recover_date=False)`. Their compatible orders must have a nonempty
intersection. `09/10/2026` against year `2026` remains ambiguous and supplies no
witness. Resolved witnesses requiring incompatible DMY/MDY orders cannot open the
configured path. AUTO training, coverage, minimum rows, clock diversity and
supporting-row counts continue to use day precision exclusively.

Permanent regressions cover month/year with `full_datetime`, complete-calendar
`partial_date_catalogue_day` and `explicit_utc`, exact UTC preservation, contradictory
row rejection, corpus reversal, ambiguous calendars, incompatible witnesses and
unchanged AUTO eligibility. No partial/damaged calendar or missing day is recovered
by the order-witness path.
