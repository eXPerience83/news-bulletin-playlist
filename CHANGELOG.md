# Changelog

All notable product and runtime changes are recorded here. The project remains pre-1.0; unreleased
work stays under **Unreleased**, while reviewed development milestones are recorded below.

## [Unreleased]

No user-visible changes yet.

## [0.1.0] - 2026-09-11

### Added

- Authenticated managed-playlist administration backed by installation-owned managed state, with
  built-in playlist/source catalog data shipped by the image.
- Initial managed playlist templates for Spain/Spanish plus international news in Spanish, English,
  French, German and Polish, with bundled Spotify cover assets.
- Managed per-playlist maximum episode duration, persisted locally and editable without reconnecting
  Spotify or forcing playlist metadata writes.
- Runtime source support for SER, RNE, Onda Cero, ABC, CNN 5 Cosas, UN News Today, Reuters World
  News, ONU en minutos, N+ Univision 24-7, DW Actualidad en análisis, RFI Journal Monde,
  Deutschlandfunk Die Nachrichten and RMF FM Fakty.
- Three-axis source classification (`ORIGIN · SCOPE · LANGUAGE`) so provider country, editorial scope
  and spoken language/locale remain independent.
- Sanitized structured diagnostics with bounded retention, authenticated filtering/export and
  actionable reconciliation failure context.
- Spotify Web API quota observability, durable destination-planner evidence and process-local
  verification leases.
- Snapshot-first periodic destination audits for settled managed playlists, allowing an eligible
  audit to verify with one snapshot request and no item read or write when the attested snapshot is
  unchanged.

### Changed

- Playlist chronology and 48-hour eligibility prefer semantic bulletin `edition_at` over RSS
  publication time, with `published_at` retained as the fallback and for source diagnostics.
- General/default maximum episode duration increased from 8 minutes to a provisional configurable
  30 minutes while production evidence is gathered for the long-term product policy.
- `Noticias España` evolved into **Noticias en Español**, while source classification now keeps Spain,
  Latin-American and international Spanish products conceptually separate.
- Spotify work is now planner-driven and local-first: unchanged cycles can avoid OAuth refresh,
  Spotify client construction and Web API calls when qualified process-local evidence allows it.
- Periodic destination verification runs on the six-hour audit model, with snapshot-first verification
  used only for clean `audit_only` work; dirty, unknown, pending or incompatible states still use the
  conservative reconciler.
- Playlist descriptions include plain-text project attribution when Spotify accepts it, without
  making cosmetic metadata a provisioning dependency.

### Fixed

- Spotify HTTP 429 now establishes a durable runtime-wide cooldown that survives restarts, honors
  valid `Retry-After` values and suppresses further Spotify I/O without stopping ordinary RSS/local
  work.
- Persisted Spotify episode mappings are revalidated before playlist writes so stale `MATCHED` state
  cannot authorize destructive reconciliation.
- Successful writes whose exact items are already visible while Spotify still exposes an older
  snapshot are kept as durable pending snapshot confirmations instead of triggering duplicate writes.
- Partial/unavailable playlist observations, ambiguous writes and unexpected snapshot transitions stay
  fail-closed and cannot create false strong attestations.
- Periodic audit verification no longer performs unnecessary multi-page item reads when a settled
  attested snapshot is unchanged.
- Diagnostics preserve actionable failure reasons and keep retention/export limits meaningful instead
  of collapsing useful provider/reconciliation detail.

### Validation / follow-up

- Spotify quota optimization phases are tracked under #156; Phase 5 snapshot-first destination audits
  merged in #171 and are awaiting natural production-audit confirmation before #161 is marked
  production-validated.
- #132 continues to own production duration evidence and the eventual concise-vs-long product
  decision.
- #53 remains the living source research catalog and #44 remains the playlist rollout matrix.
