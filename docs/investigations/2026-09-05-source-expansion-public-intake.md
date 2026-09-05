# Post-#130 source expansion — public + authenticated technical intake

Date: 2026-09-05

This investigation records the first post-#130 source-expansion intake. Public web/feed discovery was followed by a bounded authenticated Spotify **GET-only** catalogue/matcher review. No playlist, metadata, cover, item or authorization state was mutated.

It does **not** promote every candidate into the runtime catalog by itself; it records which contracts are deterministic enough to implement and which still require product or safety decisions.

## Product placement rule

Spanish source placement uses both editorial market and spoken locale:

- `ES · es-ES`: Spain-focused news spoken in Spain Spanish;
- `LAT · es-419`: Latin-America/Americas-focused news spoken in Latin-American Spanish;
- `INT · es-ES`: world/international news spoken in Spain Spanish;
- `INT · es-419`: world/international news spoken in Latin-American Spanish.

A generic `es` value is neutral/unresolved and must not cause automatic sharing between Spain and LAT products.

CNN 5 Cosas is already confirmed by listening as `US · INT · es-ES`.

## Authenticated intake summary

| Source | Spotify show ID | Collection | Spotify sample | Duration profile | Release precision | Verdict | Remaining gate |
| --- | --- | --- | ---: | --- | --- | --- | --- |
| ONU en minutos | `77hGWK2o0NYsdS8WuXiLo6` | `https://news.un.org/feed/subscribe/es/audio-product/all/audio-rss.xml` | 50 | min 0:27; median 7:06; p90 8:03; max 8:34; all <=10m | day 50/50 | `READY_DEDICATED_FILTER` | broad UN feed requires a fail-closed product filter; spoken locale unresolved |
| DW Actualidad en análisis | `7CzHDusNXRICUXuefIXbxd` | `https://rss.dw.com/xml/podcast_actualidad_en_analisis` | 50 | min 13:12; median 23:24; p90 29:31; max 33:04; 5/50 >30m | day 50/50 | `READY_RELEASE_DATE_TITLE` | spoken locale unresolved; occasional >30m items remain duration-filtered |
| N+ Univision 24-7 | `7G8CEhjsTshZeGtPLcuW6T` | `https://feeds.simplecast.com/CC3CSCi5` | 50 | min 11:12; median 18:14; p90 22:11; max 23:55 | day 50/50 | `READY_RELEASE_DATE_TITLE` | spoken locale unresolved |
| Reuters World News | `1alpjXkCUjn3Y9fR5xl8fZ` | `https://feeds.megaphone.fm/reutersworldnews` | 50 | min 9:27; median 10:15; p90 10:42; max 10:54 | day 50/50 | `READY_RELEASE_DATE_TITLE` | none observed |
| CBC World Report | `5qaYz2SRxlPUszXZQWNl1U` | `https://www.cbc.ca/podcasting/includes/wr.xml` | 50 | min 10:02; median 10:09; p90 10:09; max 27:42 | day 50/50 | `READY_RELEASE_DATE_TITLE` | monitor future cadence changes; no current same-title/day collision |
| Chequia en 30 minutos | `7FNA3UYPexmOtCtsdWX9QN` | `https://espanol.radio.cz/rcz-rss/show/audio/8705332` | 45 usable / 50 slots | min 25:38; median 28:49; p90 29:29; max 29:48 | day 45; 5 null slots | deterministic content, but blocked by current structural fail-closed matcher policy | safe global handling of unavailable/null Spotify catalogue slots + spoken locale |

Duration fields use Spotify metadata and the observed bounded sample.

## ONU en minutos

- Exact Spotify show ID: `77hGWK2o0NYsdS8WuXiLo6`.
- The supplied public episode examples all resolve to that show. A second duplicate-looking search result exists but is not the official identity used by those episodes and must not be configured.
- The official UN Spanish audio RSS is broad: the observed 100 normalized entries contain 99 `La ONU en Minutos ...` items and one unrelated Spanish UN audio item.
- A strict title/product filter can therefore reject non-product entries fail-closed before matching.
- The selected product entries matched 50/50 against Spotify using exact normalized title + day release date, with no duplicate-title or same-title/day collisions.
- Conceptual classification: `UN · INT · <spoken locale unresolved>`.
- Do not assign `es-ES` or `es-419` until listening confirms it.

