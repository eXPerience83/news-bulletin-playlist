# Changelog

All notable product and runtime changes are recorded here. The project remains pre-release; changes
after the latest release stay under **Unreleased** until the next stable release is cut.

## [Unreleased]

**Proposed next beta: 0.3.0b1 — package metadata staged, NOT published or
deployed.** The previous known production beta is `0.2.3b1`; this section
describes unreleased `main`, not the deployed TrueNAS image.

**BREAKING CHANGE: forward-only v11 configuration authority; NO SUPPORTED
DOWNGRADE.** Fresh installations start with **zero** installed sources, playlist
templates, local drafts and Spotify destinations; image-owned 13 source presets
and seven playlist examples are *read-only* until explicitly installed.
Existing v6–v10 installations migrate once, preserving their full historical
source/catalogue and four real destination identities; `managed-state-v11.json`
is then authoritative, and `managed-state.json` remains **inert evidence**.
Missing, corrupt or ambiguous v11 state must fail closed, never reimport
legacy state as a fallback. No support for booting an old image after migration.

A complete independent /data backup would reduce the risk of irreversible loss.
For the owner's planned 0.2.3b1 → 0.3.0b1 cutover, however, **the backup and
copy-based rehearsal have explicitly been declined**: local configuration,
identities, OAuth, journals or artwork could require manual reconstruction if
migration fails. Portable JSON and ZIP are not backup substitutes. This
changelog and version bump do not authorize Spotify access, GHCR publication,
release tags, edge/stable/latest movement or TrueNAS deployment.

A successful candidate CI/Container check **does not prove the live RNE
incident is resolved**. The null-bearing source permits only add-only
reconciliation with positively observed eligible episodes. If the destination
is at its configured cap, writes pause without evictions. Production acceptance
and actual deployment are separately tracked in #562 and #565.

