# Universal conservative temporal detection (#441 / PR #443)

## Runtime aggregate diagnostics

On successful catalogue construction and journal replacement, source outcomes carry
the configured `temporal_strategy`, detector `temporal_supporting_rows` and
`temporal_conflicts`, plus final refresh snapshot provenance counts:
`timestamp_title_count`, `timestamp_public_count`, `timestamp_first_seen_count`.
Their sum equals the accepted episode count in that bounded snapshot, before playlist
filtering/retention and before journal reuse of previously validated public history.
An episode whose older public evidence is reused by the journal may therefore be
counted as FIRST_SEEN in this refresh snapshot while remaining PUBLIC in persisted
state. These are current-refresh evidence metrics, not a persisted inventory census.
No database read is added to obtain a different denominator.

The same block includes six public outcome counts: accepted, rejected_future,
catalogue_conflict, invalid, identity_mismatch and shadowed. Counts apply only to
collection-accepted rows evaluated in that refresh. Identity mismatch includes the
other catalogue episodes that do not match the single public observation, so a large
value alone does not indicate detector corruption. TITLE means proven semantic time;
PUBLIC means validated lower-priority publication time; FIRST_SEEN means that refresh
could not establish either exact path. `parser_fallback_count` remains the semantic
parser count and must not substitute for final provenance counts.

`source_cycle_completed`, `source_state_preserved` and
`source_detector_false_negative` include these fields only when that outcome has a
newly constructed and applied refresh snapshot. A failed refresh, unchanged source
or other `refreshed=false` cycle omits the entire block, rather than emitting invented
zeros or stale metrics. A refreshed but incompletely covered catalogue can include
its actual snapshot metrics even if the existing event is source_state_preserved.
All available counts include zeros consistently. Normal diagnostics ZIP exports
contain bounded aggregates; Source Audit retains detailed per-episode evidence.

Repeated `public_release_rejected_future_count` can support future wall-time research.
Public wall-time reinterpretation remains future work under #439; no public mode or
new selector is introduced here. No requests or SQLite reads are made solely for logs.

## Authority and configuration

Every ordinary new source defaults to `temporal_strategy = auto`. Adding one needs
the existing source identity/classification and an IANA timezone, not Python, a
broadcaster parser, a layout profile, regex, separators, or a date-order setting.

The runtime hierarchy is:

1. AUTO inference using only this source's already fetched official catalogue.
2. Its optional, explicitly configured reusable capability, if AUTO cannot prove
   the row. A demonstrated contradiction cannot be overridden.
3. Validated exact public Spotify release timestamp from the already observed current episode.
4. Existing catalogue precision / `FIRST_SEEN` fallback, keeping the episode.

`disabled` bypasses both semantic routes, but still permits validated public release
evidence. This PR integrates the conservative literal-instant path investigated in
#439; wall-time reinterpretation remains unimplemented and #439 remains open.
Collection filtering remains independent.

| Value | Optional capability after AUTO |
|---|---|
| `auto` | None; insufficient evidence is a normal fallback. |
| `full_datetime` | One complete calendar field and one editorial clock in a bounded edition block. Includes explicitly marked clocks and terminal bare hour/HHMM fields under this operator contract. |
| `clock_catalogue_day` | Leading editorial clock with a short nonnumeric label, or a terminal clock beside an unusable seven-digit compact date slot; requires official day precision. Cannot consume a valid full date. |
| `partial_date_catalogue_day` | Full or partial date plus clock. Partial D/M or M/D requires official day precision and must match that day; the year comes exclusively from that day. A complete date can also validate against month/year precision. |
| `explicit_utc` | Complete date and clock with explicit UTC/GMT; the source timezone must yield zero offset at that instant. |
| `disabled` | No semantic timestamps, even with a proven structure. |

An override is an operator assertion that the bounded field denotes an editorial
clock. It does not waive calendar, Unicode, competing-token, timezone or DST checks.
For example, the clock capability accepts `16:00 Invented edition` as well as
`16:00 Fakty`; AUTO accepts neither in isolation. A source that merely discusses
scheduled events must not be assigned this capability without reviewing that claim.

Unknown sources use AUTO. They may independently infer a structure equivalent to
one previously seen elsewhere, but do not inherit another source's reviewed product
contract. There is no global broadcaster-profile injection or German-phrase bypass.
`Uhr`, AM/PM and UTC/GMT are clock syntax, not broadcaster identities.

