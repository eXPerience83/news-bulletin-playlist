# Changelog

All notable product and runtime changes are recorded here. The project is still pre-release, so the
current work remains under **Unreleased** until a stable release is cut.

## [Unreleased]

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
- Generic deterministic `release_date_title` matching strategy for feeds whose titles do not expose a
  trustworthy semantic edition timestamp: the engine matches exact normalized title + compatible
  Spotify release date inside the configured show identity.
- Source editorial-scope metadata and three-axis source classification:
  **`ORIGIN · SCOPE · LANGUAGE`**, separating provider country from actual news coverage and language.
- Admin source labels/table now expose country, scope and language. For example, CNN 5 Cosas is
  represented as `US · INT · es`, so its US origin is not confused with US-only editorial coverage.
- Plain-text project attribution appended to Spotify playlist descriptions on metadata creation/sync:
  `Proyecto / Project: https://github.com/eXPerience83/news-bulletin-playlist`.
- Sanitized per-source/per-playlist duration histograms in diagnostics, plus explicit events for
  accepted editions >=20 minutes and duration exclusions, to support the evidence review in #132.
- Source-selection documentation that rejects predominantly long-form products even when individual
  episodes fit below a playlist hard limit.
- Runtime support for Reuters World News, ONU en minutos, N+ Univision 24-7 and DW Actualidad en
  análisis. ONU's broad UN Spanish audio feed is filtered fail-closed to dated ONU-en-minutos
  editions. CBC World Report remains verified research but is withheld from the runtime catalogue
  because its official RSS did not meet the production provider-watch fetch contract.

### Changed

- Current general/default episode ceiling increased from **8 minutes / 480 seconds** to
  **30 minutes / 1800 seconds**. It remains configurable per managed playlist. The long-term default
  will be selected from production duration evidence under #132.
- The legacy schema-v1 YAML path now inherits the same shared 30-minute default when a duration-policy
  object omits `default_max_seconds`; an explicit YAML value still overrides it.
- `Noticias España` product naming becomes **Noticias en Español**, matching its existing cover and
  reflecting that the playlist combines Spain sources with selected genuinely global Spanish news.
- `Noticias en Español` defaults to SER, RNE, Onda Cero and ABC.
- `INT · ES` starts with **CNN 5 Cosas** as a genuinely global Spanish source. Region-specific
  products such as RFI Noticias de América remain research candidates rather than global defaults.
- `INT · EN` uses concise **UN News Today** and **Reuters World News** instead of the predominantly
  long-form BBC Global News Podcast candidate.
- RFI Journal Monde, Deutschlandfunk and RMF Fakty seed the first FR/DE/PL playlist experiments while
  source classification remains honest about `INT` versus `MIX` editorial scope.
- Spain-origin sources now carry `es-ES` language metadata where the Spain variant is known; CNN
  remains general `es` metadata rather than being mislabeled as Spain-origin Spanish.

### Fixed

- Avoided the old 8-minute default silently dropping useful ordinary editions such as the observed
  **RNE 10:05 / 605-second** bulletin from production diagnostics on build `71788836ea1e`.
- Spotify description attribution remains outside editable managed state, preventing duplicate project
  URLs across repeated metadata updates while staying within the playlist description length limit.

### Validation / follow-up

- #129 tracks configurable duration policy implementation.
- #130 is the reviewed multi-playlist/dev-candidate PR.
- #132 tracks production log review and the eventual default hard-duration decision.
- #53 remains the living source research catalog; #44 remains the playlist rollout matrix.
