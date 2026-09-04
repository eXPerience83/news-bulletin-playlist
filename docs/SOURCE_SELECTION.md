# Source selection and classification

This document defines how candidate news sources are classified before they are promoted into the built-in runtime catalog.

The goal is to make two independent questions visible at a glance:

1. **What geographic/editorial scope does this source serve?**
2. **What language/locale is the bulletin intended for?**

We use the compact project notation:

> **`SCOPE · LANGUAGE`**

Examples:

- `INT · ES` — genuinely international/global news in Spanish;
- `ES · ES` — Spain-focused/general news in Spanish;
- `INT · FR` — genuinely international/global news in French;
- `FR · FR` — France-focused/general news in French;
- `MX · LA` — Mexico-focused news in Latin-American Spanish;
- `LATAM · LA` — Latin-America-focused news in Latin-American Spanish.

These labels are project editorial shorthand, not a replacement for the source model's country/language metadata. A source can eventually be relevant to more than one playlist; the label is meant to make the primary editorial fit explicit during research and review.

## Scope tokens

- `INT` — cross-country/global editorial scope. It must not be merely a foreign source or a regional service outside the target country; the bulletin itself should cover international/world news broadly.
- ISO-like country tokens such as `ES`, `FR`, `DE`, `PL`, `MX` — primarily tied to that national news market.
- Region tokens such as `LATAM` may be used when the product is intentionally regional rather than global or country-specific.

The producer's physical country is not enough to determine scope. For example, a US-produced Spanish bulletin can be `INT · ES` when its editorial product is genuinely global.

## Language/locale tokens

Current shorthand:

- `ES` — Spanish suitable for the general Spain/neutral-Spanish playlists;
- `LA` — Latin-American Spanish;
- `EN` — English;
- `FR` — French;
- `DE` — German;
- `PL` — Polish.

Additional language/locale tokens can be added when a real playlist/source requires them.

## Duration-profile gate

A playlist duration ceiling and a source's editorial format are different things.

**A source whose normal product is predominantly long-form is not promoted to the default runtime catalog merely because some episodes fit under the playlist's 30-minute ceiling.**

Every candidate should be classified from a representative recent sample as one of:

- **Concise** — normal editions are clearly bulletin/news-summary length;
- **Mixed** — normally concise, but with occasional longer editions or identifiable longer tranches;
- **Long-form** — long episodes are the normal product rather than an exception.

Default runtime promotion accepts `Concise` and may accept `Mixed` when the concise bulletin identity is clear and occasional long editions can safely be removed by playlist duration policy. `Long-form` candidates remain research-only unless a deterministic concise sub-feed/sub-series can be isolated.

This is deliberately a source-level editorial gate. The current per-playlist default of **1800 seconds / 30 minutes** remains the final destination-side safety filter and can be configured independently.

## Current runtime classification

| Source | Primary fit | Duration profile | Runtime role |
| --- | --- | --- | --- |
| Cadena SER | `ES · ES` | Mixed | Spain/general Spanish; hourly bulletins with a longer morning edition |
| Radio Nacional de España | `ES · ES` | Concise | Spain/general Spanish recurring bulletins |
| Onda Cero | `ES · ES` | Concise | Spain/general Spanish recurring bulletins |
| ABC — Las Noticias de ABC | `ES · ES` | Concise | Spain/general Spanish daily news summary |
| CNN 5 Cosas | `INT · ES` | Concise | global/international Spanish briefing |
| UN News Today | `INT · EN` | Concise | global English bulletin |
| RFI — Journal Monde | `INT · FR` | Mixed | world-news bulletin; ordinary journals are concise, with occasional longer tranches |
| Deutschlandfunk — Die Nachrichten | `INT · DE` | Concise | short recurring German-language news bulletin with national and world coverage |
| RMF FM — Fakty | `INT · PL` | Mixed | short recurring Polish-language news service with occasional longer editions |

The `INT · DE` and `INT · PL` assignments describe their current use in the international-language playlists. If later research finds a better source split between national and international products, the project can introduce `DE · DE` / `PL · PL` playlists without changing the single-engine architecture.

## Current research-only examples

| Candidate | Classification | Status / reason |
| --- | --- | --- |
| BBC Global News Podcast | `INT · EN` | **Long-form**; not promoted under the concise-source rule |
| RFI Español — Noticias de América | `LATAM · LA` | Regionally scoped to the Americas; potentially useful for a future Latin-American playlist, not a default global-Spanish source |
| ONU en minutos | likely `INT · ES` | Strong concise global candidate; deterministic Spotify show identity still needs verification before promotion |

Other candidates such as DW, France 24, COPE or additional national broadcasters should be classified only after checking the actual bulletin product, not the publisher brand in isolation.

## Promotion checklist

Before a source is added to the built-in catalog, record evidence for all of the following:

- stable feed/discovery endpoint;
- deterministic source-native identity;
- deterministic Spotify show identity;
- matching strategy that does not depend on global search;
- primary `SCOPE · LANGUAGE` classification;
- representative recent duration profile (`Concise`, `Mixed`, or `Long-form`);
- normal publication cadence;
- geographic/editorial fit for at least one managed playlist;
- any known release delay or title/timestamp quirks;
- safe behavior when the source or Spotify catalogue is temporarily unavailable.

A recognizable brand, broad news coverage, or the existence of a podcast feed is not by itself enough for promotion.