## Deterministic inference policy

Inference is recreated for each catalogue, capped at 200 rows. No learning is shared
between sources, persisted between refreshes, or fetched with additional requests.
A structure requires:

- Exactly one date-shaped field and one clock-shaped field. No narrative words
  between them. They form a terminal edition block, or a leading block followed by
  a pipe-delimited short label. Brackets must be balanced in the supported forms.
- Identical normalized literal surroundings, field ordering and separator/clock
  families. The clock format can be 12h/24h; dates can be DMY/MDY/YMD, compact or
  supported named months. A fixed single-digit label such as `5 cosas` is allowed;
  a multi-digit narrative number in the prefix is not.
- At least five distinct corroborated title/day pairs and three distinct clocks.
  Multiple days are not mandatory: DLF's ten same-day editions provide sufficient
  structural evidence. Five copies of one title or five identical clocks do not.
- At least 80% of the whole fetched sample corroborates that exact structure.
  Independent collection filtering does not shrink this denominator.
- Training uses canonical official day precision. Each title calendar/clock must
  resolve uniquely, agree with that day and survive DST validation.
- Compatible date orders must have a nonempty intersection across supporting rows.
  Contradictory calendars/clocks or inconsistent orders block that structure, also
  for configured overrides. A learned order cannot change to make a later row fit.

The result is a deterministic structural contract, not a probability score or a
linguistic proof that a title describes an edition. A source consistently formatted
like an edition but actually describing event schedules is a residual limitation
of title/catalogue evidence. AUTO does not infer isolated clock-only or partial-date
families; those use explicit capabilities. A changed/sparse/mixed catalogue loses
AUTO authority and falls back unless its configured capability independently fits.

No valid date may disagree with the official release period. Coarse month/year
precision can validate a complete title date after a structure is learned or under
the full-date capability; it cannot supply a missing day. No midnight is invented.
IANA `ZoneInfo` and the shared `unique_aware_datetime` reject gaps and repeated times.
There are no seasonal fixed offsets. Safe syntax normalization is reused; numeric
confusables, full-width digits and unsupported compatibility transformations fail.

### Damaged compact date

`NOTICIAS - RNE 2809206 - 1730H` is not a valid date. It falls back in AUTO without
support. Five valid compact-date/clock rows of that same source/layout, still meeting
80% coverage, establish the terminal clock slot and allow the official day to replace
the damaged field. The explicitly selected clock capability also supports that slot.
All six supplied damaged examples are tested; they resolve to 15:30, 14:30, 14:00,
13:30, 13:00 and 12:00 UTC on 2026-09-28 in Europe/Madrid. Provenance is
`catalogue_context`, never an accepted seven-digit calendar date.

## Built-ins and compatibility

AUTO always runs first (except disabled). The configured values below preserve
sparse historical catalogues without importing broadcaster grammars into the engine.

| Source | Configured strategy | Why |
|---|---|---|
| SER | `full_datetime` | AUTO learns a rich stable catalogue; capability preserves sparse editions/recovery syntax. |
| RNE | `full_datetime` | AUTO learns marked-clock layouts; capability also covers sparse compact and bare-hour history. Damaged-only catalogues need clock override or qualifying AUTO support. |
| Onda Cero | `full_datetime` | AUTO sufficient with support; sparse editions retain exact times. |
| CNN | `full_datetime` | AUTO learns MDY/12h; official calendar disambiguates supported full dates. |
| RFI | `partial_date_catalogue_day` | AUTO handles rich full-date families; capability also validates partial dates against the day. Explicit GMT remains checked. |
| RMF | `clock_catalogue_day` | Clock-only AUTO deliberately lacks calendar corroboration. |
| Deutschlandfunk | `auto` | Ten provided same-day titles independently prove the structure. 21:00 Berlin = 19:00Z. |
| ABC, DW, N+ Univision, Reuters, UN News (English and Spanish) | `auto` | No clock evidence means fallback; date-only titles remain fallback. |

COPE is an existing compatibility parser/test family, not a current built-in source.
Expansión is a supplied fallback example, also not a current built-in definition.
Its leading clock/date/label family is supported by AUTO and `full_datetime`.
No parser ID is selected by runtime temporal detection. Legacy adapters remain for
their existing public APIs, registry validation and independent differential tests.
They are not injected into the detector. Some old `provider_compat` inputs now use
generic `exact` parsing; UTC timestamps are unchanged and this provenance change is
explicitly tested. `normalized` and `catalogue_context` remain distinct.

