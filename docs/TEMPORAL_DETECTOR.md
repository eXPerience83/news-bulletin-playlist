# Universal conservative temporal detector (#441)

The official catalogue ingestion uses one source-independent temporal detector. It no
longer selects runtime timestamp authority by `parser_id`. The persisted parser IDs
and context-free parser APIs remain compatible; no database migration is required.
All runtime local times use the source's `ZoneInfo`, including historical layouts.

## Evidence and fallback

1. A reviewed complete edition layout or a corroborated catalogue structure proves a
   title calendar date and clock. The date must belong to the official release period.
2. A reviewed edition-clock layout can use a canonical official day. A corroborated
   structure can also recover an unusable date field using that day, provided the
   clock is independently valid. A valid date contradicting the catalogue never takes
   this route.
3. Failure leaves `edition_at` unset and retains the episode with the existing
   catalogue precision / `FIRST_SEEN` behavior. Date-only titles never invent midnight.

Both exact routes require a unique physical instant through `unique_aware_datetime`.
DST gaps and folds are rejected. Explicit GMT cannot be interpreted as a nonzero
local offset. No fixed seasonal offsets, network requests, audio, public timestamp
semantics, or product filters are introduced. The future #439 evidence layer belongs
between a failed semantic decision and the existing fallback; it is not implemented.

`TITLE` still means proven semantic time, including a title clock plus an official
day. Parser outcomes remain `exact`, `normalized`, `provider_compat`,
`catalogue_context`, and `fallback`. Source Audit additionally reports a typed,
bounded `temporal_evidence` reason and `temporal_supporting_rows` in the UI/JSON/ZIP.
These additions do not change persisted timestamp provenance.

## Reviewed layouts and compatibility

SER, RNE, Onda Cero, CNN, COPE and RFI are reduced to declarative profiles, sharing
`TemporalProfile` / `ProfileTitleParser`. Their old independent datetime-building
methods and the obsolete recovery base class are removed. The layouts retain the
reviewed boundaries and syntax recovery, including the historical two-digit-year
policies. COPE's national label and RFI's explicit GMT layout remain compatibility
evidence, not new collection filters. Generic structural inference may prove other
products; timestamp parsing never removes them from the catalogue.

RMF delegates clock/day resolution to reusable `CatalogueClockProfile`; its reviewed
`Fakty` prefix still supplies edition semantics for sparse samples. The universal
runtime checks competing temporal candidates before using any reviewed clock prefix.
A numeric suffix after the reviewed clock prefix also fails closed: it may contain
a malformed clock/date that token discovery cannot recognize.
The context-free RMF adapter remains for registry/API compatibility and differential
validation, not runtime dispatch.

A reusable German bulletin phrase (`[Die] Nachrichten vom DATE, CLOCK Uhr`) supplies
reviewed evidence without source identity. Deutschlandfunk's example resolves to
`2026-09-28T19:00:00Z` in `Europe/Berlin` even with one catalogue row. Another source
with that phrase uses its own timezone. Unknown sources can reuse every profile;
there is no source-name branch in catalogue ingestion or the detector.

The `release_date_title` marker remains a compatibility configuration value. It no
longer disables proven semantic evidence. Sources without such evidence still fall
back automatically.

## Catalogue inference policy

Inference runs only over the already-fetched source catalogue (maximum 200 rows).
A larger caller-requested catalogue disables inference, leaving reviewed layouts
available. No state is persisted or shared between sources or refreshes.

Discovery finds bounded possible date/clock tokens, including invalid values. It
is not itself evidence of an edition. An unreviewed structure must satisfy all of:

- Exactly one date-shaped field and one clock-shaped field in a terminal edition
  block, with punctuation/whitespace rather than narrative words between the fields.
- Identical normalized surrounding text and token positions, date separator family
  and clock format. No number-bearing prefix, trailing narrative, or unsafe Unicode
  normalization is used to establish a structure.
- At least five distinct corroborated title/day pairs, at least two official days,
  and at least three distinct clocks.
- Corroborated rows cover at least 80% of the complete bounded sample, including
  titles later rejected by an independent collection filter in the denominator.
- Corroboration requires official day precision. Compatible month/year precision
  can subsequently validate a full title timestamp but cannot train the structure.
- Date orders compatible with all corroborating rows have a nonempty intersection.
  Each accepted row must still resolve to exactly one date within its official period.
- No row of that structure with a usable calendar may contradict its catalogue or
  fail clock/DST validation. Rows with unusable dates do not count as support.

The thresholds are deliberately conservative policy, not a probability estimate.
Five repetitions alone do not activate arbitrary clocks inside headlines. New
clock-only structures without corroborated date evidence are not inferred; they
need a reviewed edition-clock layout. This is a deliberate coverage limitation.

