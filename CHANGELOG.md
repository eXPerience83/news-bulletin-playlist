# Changelog

All notable product and runtime changes are recorded here. The project remains pre-release; changes
after the latest release stay under **Unreleased** until the next stable release is cut.

## [Unreleased]

No changes yet.

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