## DW Actualidad en análisis

- Exact Spotify show ID: `7CzHDusNXRICUXuefIXbxd`.
- Product RSS and Spotify identity are stable and dedicated to the programme.
- 50/50 recent RSS entries matched by exact normalized title + day release date; no title/day collisions were observed.
- 45/50 sampled episodes are at or below the current 1800-second default; five exceed 30 minutes, with a 33:04 maximum.
- Product decision: keep it eligible for the normal configurable-duration product rather than forcing the entire source into long-form. The existing playlist duration ceiling will reject the outliers.
- Conceptual classification: `DE · INT · <spoken locale unresolved>`.

## N+ Univision 24-7

- Exact Spotify show ID: `7G8CEhjsTshZeGtPLcuW6T`.
- Official collection: `https://feeds.simplecast.com/CC3CSCi5`.
- 50/50 recent RSS entries matched exact title + release day; all observed Spotify dates used day precision and there were no same-title/day collisions.
- The observed catalogue is edition-shaped: one topical multi-story/current-news edition per observed date, roughly 11–24 minutes.
- Technically it is suitable as an international/world candidate independently of the later spoken-locale decision.
- Conceptual classification: `US · INT · <spoken locale unresolved>`.

## Reuters World News

- Exact Spotify show ID: `1alpjXkCUjn3Y9fR5xl8fZ`.
- Official collection: `https://feeds.megaphone.fm/reutersworldnews`.
- 50/50 exact title/day matches, all with day precision; zero duplicate exact titles and zero same-title/day collisions.
- Existing `release_date_title` matching is sufficient.
- Conceptual classification: `GB · INT · en`.
- This is the cleanest immediate candidate to broaden the international-English runtime mix.

## CBC World Report

- Exact Spotify show ID: `5qaYz2SRxlPUszXZQWNl1U`.
- Official collection: `https://www.cbc.ca/podcasting/includes/wr.xml`.
- Repeated weekday titles are common across days, but the 50-item sample had zero same-title/same-release-date collisions.
- RSS and Spotify each exposed one durable archived entry per observed calendar day despite CBC's public statement that the programme may update multiple times each morning.
- Existing day-precision title matching is deterministic in the observed catalogue.
- Conceptual classification: `CA · MIX · en`.
- Keep provider monitoring for a future change from replacement-style updates to multiple retained same-day episodes.

## Chequia en 30 minutos

- Exact Spotify show ID: `7FNA3UYPexmOtCtsdWX9QN`.
- Official dedicated RSS: `https://espanol.radio.cz/rcz-rss/show/audio/8705332`.
- All usable observed Spotify entries have exact title/day-compatible RSS counterparts and no title/day collisions.
- Five of the first 50 Spotify catalogue slots were null/unavailable. The current generic matcher correctly rejects that structurally incomplete catalogue page rather than guessing around missing media.
- Do **not** add a source-specific bypass. Any change must be a globally safe unavailable-item policy that preserves fail-closed semantics for every provider.
- Conceptual classification: `CZ · NAT · <spoken locale unresolved>`.

## Implementation priority after intake

1. Reuters World News — direct `release_date_title`, no material blocker.
2. ONU en minutos — add a dedicated fail-closed product filter, then keep exact title/day matching.
3. N+ Univision 24-7 — direct `release_date_title`; do not assign to ES/LAT defaults until listening resolves locale.
4. DW Actualidad en análisis — direct `release_date_title`; keep the existing 30-minute ceiling and allow outlier exclusion.
5. CBC World Report — direct `release_date_title`; provider-watch should detect future same-day catalogue shape changes.

Chequia remains deferred until a separate globally safe policy for null/unavailable Spotify catalogue slots is designed and tested.

## Human listening still required

The technical intake must not infer spoken locale from publisher origin or Spotify's generic language metadata. Listening remains required for:

- ONU en minutos — `es-ES` vs `es-419`;
- DW Actualidad en análisis — `es-ES` vs `es-419`;
- N+ Univision 24-7 — `es-ES` vs `es-419`;
- Chequia en 30 minutos — `es-ES` vs `es-419`.

No playlist mutation is required to resolve those product-placement decisions.