Optional preset implementation (#552) and **synthetic-only** v11 acceptance
(#553) are complete in code (PRs #585–#587, #589). The initial attribution and
selected-cover fix (#528) is also merged in code (PR #590). This does **not**
prove the behavior on the owner's real Spotify account or on the TrueNAS
installation; #527/#528, #562 and beta gate #565 retain their independent
live/operational acceptance requirements. No prerelease or stable promotion
has occurred.

### Changed

- **v11 operational authority:** atomically migrate legacy v6–v10 into the
  installation-owned v11 document; retain frozen historical sources/templates,
  all four Spotify destinations, dormant assignments, overrides, portable UUIDs,
  quarantine, exact temporal policies, cover references and operational journals.
  The durable v11 authority marker prevents fallback to the legacy file after
  migration; CAS rejects stale overwrites and partial/corrupt state fails closed.
  Migrated installations do not silently adopt presets on future image changes.
- **Forward-only installation model:** new roots are empty and opt-in; Admin,
  runtime and portable import/export use the same v11 owner configuration.
  Bundled examples are not installed or made into Spotify playlists at startup.

- Prepared MIT software licensing under `Copyright (c) 2026 Roberto Ribes`,
  updated package/image license metadata and the public/admin copyright notices.
- Clarified the pre-release support boundaries, ownership of built-in versus
  installation-owned playlist drafts, and uncertainty around undocumented Spotify
  Embed change detection. No additional Spotify API entitlement is implied.

- Preserve known episode identities when Spotify show catalogues contain literal `null`
  slots; the unknown slot still makes full catalogue coverage unproven. Eligible,
  positively observed new episodes may now be inserted by a strictly **add-only** path
  without deleting, reordering or replacing existing playlist entries (#560/#562,
  PRs #561/#563). Incomplete sources never authorize full reconciliation.
- An incomplete destination pauses additions rather than evicting when the configured
  item cap would be exceeded. A durable local pending-add intent protects against
  duplicate retries after uncertain POST/transport outcomes or restarts; normal
  exact reconciliation resumes after complete source coverage.
- Commit source episodes, accepted history and refresh trust atomically, rolling back the whole
  refresh on failure instead of certifying partial new rows with old coverage.

### Added

- Admin shows 13 read-only packaged source presets and seven playlist examples,
  distinct from installed sources, local editable drafts and managed Spotify
  destinations. Explicit JSON import installs sources by canonical Spotify show
  identity; example selection creates only a durable local draft without OAuth,
  provider request or scheduler work. Missing/disabled sources require
  operator action. Example covers become persistent content-addressed owner
  JPEGs; exact durations/exceptions and metadata survive restart (#552, #587).
- Synthetic end-to-end acceptance for v6–v10 forward upgrades with four
  historical destination IDs and real isolated SQLite/WAL, and fresh v11
  JSON source import → local draft → ZIP round-trip across separate roots,
  idempotent restart, malformed ZIP and stale CAS rejection (#553, #589).

- Installation-owned custom playlist templates and drafts through Admin, with
  deliberate Spotify activation, bounded editing, safe deletion/reference checks,
  optional uploaded artwork and an installation-neutral default cover (#259, #527).
  Local editing and importing do not automatically create a Spotify destination.
- **Portable source JSON v1** export/download and authenticated import with read-only
  preview, stable show-ID identity matching, deduplication, explicit conflict handling,
  capability checks, one-use CSRF and atomic installation-owned apply (#546–#549,
  PRs #554–#557).
- **Portable playlist ZIP v1** deterministic export with source closure, manifest,
  SHA-256 integrity records and self-contained cover bytes. Authenticated ZIP import
  validates contents and imports editable local drafts, without Spotify activation
  or destination credentials (#550/#551, PRs #558/#559). Portable exports are not
  backups of database, OAuth or Spotify state.
- Status pages and sanitized diagnostics identify specific blocked sources, explain
  coverage-unproven `null` slots and distinguish skipped desired-state evaluation
  from an actual empty playlist. Add-only outcomes report inserted item counts and
  preserved/paused conditions (#560/#562).
- Closed `item_value_kind` diagnostics for rejected non-object catalogue items, without retaining
  their contents or changing Source Audit acceptance, pagination or request counts.

- Configurable inclusive minimum/maximum duration per playlist, with the provisional default
  unchanged at 0..30 minutes. Admin uses whole minutes and preserves historical exact seconds
  when the displayed value is unchanged; changing minutes saves an exact whole-minute bound.
- Below-minimum exclusion diagnostics and minimum-aware configuration write safety.

### Fixed

- **First Spotify playlist activation:** include the bounded project
  attribution in the *initial* create request for any new template, not only
  custom drafts. After the new destination is durably recorded, attempt the
  selected cover once; report failures separately and leave explicit metadata/
  cover sync available without recreating the destination. Existing managed
  destinations are never rewritten by this fix (#528, #590).
- **Admin / Sources — Export all audits** no longer runs provider requests synchronously on
  the HTTP request thread. A single bounded, read-only background job reports per-source
  progress, supports cooperative cancellation and offers a sanitized ZIP after completion;
  Admin and `/healthz` remain responsive while it runs. Hardened job-id validation,
  durable progress diagnostics and the post-ZIP deadline check (#541, PR #543).

### Compatibility / operations

- **Operational managed-state is v11**, migrated once from the previous
  v6–v10 layouts; older schema versions remain historical *input formats*,
  not simultaneous active authorities. SQLite remains database format **3**
  with preserved WAL and history; add-only recovery uses an isolated, lazily
  created SQLite intent table for uncertain Spotify POSTs.
- Declared Python package support remains **3.14 and 3.15**; GitHub CI
  validates **3.15** while Ruff/mypy enforce the declared 3.14 lower bound.
  The unreleased Dockerfile currently references pinned Python **3.15.2**;
  the separately published production image remains subject to the existing
  3.14 compatibility contract pending #494's independent validation.
  No image promotion accompanies this candidate preparation.
- Source and playlist import/export actions are local-only and authenticated.
  They do not include or migrate live Spotify destinations, secrets, history,
  pending provider journals or production `/data`. Legacy source/template
  preset separation and clean-install synthetic acceptance were completed
  by #552/#553. They do not authorize using production /data.
- Source Audit exports are session-authenticated, limited to 20 sources, 600 seconds and
  16 MiB per ZIP, and kept in memory for at most 30 minutes after completion (or until
  another job replaces them). No playlist/Spotify destination writes, schema changes or
  persistence migration. The deployed #541 fix was validated on 2026-10-27 with 17/17
  source audits exported successfully in approximately 32 seconds.
- Managed-state v6 atomically upgrades v1-v5 with minimum 0 and preserves exact maxima.
  Provisioning journal v2 snapshots both bounds and still recovers pending v1 journals with minimum 0.
  SQLite remains database format 3 with unchanged tables/columns.
- Range Save remains local and wakes normal item reconciliation without direct OAuth,
  metadata or cover operations. #132 still owns final limits and any short/long split.

## [0.2.2] - 2026-10-02

### Changed

- Updated both Docker builder and runtime stages from the pinned
  `python:3.14.7-slim-trixie` image to `python:3.14.8-slim-trixie` with the new pinned digest.
  The supported interpreter contract remains CPython **3.14.x**.

### Compatibility / operations

- No persistence migration is required from 0.2.1: managed-state remains schema **v5** and SQLite
  remains database format **3**.
- This release candidate changes container patch level only; engine/provider/scheduler/source/catalog
  and playlist behavior are unchanged.

## [0.2.1] - 2026-10-02

### Changed

- Disabled source assignments now remain durable as dormant assignment intent instead of freezing the
  whole playlist. Effective membership includes only assigned, enabled sources; re-enabling restores
  surviving assignments, while explicit unassignment remains permanent. Runtime loading also pauses
  previously valid active playlists whose known assignments are all disabled, preserving source IDs
  and Spotify destinations without provider I/O.
- Explicit Spotify metadata synchronization now renders one bounded inline attribution:
  `<base> · by eXPerience83 · https://github.com/eXPerience83` (or the standalone product attribution
  for an empty base). The editable base reserves the 52-character suffix so the final description
  remains at or below 300 characters.

### Fixed

- Admin-triggered Enable/Disable/Save wakes no longer lose the immediate engine cycle to a stale WAL
  diagnostics snapshot. Diagnostics initialization reserves its writer transaction before schema
  checks, preventing the reproduced `SQLITE_BUSY_SNAPSHOT` race without adding retries, sleeps or
  provider I/O to the Admin mutation path.
- Spotify metadata HTTP 400/API/transport failures are no longer converted into success by retrying
  with the attribution removed. Metadata uses one PUT and surfaces failure to the operator; cover
  upload remains an independent operation and is still attempted after a metadata failure.
- FIRST_SEEN/coarse fallback ordering now stays on one physical timeline. Exact TITLE/PUBLIC evidence
  continues to use UTC `edition_at`; fallback rows order by the earlier of real `first_seen_at` and
  the end of the source-local Spotify release period. This fixes source-local-midnight misordering
  without creating `edition_at`, upgrading provenance or changing retention semantics.

### Compatibility / operations

- No persistence migration is required from 0.2.0: managed-state remains schema **v5** and SQLite
  remains database format **3**.
- The historical diagnostic label `applied_without_attribution` remains readable/exportable for rows
  written by earlier versions, while the current runtime no longer emits that success outcome.
- The supported interpreter remains CPython **3.14.x**; the pinned Python 3.14.7 slim image digest was
  refreshed without changing the runtime contract.

## [0.2.0] - 2026-09-30

### Added

- Added a responsive public status surface and a unified first-party Admin shell for Playlists,
  Sources and Diagnostics, with server-side login sessions, session-scoped CSRF protection and
  explicit logout.
- Added durable installation-owned custom Spotify-first sources. Admin can inspect a Spotify show,
  validate and create a source, edit safe metadata, enable/disable it, assign/unassign it from
  playlists and delete it when no playlist still references it.
- Added reusable per-source inspection reports plus bounded authenticated batch Source Audit ZIP
  exports, keeping detailed evidence out of ordinary diagnostics while preserving sanitized
  operational review data.
- Added separate accepted-episode research history and duration-profile diagnostics so production
  evidence can inform the eventual hard-duration policy without changing playlist behavior.
- Added universal source-local temporal detection with conservative catalogue corroboration and safe
  fallback, plus independently configurable Spotify public-release interpretation
  (`instant`, `wall_time`, `disabled`).
- Added explicit managed-action success notices so activation, settings saves, Spotify metadata/cover
  sync, stop and provisioning recovery actions no longer return silently on success.
- Added a README index for the confirmed public production playlists: **Noticias España**,
  **International News · EN** and **Wiadomości międzynarodowe · PL**.

### Changed

- Managed-state JSON now uses schema **v5**. It stores custom source configuration, temporal strategy,
  public-release mode and bounded built-in release-mode overrides while keeping built-in definitions
  image-owned; v1/v2/v3/v4 upgrade atomically.
- SQLite now uses database format **3**. Format 2 introduced separate accepted-episode history;
  format 3 extends current/history timestamp provenance to validated Spotify public-release evidence
  through reviewed forward migrations from the 0.1.0 baseline.
- Source origin models countries and institutional providers independently from editorial scope and
  language; United Nations sources no longer require an invented country.
- Spotify playlist attribution now points to the public author profile instead of the private
  repository: `News Bulletin Playlist · by eXPerience83 · https://github.com/eXPerience83`.
  Historical private-repository footers remain recognized only for idempotent cleanup.
- Episode state is reloaded only when reconciliation/stale-item evidence requires it, avoiding
  redundant Spotify-first persistence reads while preserving retry safety.
- CI path filtering avoids spending the Python PR job on unrelated changes while retaining all
  tested Python/contract inputs as triggers.

### Fixed

- Source inspection tolerates Spotify's current show-metadata response while retaining bounded
  validation and fail-closed identity checks.
- Runtime startup waits for HTTP/runtime readiness before starting the scheduler, avoiding startup
  races observed around production deployment.
- Sanitized diagnostics preserve meaningful engine failure classification instead of collapsing safe
  failures into less useful generic state.
- Shared temporal parsing rejects unprovable DST wall times, including gaps and ambiguous folds,
  rather than manufacturing an instant.
- Catalogue reload still occurs after failed reconciliation or stale escalation where fresh evidence
  is required, despite the new conditional-reload optimization.

### Compatibility / operations

- Version 0.1.0 remains the first supported persistence compatibility baseline. Upgrades from its
  database format 1 and managed-state generations continue through explicit forward migrations;
  pre-0.1 private-development databases remain outside the supported migration contract.
- The supported interpreter remains CPython **3.14.x** and the runtime remains Spotify-first with no
  audio download/storage.
- Public playlist lifecycle changes must update the README playlist index in the same change; private,
  test and merely built-in-but-unpublished destinations are intentionally excluded.

## [0.1.3] - 2026-09-22

### Changed

- Removed the unused reconciliation UTC helper and the obsolete standalone engine-scheduler fallback,
  leaving the production lifecycle controller as the single scheduler owner.
- Centralized operator-facing Spotify authorization labels and canonical raw 22-character base62 ID
  validation while preserving the existing caller-specific error messages and keeping full Spotify
  URI validation separate.
- Centralized durable parent-directory `fsync` handling for managed state, provisioning state and
  Spotify authorization persistence, preserving `O_DIRECTORY` where supported and each caller's
  existing error translation.
- Replaced diagnostics runtime's temporary mutation of
  `engine_runtime.OperationalHealthHandler` with explicit handler injection. The base operational
  handler remains the default internal fallback and its `/` renderer was retained after a separate
  reachability review.

### Compatibility / validation

- Database format remains **1** and managed-state formats are unchanged; 0.1.3 requires no
  persistence migration from 0.1.2.
- The complete post-cleanup `main` range was re-audited after #343-#349 with no functional
  regression found across scheduler lifecycle, OAuth/admin presentation, Spotify identifier
  validation, durable-file synchronization or HTTP handler composition.
- Every code-changing cleanup PR in the series passed both CI and Container validation on its final
  head; the final runtime composition head passed Ruff, formatting, mypy and **742 tests** plus the
  hardened container health/web/restart smoke test.
- #349 was closed without code deletion after confirming the base operational status renderer is an
  intentionally supported internal fallback rather than unreachable dead code.

## [0.1.2] - 2026-09-22

### Changed

- Completed the post-0.1.1 dead-code and unreachable-branch cleanup across persistence,
  diagnostics, Spotify-first desired-state construction, rate-limit guarding, catalog helpers and
  reconciliation results, removing unused compatibility surfaces without changing supported runtime
  behavior.
- Simplified Spotify-first catalogue replacement by removing the unused added/updated/unchanged
  classification pass and its pre-read. Episode UPSERT semantics, durable first/last-seen provenance,
  duplicate/source validation and deletion only with proven catalogue coverage remain unchanged.
- Reduced diagnostic maintenance surface by removing unused explicit count/prune APIs and
  never-emitted detail keys. Automatic age/capacity retention and the live sanitized diagnostic
  contract remain in place.
- Slimmed the outer playlist reconciliation result to evidence actually consumed by the engine while
  preserving internal degraded-verification handling, exact/degraded destination evidence, pending
  snapshot state, unavailable-media evidence and mutation outcomes.
- Container CI no longer runs for docs-only changes or a nonexistent `LICENSE` path, and the obsolete
  PR #87 edge bootstrap fallback was removed. The required `edge-amd64` deployment channel remains
  fail-closed and anonymously pullable.

### Compatibility / validation

- Database format remains **1** and managed-state compatibility is unchanged; 0.1.2 requires no
  persistence migration from 0.1.1.
- Final `main` audit completed after the cleanup series with Ruff and mypy green and **735 tests
  passed**.
- The exact audited `main` revision was built and smoke-tested by the edge publication workflow
  before promotion to `edge` / `edge-amd64`.

## [0.1.1] - 2026-09-21

### Changed

- The current repository state is now **All rights reserved** rather than MIT-licensed. The
  `LICENSE` file plus package/container MIT metadata were removed; this does not revoke rights
  already granted for historical copies that were distributed under MIT.
- Source status and sanitized diagnostics now distinguish the latest successful public-detector
  observation from the latest successful authoritative Spotify catalogue refresh, replacing the
  ambiguous source-level `last_success_at` field without changing Spotify request cadence.
- Current documentation was aligned with the 0.1.0/database-format-1 persistence baseline,
  Spotify-first architecture boundaries, validated TrueNAS cutover behavior and current licensing.

### Fixed

- Unchanged duration profiles and noteworthy duration decisions are no longer emitted again on
  every quiet scheduler cycle. Deduplication is in-memory only, keeps episode identity out of
  persisted diagnostics, and still emits fresh evidence when the underlying episode decision changes.

## [0.1.0] - 2026-09-21

### Added

- Managed per-playlist **maximum episode duration** in `/admin/`, persisted in
  `/data/managed-state.json` and editable without reconnecting Spotify or rewriting playlist
  metadata.
- Initial multi-playlist catalog using the bundled covers:
  - `Noticias en Español`;
  - `INT · ES`;
  - `INT · EN`;
  - `INT · FR`;
  - `INT · DE`;
  - `INT · PL`.
- Runtime source support for **ABC — Las Noticias de ABC**, **UN News Today**,
  **RFI — Journal Monde**, **Deutschlandfunk — Die Nachrichten** and **RMF FM — Fakty**.
- Generic deterministic `release_date_title` parser strategy for Spotify catalogue products whose
  titles do not expose a trustworthy semantic edition timestamp: `edition_at` remains unset and
  ordering/retention use explicit Spotify release-date precision plus local first-seen provenance.
- Source editorial-scope metadata and three-axis source classification:
  **`ORIGIN · SCOPE · LANGUAGE`**, separating provider country from actual news coverage and language.
- Admin source labels/table now expose country, scope and language. For example, CNN 5 Cosas is
  represented as `US · INT · es-ES`, so its US origin is not confused with US-only editorial coverage.
- Plain-text project attribution appended to Spotify playlist descriptions on metadata creation/sync:
  `Proyecto / Project: https://github.com/eXPerience83/news-bulletin-playlist`.
- Sanitized per-source/per-playlist duration histograms in diagnostics, plus explicit events for
  accepted editions >=20 minutes and duration exclusions, to support the evidence review in #132.
- Source-selection documentation that rejects predominantly long-form products even when individual
  episodes fit below a playlist hard limit.
- Runtime support for Reuters World News, ONU en minutos, N+ Univision 24-7 and DW Actualidad en
  análisis. ONU's broad Spotify show catalogue is filtered fail-closed to dated ONU-en-minutos
  editions. CBC World Report remains verified research but is withheld pending re-validation against
  the current Spotify-first catalogue/parser/duration promotion contract.
- Durable runtime-wide Spotify 429 backoff state with sanitized `Retry-After` diagnostics. Public
  change detection and local computation may continue, while authenticated Spotify work is
  suppressed until the persisted retry deadline expires, including across process/container restarts.

### Changed

- Persistence now starts from a clean Spotify-first database format 1 in
  `/data/news-bulletin-playlist-v1.sqlite3`; the private-development v1-v8 migration chain,
  RSS/matcher/planner schema compatibility and legacy diagnostic-row allowlist have been retired.
  The previous SQLite file is left untouched during the controlled 0.1.0 cutover for rollback.
- Runtime configuration is now exclusively the built-in catalog plus versioned
  `/data/managed-state.json`; the pre-1.0 full-YAML runtime path and `NEWS_PLAYLIST_CONFIG`
  selector have been retired with fail-closed detection of obsolete settings/files.
- Current general/default episode ceiling increased from **8 minutes / 480 seconds** to
  **30 minutes / 1800 seconds**. It remains configurable per managed playlist. The long-term default
  will be selected from production duration evidence under #132.
- `Noticias España` product naming becomes **Noticias en Español**, matching its existing cover and
  reflecting that the playlist combines Spain sources with selected genuinely global Spanish news.
- `Noticias en Español` defaults to SER, RNE, Onda Cero and ABC.
- `INT · ES` starts with **CNN 5 Cosas** as a genuinely global Spanish source. Region-specific
  products such as RFI Noticias de América remain research candidates rather than global defaults.
- `INT · EN` uses concise **UN News Today** and **Reuters World News** instead of the predominantly
  long-form BBC Global News Podcast candidate.
- RFI Journal Monde, Deutschlandfunk and RMF Fakty seed the first FR/DE/PL playlist experiments while
  source classification remains honest about `INT` versus `MIX` editorial scope.
- Spain-origin sources carry `es-ES` language metadata where the Spain variant is known; CNN is also
  explicitly `es-ES` by listening confirmation while remaining `US · INT` in origin/scope.

### Fixed

- Explicit Spotify metadata synchronization now records whether the playlist description was applied
  with the project attribution or through the existing exact-HTTP-400 fallback without attribution,
  while keeping rejected provider text out of diagnostics.
- Avoided the old 8-minute default silently dropping useful ordinary editions such as the observed
  **RNE 10:05 / 605-second** bulletin from production diagnostics on build `71788836ea1e`.
- Spotify description attribution remains outside editable managed state, preventing duplicate project
  URLs across repeated metadata updates while staying within the playlist description length limit.
- Spotify HTTP 429 no longer causes fixed ten-minute Web API retry loops. A valid provider
  `Retry-After` is honored (or a conservative 30-minute fallback is used), a later existing deadline
  is never shortened, and the first 429 suppresses every subsequent Spotify request in that engine
  cycle without weakening destination verification or reconnecting Spotify.

### Validation / follow-up

- #129 tracks configurable duration policy implementation.
- #130 is the reviewed multi-playlist/dev-candidate PR.
- #132 tracks production log review and the eventual default hard-duration decision.
- #140 tracks the durable Spotify 429 / `Retry-After` recovery behavior.
- #53 remains the living source research catalog; #44 remains the playlist rollout matrix.
