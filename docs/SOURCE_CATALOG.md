# Bulletin source catalog

This document is the version-controlled research inventory for bulletin sources tracked by
[#53](https://github.com/eXPerience83/news-bulletin-playlist/issues/53).

It answers a different question from the runtime catalog:

> Which short, frequently updated news bulletins exist, and how close is each one to being
> technically usable by the engine?

The runtime `BUILTIN_CATALOG` remains the authoritative list of sources that the application
actually supports. An entry in this document does **not** become selectable or runnable merely
because it is listed here.

## Boundaries

Keep three concerns separate:

1. **Research catalog (this document / #53):** candidates, evidence, blockers and promotion state.
2. **Built-in runtime catalog (`catalog.py`):** implemented sources with deterministic provider and
   Spotify contracts plus automated tests.
3. **Installation state (`/data`):** which supported sources a particular installation selects for
   each managed playlist.

Research candidates must never be loaded dynamically into production configuration. Promotion is a
code change reviewed through the normal PR, CI and provider-contract process.

## Status model

| Status | Meaning |
| --- | --- |
| `research` | Market/source is still being explored; no sufficiently strong candidate is established. |
| `candidate` | A plausible bulletin has been identified but still needs full deterministic verification. |
| `verified` | Feed/catalogue/cadence/duration and matching feasibility have been checked; implementation may still be pending. |
| `implemented` | The source is present in the built-in runtime catalog with parser/matching support and tests. |
| `blocked` | The source is useful, but a required deterministic path is missing or unsuitable. |
| `rejected` | The source was evaluated and does not fit the product; keep the reason so it is not repeatedly rediscovered. |

A status may include a temporary qualifier such as `implemented / review` when the current source is
supported but a better bulletin/feed should still be investigated.

## Promotion contract

Before moving a source from research/candidate into the runtime catalog, verify and record at least:

- an official programme/feed identity and stable collection URL;
- current publication cadence from real recent editions;
- typical duration and meaningful outliers;
- stable source identity/GUID behaviour;
- source timezone and edition-time/title pattern;
- a deterministic Spotify show/catalogue identity when Spotify is the destination;
- deterministic source-edition to Spotify-episode matching without global search guesses;
- compatibility with the engine's existing failure-safety model;
- parser/matcher/provider-watch tests appropriate to the source;
- any deliberately longer recurring edition as a **bounded common-policy exception**, not an
  ad-hoc parser or source-wide duration bypass.

Promotion should be done by a focused implementation PR. Updating research notes alone must never
change production behaviour.

## Runtime-supported sources

These are the sources currently present in `BUILTIN_SOURCES`; they are the only entries from this
inventory that the managed runtime exposes today.

| Source id | Country | Language | Bulletin/provider | Status | Notes |
| --- | --- | --- | --- | --- | --- |
| `ser` | ES | es | Cadena SER — Las noticias de la SER | `implemented` | Hourly core source. The recurring long 08:00 edition is handled by the bounded `ser_morning_0800` playlist-policy exception rather than parser logic. |
| `rne` | ES | es | RNE — Noticias RNE | `implemented` | Frequent short national bulletins. Runtime matching supports the observed delayed Spotify release date. |
| `ondacero` | ES | es | Onda Cero — Las noticias en Onda Cero | `implemented / review` | Existing deterministic runtime path; continue checking whether a shorter or more granular bulletin feed is preferable. |
| `cnn` | US | es | CNN — 5 Cosas | `implemented` | Spanish-language international source currently available to the Spain playlist through explicit source membership. |

## Research inventory

The table below is the initial backlog captured from #53. A `candidate` label is intentionally not a
claim that the source is ready for production.

| Country | Lang | Bulletin/source | Status | Current research note |
| --- | --- | --- | --- | --- |
| ES | es | ABC — Las Noticias de ABC | `candidate` | Short daily bulletin candidate; deterministic runtime verification still required. |
| ES | es | COPE — boletines | `blocked` | Parser/research exists, but a deterministic Spotify catalogue path was not verified in the P0 work. |
| DE | de | Deutschlandfunk — Die Nachrichten | `candidate` | Hourly, with additional half-hour editions during parts of weekdays; strong product fit to verify. |
| DE | de | Tagesschau in 100 Sekunden | `candidate` | Multiple very short updates per day; verify feed identity and deterministic Spotify path. |
| FR | fr | RTL — Le journal RTL | `candidate` | Hourly short bulletin candidate; full deterministic verification pending. |
| FR | fr | Europe 1 — Le journal | `candidate` | Frequent feed with both short and longer editions; common duration policy is relevant. |
| PL | pl | RMF FM — Fakty | `candidate` | Roughly hourly short bulletin candidate. |
| PL | pl | TOK FM — Informacje | `candidate` | Frequent normal bulletin-length editions with observed long outliers to characterise. |
| PL | pl | Radio ZET — Wiadomości | `candidate` | Hourly bulletin candidate. |
| IT | it | Sky TG24 — news bulletin feed | `candidate` | Multiple short editions per day; deterministic feed/catalogue verification pending. |
| PT | pt | Antena 1 — Noticiário | `candidate` | Many daily concise editions; verify identity and Spotify path. |
| BE | nl | VRT NWS update | `candidate` | Hourly, explicitly short-format candidate; verify collection/catalogue contract. |
| CH | de | SRF Nachrichten | `candidate` | Multiple short daily editions; verify deterministic destination path. |
| FI | fi | Yle Uutiset / Radiouutiset | `candidate` | Short radio-news editions found; Spotify catalogue path still needs verification. |
| SE | sv | Sveriges Radio — Ekot senaste nytt | `candidate` | Hourly short bulletin candidate; strong fit to verify. |
| DK | da | DR — Radioavisen | `candidate` | Hourly short bulletin candidate. |
| GB | en | Times Radio News Briefing | `candidate` | Short editions several times per day; deterministic runtime verification pending. |
| GB | en | BBC Radio 4 — News Summary | `candidate` | Strong short-format candidate; verify stable individual Spotify catalogue path. |
| AT | de | Ö1 — Nachrichten / Journale | `candidate` | Useful short editions appear to exist; verify that target editions can be isolated deterministically. |
| IE | en | RTÉ news bulletins | `research` | Search for a better short/frequent candidate than long-form programmes. |
| NL | nl | NOS | `research` | Continue searching for a true short recurring bulletin rather than single-topic explainers. |
| RO | ro | Radio România Actualități — Știri | `candidate` | Hourly schedule appears promising; verify feed and Spotify catalogue. |

## Coverage backlog

Research should continue by **country + language**, not country alone. Priority coverage currently
includes Spain/Spanish; France/French; Germany/German; Poland/Polish; United Kingdom/English;
Italy/Italian; Portugal/Portuguese; Netherlands/Dutch; Belgium in viable languages; Austria/German;
Switzerland in viable German/French/Italian combinations; Ireland/English; Sweden/Swedish;
Denmark/Danish; Norway/Norwegian; Finland/Finnish and Swedish where viable; Czechia/Czech;
Slovakia/Slovak; Romania/Romanian; Hungary/Hungarian; Greece/Greek; and other European markets when
source quality justifies them.

Multilingual countries must remain separate country/language combinations. International-by-language
playlists and country-specific playlists are separate product decisions tracked in #44.

## Review workflow

[#55](https://github.com/eXPerience83/news-bulletin-playlist/issues/55) owns recurring review of this
catalog: monthly while European coverage is expanding, then quarterly once the main markets are
stable, with an extra review after major broadcaster/feed or Spotify catalogue changes.

For each review:

1. look for new short, frequently updated bulletin candidates;
2. revisit `candidate`, `blocked` and `research` entries;
3. re-check implemented feeds, cadence, title patterns, durations and Spotify catalogue identities;
4. update this document and #53 with meaningful evidence/status changes;
5. open focused implementation issues only for sources actually selected for rollout.

Provider-watch has a narrower role: it detects breakage in already-known implemented provider
contracts. It does not discover new sources or promote research candidates.