Managed JSON schema v4 defaults the optional temporal field to AUTO when absent.
v1/v2/v3 documents migrate through the existing atomic save. SQLite format 3
transactionally widens both episode provenance checks; rollback to an older binary
requires pre-upgrade database and managed-state backups. Create/Edit use the shared
server-rendered selector; unknown values fail validation. Existing auth/CSRF checks
are unchanged. Built-ins show their capability in the source list. Temporal changes
participate in the guarded playlist-write contract.

Saved strategy changes apply when the source catalogue is next normally refreshed.
Editing does not rewrite accepted episode history or force Spotify requests. Existing
accepted catalogue state remains last-known-good until that refresh, including its
previous timestamp provenance. No detector/catalogue cadence is changed.

## Observability and ordering

Source Audit adds configured strategy, detected-structure flag, supporting rows,
coverage and conflicts to its JSON and UI. Per-episode typed evidence distinguishes
`catalogue_structure`, `configured_capability`, `disabled`, conflict and unproven
fallback. Parser outcome distinguishes full-title, normalized and catalogue-day
routes. JSON/ZIP exports retain their existing schema and keys with additive metadata;
persisted `TITLE` and `FIRST_SEEN` values do not change meaning. The additive
`public_release_outcome` reports no evidence, accepted, identity mismatch, invalid/
unsupported, catalogue conflict, rejected future or shadowed by TITLE.
`SPOTIFY_PUBLIC_RELEASE` (`spotify_public_release`) is a distinct exact provenance.

New exact timestamps use existing ordering/48-hour retention. A same-day fallback
blocks `_provable_latest_id` because its local-day interval can extend beyond the
latest known clock. Tests cover unordered catalogue rows, mixed exact/fallback,
cutoff crossing, fallback preservation and New York UTC-day boundaries. A parsing
failure never removes an episode from catalogue ingestion.

## Differential evidence

Original oracle revision: `67498dde01468457f62fadff6931bf4925efdd42`.
Correction starts at `13942a98021003e3fcc4e6dad6a73e9fa2ba4071`.
The frozen 162 literal historical/test titles plus 6,960 generated boundary cases
remain reproducible. Their dates are synthesized from historical local dates, not
a newly downloaded production catalogue. The original 2,224-title #428 artifact
was unavailable; this matrix does not claim to reproduce it.

The test verifies the original baseline fingerprint using unchanged legacy API
results before comparing configured generic capabilities. This table measures
sparse-capability compatibility; separate tests prove AUTO for each rich historical
family and the two invented families. “Identical” below counts identical exact UTC
instants, not unchanged fallback rows.

| Provider | Corpus | Old exact | New exact | Identical exact | Improvements | Regressions |
|---|---:|---:|---:|---:|---:|---:|
| SER | 991 | 691 | 691 | 691 | 0 | 0 |
| RNE | 2208 | 1716 | 1908 | 1716 | 192 | 0 |
| Onda Cero | 985 | 690 | 690 | 690 | 0 | 0 |
| CNN | 981 | 352 | 352 | 352 | 0 | 0 |
| COPE | 978 | 689 | 689 | 689 | 0 | 0 |
| RFI | 976 | 694 | 694 | 694 | 0 | 0 |
| RMF | 3 | 2 | 3 | 2 | 1 | 0 |
| Historical total | 7122 | 4834 | 5027 | 4834 | 193 | 0 |
| DLF supplied catalogue, AUTO | 10 | 0 | 10 | 0 | 10 | 0 |

The 192 RNE expansions are exhaustively checked pipe-separated compact-date/clock
or bare-hour combinations, with independent expected clocks 19:00, 19:30 or 09:30.
The one RMF expansion is `16:00 Faktycznie`: a configured generic clock capability
does not require the old product spelling. This is capability expansion, not evidence
that an arbitrary real podcast's clock is editorial. AUTO still rejects it alone.
Additional targeted tests cover the actual RMF clocks, six damaged RNE dates and RFI
partial dates. No new editorial inclusion/exclusion rule is introduced.

