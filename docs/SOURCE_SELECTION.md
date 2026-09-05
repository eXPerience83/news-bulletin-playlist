# Source selection and classification

This document defines how candidate news sources are classified before they are promoted into the built-in runtime catalog.

Three independent questions must stay visible:

1. **Where does the provider/product originate?**
2. **What geographic/editorial scope does this bulletin actually cover?**
3. **What language/locale is the bulletin published in?**

We use the compact project notation:

> **`ORIGIN · SCOPE · LANGUAGE`**

Examples:

- `ES · NAT · es-ES` — Spain provider, primarily national/general Spain news, Spanish from Spain;
- `US · INT · es` — US provider, genuinely international/global news, general Spanish;
- `US · INT · en` — US-based provider/product, global scope, English;
- `FR · INT · fr` — French provider, global scope, French;
- `MX · NAT · es-419` — Mexican provider, national Mexico scope, Latin-American Spanish;
- `FR · REG · es-419` — French provider, regional Americas scope, Latin-American Spanish.

The three axes are source metadata. They are deliberately independent from playlist names. A playlist may still use compact product shorthand such as `INT · ES` while selecting sources from more than one provider country.

## Origin

`ORIGIN` describes the provider/product's primary country or operational origin. It does **not** decide where the source can be used.

This is why **CNN 5 Cosas remains useful for Noticias en Español even though its provider country is US**: its source classification is `US · INT · es`, so the editorial product is global rather than US-local.

For international institutions, the runtime may use a practical operational country/HQ value when a country field is required. Editorial suitability must always come from `SCOPE`, never inferred from that country value.

## Editorial scope

The runtime source model exposes these scope values:

- `LOC` — local/city or similarly narrow local coverage;
- `REG` — regional or supra-national regional coverage, such as the Americas or Latin America;
- `NAT` — primarily one national news market;
- `INT` — genuinely international/world coverage across countries;
- `MIX` — a recurring bulletin intentionally mixes national and international coverage without one clearly dominating.

A foreign publisher is **not automatically `INT`**. For example, a Spanish-language bulletin made in Canada but devoted mainly to Canadian news would be `CA · NAT · <language>` and would not be selected merely because it is foreign and in Spanish.

Likewise, `RFI Español — Noticias de América` is regional rather than global. It belongs conceptually under `FR · REG · es-419`, not the default `INT · ES` product.

## Language and locale

Use BCP-47-compatible tags in runtime metadata whenever the variant matters:

- `es-ES` — Spanish from Spain;
- `es-419` — Latin-American Spanish;
- `es` — general/neutral Spanish when a more precise variant is not justified;
- `en` — English;
- `fr` — French;
- `de` — German;
- `pl` — Polish.

A source can be editorially useful even when its provider country differs from the target playlist. Language and editorial scope decide suitability independently from origin.

## Duration-profile gate

A playlist duration ceiling and a source's normal editorial format are different things.

**A source whose normal product is predominantly long-form is not promoted to the default runtime catalog merely because some episodes fit under the playlist's 30-minute ceiling.**

Every candidate should be classified from a representative recent sample as one of:

- **Concise** — normal editions are clearly bulletin/news-summary length;
- **Mixed** — normally concise, but with occasional longer editions or identifiable longer tranches;
- **Long-form** — long episodes are the normal product rather than an exception.

Default runtime promotion accepts `Concise` and may accept `Mixed` when the concise bulletin identity is clear and occasional long editions can safely be removed by playlist duration policy. `Long-form` candidates remain research-only unless a deterministic concise sub-feed/sub-series can be isolated.

This is deliberately a source-level editorial gate. The current per-playlist default of **1800 seconds / 30 minutes** remains the final destination-side safety filter and can be configured independently. Issue #132 tracks the evidence-based decision on the eventual long-term default.

## Current runtime classification

| Source | `ORIGIN · SCOPE · LANGUAGE` | Duration profile | Runtime role |
| --- | --- | --- | --- |
| Cadena SER | `ES · NAT · es-ES` | Mixed | Spain/general Spanish; hourly bulletins with a longer morning edition |
| Radio Nacional de España | `ES · NAT · es-ES` | Concise | Spain/general Spanish recurring bulletins |
| Onda Cero | `ES · NAT · es-ES` | Concise | Spain/general Spanish recurring bulletins |
| ABC — Las Noticias de ABC | `ES · NAT · es-ES` | Concise | Spain/general Spanish daily news summary |
| CNN 5 Cosas | `US · INT · es` | Concise | global/international Spanish briefing; intentionally available to both general Spanish and international-Spanish playlists |
| UN News Today | `US · INT · en` | Concise | global English bulletin |
| RFI — Journal Monde | `FR · INT · fr` | Mixed | world-news bulletin; ordinary journals are concise, with occasional longer tranches |
| Deutschlandfunk — Die Nachrichten | `DE · MIX · de` | Concise | short recurring German bulletin mixing Germany and world news |
| RMF FM — Fakty | `PL · MIX · pl` | Mixed | short recurring Polish bulletin mixing domestic/world news, with occasional longer editions |

The initial `INT · DE` and `INT · PL` playlist templates are therefore experiments using strong concise `MIX` sources while research continues for purer `INT` sources. Source classification remains truthful even when a playlist uses the source provisionally.

## Current research-only examples

| Candidate | Classification | Status / reason |
| --- | --- | --- |
| BBC Global News Podcast | `GB · INT · en` | **Long-form**; not promoted under the concise-source rule |
| RFI Español — Noticias de América | `FR · REG · es-419` | Regional Americas product; potentially useful for a future Latin-American playlist, not a default global-Spanish source |
| ONU en minutos | `INT-origin · INT · es` pending exact origin metadata | Strong concise global candidate; deterministic Spotify show identity still needs verification before promotion |

Other candidates such as DW, France 24, COPE or additional national broadcasters should be classified only after checking the actual bulletin product, not the publisher brand in isolation.

## Promotion checklist

Before a source is added to the built-in catalog, record evidence for all of the following:

- stable feed/discovery endpoint;
- deterministic source-native identity;
- deterministic Spotify show identity;
- matching strategy that does not depend on global search;
- provider/product origin;
- editorial scope (`LOC`, `REG`, `NAT`, `INT`, or `MIX`);
- language/locale tag;
- representative recent duration profile (`Concise`, `Mixed`, or `Long-form`);
- normal publication cadence;
- geographic/editorial fit for at least one managed playlist;
- any known release delay or title/timestamp quirks;
- safe behavior when the source or Spotify catalogue is temporarily unavailable.

A recognizable brand, broad news coverage, or the existence of a podcast feed is not by itself enough for promotion.
