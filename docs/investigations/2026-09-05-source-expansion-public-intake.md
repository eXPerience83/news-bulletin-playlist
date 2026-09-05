# Post-#130 source expansion — public technical intake

Date: 2026-09-05

This is a public-web/feed intake for the follow-up source-expansion work. It records deterministic identities that can be established without Spotify OAuth. It does **not** promote any source into the runtime catalog by itself.

## Product placement rule

Spanish source placement uses both editorial market and spoken locale:

- `ES · es-ES`: Spain-focused news spoken in Spain Spanish;
- `LAT · es-419`: Latin-America/Americas-focused news spoken in Latin-American Spanish;
- `INT · es-ES`: world/international news spoken in Spain Spanish;
- `INT · es-419`: world/international news spoken in Latin-American Spanish.

A generic `es` value is neutral/unresolved and must not cause automatic sharing between Spain and LAT products.

CNN 5 Cosas has been confirmed by listening as `US · INT · es-ES`.

## First technical intake set

| Source | Public collection identity | Public Spotify identity | Observed shape | Main unresolved gate |
| --- | --- | --- | --- | --- |
| ONU en minutos | `https://news.un.org/feed/subscribe/es/audio-product/all/audio-rss.xml` | show ID still to verify | daily concise multi-story bulletin, commonly ~5–9m | feed contains broader Noticias ONU Spanish audio; isolate only `La ONU en Minutos <date>` and verify deterministic Spotify show/matching |
| DW Actualidad en análisis | `https://rss.dw.com/xml/podcast_actualidad_en_analisis` | show ID still to verify | frequent international analysis, recent public examples mostly ~17–28m; at least one 32:10 outlier | spoken locale + Spotify show identity/title-date matcher; retain 1800s ceiling |
| N+ Univision 24-7 | `https://feeds.simplecast.com/CC3CSCi5` | `7G8CEhjsTshZeGtPLcuW6T` | daily US/world multi-story programme, often ~12–18m | confirm `es-419` by listening; verify RSS→Spotify title/date stability and overlap policy |
| Reuters World News | `https://feeds.megaphone.fm/reutersworldnews` | `1alpjXkCUjn3Y9fR5xl8fZ` | daily multi-story world briefing around 10m | verify exact title/release-date matching and bounded catalogue behavior |
| CBC World Report | `https://www.cbc.ca/podcasting/includes/wr.xml` | `5qaYz2SRxlPUszXZQWNl1U` | ~10m Canada/world briefing, public description says updated multiple times each morning | determine whether same-day repeated titles/episodes make day-precision matching ambiguous; do not promote until resolved |

## Public evidence notes

### ONU en minutos

- Apple/public podcast directories identify it as an official United Nations / Noticias ONU daily Spanish bulletin.
- Recent public episodes are usually roughly 5–9 minutes.
- Titles are strongly structured (`La ONU en Minutos <date>`), which is promising for deterministic filtering.
- The identified UN RSS is a broader Spanish-audio product feed, so collection must reject unrelated interviews/features rather than ingest the whole feed.
- Exact Spotify show identity must be established read-only before implementation.

### DW Actualidad en análisis

- DW's public podcast feed is stable and product-specific.
- The programme is international analysis rather than a pure headline bulletin, but its ordinary duration is compatible with the current 30-minute configurable ceiling.
- Public recent samples include approximately 17, 19, 21, 22, 25 and 28 minutes, with a known 32:10 outlier that the playlist ceiling should exclude.
- Titles are topic-specific and therefore appear suitable for exact normalized title + release-date matching if Spotify exposes day precision consistently.
- Spoken locale remains a product-placement question to confirm from recent audio.

### N+ Univision 24-7

- Simplecast feed identity is public and stable.
- Spotify show ID is already known.
- The programme describes itself as a daily US/world news product for Hispanic audiences.
- Episode titles are topic-specific rather than a single recurring generic title, which is promising for the existing release-date/title strategy.
- Spoken locale and editorial placement must remain separate: even if `es-419`, US-heavy coverage does not automatically make it a LAT-national/regional default.

### Reuters World News

- Exact Megaphone RSS and Spotify show ID are public.
- Reuters describes the product as a daily ten-minute world briefing.
- Public Spotify examples remain tightly clustered around ~9–11 minutes.
- This is the strongest immediate candidate to broaden `INT · EN` after authenticated matcher verification.

### CBC World Report

- Exact CBC RSS and Spotify show ID are public.
- CBC describes it as a ten-minute Canada/world briefing with multiple updates each morning.
- That cadence creates a specific matching risk: if multiple episodes on one calendar day reuse the same title and Spotify only exposes day-level release precision, exact title + release date may remain ambiguous.
- Authenticated catalogue inspection must determine whether Spotify actually exposes one or multiple same-day episodes and whether titles/native IDs provide a deterministic discriminator.

## Read-only Spotify gate

Before implementation, inspect each configured/located show with authenticated **GET-only** calls and record:

1. exact show ID and publisher/name verification;
2. recent bounded episode pages and pagination;
3. `release_date` and `release_date_precision`;
4. exact Spotify episode titles;
5. duration distribution;
6. duplicate title/date collisions;
7. unavailable/null items if any;
8. whether existing `release_date_title` matching is deterministic;
9. any source requiring a dedicated parser/matcher strategy.

No playlist create/update/delete, metadata write, cover write or item mutation is required for this intake.