For example, `NOTICIAS - RNE 2809206 - 1730H` alone falls back. With five qualifying
compact-date/compact-clock rows of the same structure covering at least 80% of the
sample, it resolves to 17:30 on the official day. `2809206` is never accepted as a
calendar date. A valid but different title date, insufficient support, a changed
layout or a DST ambiguity still falls back. This mechanism also works with an
unrelated stable product label; it does not inspect `source_id` or RNE identity.

Inference intentionally loses authority if a later refresh no longer supplies its
required evidence. In that case the existing catalogue fallback applies; no learned
profile silently survives a contradictory or sparse refresh.

## Differential evidence

Base revision: `67498dde01468457f62fadff6931bf4925efdd42`.

`tests/fixtures/temporal/historical_titles.json` freezes 162 literal title cases from
existing parser/recovery/DST tests, with their original test-file provenance. These
include real historical examples and deliberately malformed/adversarial mutations.
They are not a newly downloaded production catalogue. Catalogue days for accepted
cases are synthesized from their historical local edition date; rejected cases use
a fixed day. Thus this corpus proves title compatibility, not real catalogue-date
agreement.

`tests/_temporal_corpus.py` generates 6,960 additional cases across six historical
families, four years, twelve months, day boundaries, invalid dates/clocks and DST,
including 1,200 combinations of historical compact/hour-only RNE field boundaries.
The baseline produced 4,780 accepted and 2,180 rejected results. Its SHA-256 and per-
parser counts were recorded by running the **unmodified base source** in a separate
`git archive` directory using CPython 3.14.7. The differential test verifies that
frozen fingerprint before comparing every universal result. This avoids using a
shared implementation as the only oracle.

All 7,122 comparisons preserve acceptance/rejection and exact UTC instants
(4,834 accepted, 2,288 rejected). Ten additional frozen before/after cases explicitly
classify intentional changes in `intentional_changes.json`. The
original 2,224-case script/data from #428 was not present in the repository or
available local artifacts; this larger deterministic matrix replaces that missing
artifact rather than claiming to reproduce an unavailable live sample.

Intentional runtime changes, separately tested:

- German bulletin timestamps and source-independent reviewed layouts become exact.
- Sufficiently corroborated new date/clock structures become exact.
- Damaged calendar fields can recover through the official day with explicit
  `catalogue_context` provenance, never by accepting the damaged date.
- Title/catalogue contradictions, absent timezone and competing clocks fail closed.
- Runtime uses the configured timezone rather than a parser's historical default.

No unexplained differential regression is accepted. New exact timestamps naturally
participate in existing retention/order; their existing rules are unchanged. The
latest-episode proof now bounds coarse catalogue periods in the source timezone.
Using UTC days could incorrectly declare a 22:00 New York edition newest while a
same-local-day fallback might still be later. The new interval includes both folds
at midnight transitions. A same-day fallback blocks an unproven latest-episode claim.
No episode is removed merely because this detector fails.

## Adversarial review findings

After the initial full suite passed, a second review reproduced and fixed:

- P2: a reviewed clock prefix could hide malformed additional temporal fields such
  as `21:00 Fakty 18:00:30`, `100:00`, `18:999`, a partial date or a nine-digit date.
  All five now fall back; valid nonnumeric product suffixes remain supported.
- P2: exact timestamps crossing the UTC date boundary could incorrectly outrank an
  unproven same-local-day episode. Coarse intervals now use the source timezone;
  focused New York/Berlin catalogue tests prove the fallback blocks that claim.
  Extreme calendar years widen the interval safely if UTC conversion overflows.

Other focused cases cover catalogue contradictions, changing date order, ambiguous
coarse dates, numeric confusables, Unicode normalization, missing timezone, multiple
candidates, date-only and unreviewed clock-only titles, malformed brackets, DST in
Madrid/Berlin/Warsaw/New York, catalogue reordering, sparse/oversized samples and
retention of episodes whose timestamps remain unproven.

- P2 regression found before commit: an early token-discovery rejection skipped a
  reviewed RNE layout with an adjacent date separator and an unsuffixed hour, such
  as `NOTICIAS RNE -20.06.2026- 19`. Reviewed layouts now run even when discovery
  yields no tokens. The expanded 1,200-case boundary matrix preserves the original
  accepted/rejected behavior and checks this fix against the frozen base oracle.

- P2 hardening: Python's ISO parser also accepts week-date strings such as
  `2026-W40-1`. Exact evidence now requires canonical `YYYY-MM-DD`, `YYYY-MM`, or
  `YYYY` catalogue text; week dates and signed/space-padded numeric components fail
  soft. This preserves the old RMF canonical-day guard in the universal route.

Final local validation uses CPython 3.14.7, Ruff 0.16.5, mypy 2.3.1 and pytest 9.1.1.
The full suite passes 1,455 tests. Hosted CI is separate evidence: recent runs did
not start, reporting an account billing/spending-limit block. Local validation is
not presented as a hosted CI or container result.