| Configured | Observed family | AUTO proven? | Override matched? | Final source |
|---|---|---|---|---|
| auto | Single SER/CNN/RNE/RMF/COPE title with 0/1/4 unrelated rows | No | None | FIRST_SEEN |
| auto | DLF 10 rows | Yes | None | TITLE |
| auto | New Spanish / US synthetic family, 5 rows | Yes | None | TITLE |
| clock_catalogue_day | Single SER full date/time | No | No | FIRST_SEEN |
| full_datetime | Single unrelated clock-only title | No | No | FIRST_SEEN |
| clock_catalogue_day | Reviewed leading clock + label | No | Yes | TITLE |
| clock_catalogue_day | Proven DLF catalogue | Yes | Not needed | TITLE |
| disabled | Proven DLF catalogue | Bypassed | Bypassed | FIRST_SEEN |

## Adversarial review

The correction removes the reproduced P1 global-contract contamination. A second
review also blocks overrides from bypassing a learned date order or a catalogue
contradiction and checks explicit GMT/UTC even with trailing punctuation. Matrix
tests caught adjacent RNE separators/bare-hour discovery gaps;
generic token boundaries now preserve every original accepted timestamp.

Focused tests cover multiple clocks/dates, arbitrary narrative clocks, anniversary
numbers, date-only titles, unsupported Unicode, full-width digits, safe normalization,
missing timezone, invalid canonical catalogue dates, day/month/year precision,
Madrid/Berlin/Warsaw/New York DST, duplicate samples, insufficient coverage, changed
formats and per-source isolation. Overrides are checked against the same adversarial
headlines. Statistical consistency cannot establish editorial intent for every
possible natural-language title; use Disabled when a source's apparent edition
structure actually encodes other events.

## Validated public fallback (#439 conservative integration)

The engine passes a typed `PublicReleaseEvidence` from its existing detector journal
into both normal and stale-escalation catalogue refreshes. Catalogue ingestion never
reads that journal itself and performs no new request. Absent/degraded observations
supply no new public evidence. A Source Audit reuses its existing optional public
observation; disabling that observation leaves catalogue-only behavior.

Only `object_iso_string` is enabled. A bounded ASCII ISO timestamp with seconds,
optional microseconds and literal Z/offset must be aware and normalize to UTC.
STRING remains disabled because its detector contract also includes date-only or
arbitrary strings. Source, show and episode identity must all match exactly; title
similarity is irrelevant. The instant's date in source ZoneInfo must lie within the
official `release_period`. Missing timezone, invalid ISO/period, unsupported shape,
overflow, identity mismatch and more than five minutes beyond observation fail
closed. Five minutes tolerates minor clock skew, never provider timezone mistakes.
No lower age limit is imposed. No wall_time, seasonal offsets or source-name fixes.

Semantic TITLE always wins. Public evidence applies to at most one unique catalogue
ID; remaining rows keep their own existing fallback. Public timestamps previously
persisted in history survive later refreshes with the same identity and unchanged
catalogue date/precision even when the public detector advances or degrades. A new
semantic timestamp supersedes the public value. A changed catalogue period does not
blindly inherit older public chronology. Accepted public values use the existing
exact ordering and exclusive 48-hour cutoff; a tolerated future instant is ineligible
until its encoded release time, under the unchanged future-item rule.

SQLite format 2 -> 3 rebuilds only episode state/history tables transactionally,
copying all columns and recreating explicit indexes from sqlite_master. Primary keys,
existing timestamps, source state and old provenance are retained. Format 1 first
uses the existing history migration, then upgrades to 3 in the same transaction.
Incompatible schemas or invalid rows roll back. New databases use format 3. There is
no public backfill or history conversion. Old binaries cannot read format 3: rollback
requires a pre-upgrade backup. Managed JSON v4 is an independent atomic migration.

Real recorded fixtures (no live provider access): Expansión live 05:21Z is accepted;
its old 2026-09-25 05:27Z bootstrap is accepted despite age and excluded at 48h.
Reuters 13:58Z and ABC 00:45Z are accepted. SER/RMF retain TITLE; Onda's title 23:00
Madrid remains 21:00Z despite public 21:02Z. RNE public 21:13Z observed at 19:31Z
fails closed when semantic evidence is absent, and never overrides semantic 19:00Z.

Validation is performed locally with CPython 3.14.7. Hosted Actions/container checks
are independent; previous jobs could not start because of billing/spending limits.

Local validation for the public integration: Ruff fix/check/format and mypy (73 source
files) pass; complete pytest: 1,650 tests; focused temporal/public/catalogue/migration/
audit/engine suites: 634 tests. `git diff --check` passes.
