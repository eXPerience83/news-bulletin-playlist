# Changelog

All notable product and runtime changes are recorded here. The project remains pre-release; changes
after the latest release stay under **Unreleased** until the next stable release is cut.

## [Unreleased]

### Changed

- Admin Sources now supports authenticated, CSRF-protected inspection and creation of validated
  custom Spotify shows. Review reports stay in the Admin session; Create validates with Spotify
  again before saving. Existing-source lifecycle and playlist assignment UI remain future work.
- Added a bounded Spotify show intake service: canonical raw IDs, show URIs and Spotify show URLs
  are normalized locally; duplicate identities are rejected before provider calls, and official show
  metadata plus catalogue coverage are inspected without persisting candidate/source state.
- Managed state now uses schema v3 with durable, strictly validated installation-owned Spotify-first
  source definitions. v1/v2 files migrate atomically with an empty `sources` collection; built-in
  definitions remain image-owned. Disabled selected sources preserve the current destination. Add
  Source lifecycle UI is not included yet.
- Source origin now distinguishes country and institutional providers. United Nations sources display
  `UN · INT · en` and `UN · INT · es` without an invented country; origin classification does not
  change runtime selection.

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
