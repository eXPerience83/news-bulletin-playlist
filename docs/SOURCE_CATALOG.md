# Bulletin source catalog

This document is the version-controlled research inventory for bulletin sources tracked by
[#53](https://github.com/eXPerience83/news-bulletin-playlist/issues/53).

It answers a different question from the runtime catalog:

> Which concise, frequently updated news bulletins exist, what do they actually cover, and how close
> is each one to being technically usable by the engine?

The runtime `BUILTIN_CATALOG` remains the authoritative list of sources that the application actually
supports. An entry in this document does **not** become selectable or runnable merely because it is
listed here.

Source classification uses the three-axis `ORIGIN · SCOPE · LANGUAGE` contract defined in
[`SOURCE_SELECTION.md`](SOURCE_SELECTION.md). Provider country, editorial coverage and language are
independent. For example, CNN 5 Cosas is `US · INT · es`: its US origin does not make it a US-local
source, so it can intentionally feed both the general Spanish and international-Spanish playlists.

## Boundaries

Keep three concerns separate:

1. **Research catalog (this document / #53):** candidates, evidence, blockers and promotion state.
2. **Built-in runtime catalog (`catalog.py`):** implemented sources with deterministic provider and
   Spotify contracts plus automated tests.
3. **Installation state (`/data`):** which supported sources a particular installation selects for
   each managed playlist.

Research candidates must never be loaded dynamically into production configuration. Promotion is a
code change reviewed through the normal PR, CI and provider-contract process.

## Status and duration model

| Status | Meaning |
| --- | --- |
| `research` | Market/source is still being explored; no sufficiently strong candidate is established. |
| `candidate` | A plausible bulletin has been identified but still needs full deterministic verification. |
| `verified` | Feed/catalogue/cadence/duration and matching feasibility have been checked; implementation may still be pending. |
| `implemented` | The source is present in the built-in runtime catalog with parser/matching support and tests. |
| `blocked` | The source is useful, but a required deterministic path is missing or unsuitable. |
| `rejected` | The source was evaluated and does not fit the product; keep the reason so it is not repeatedly rediscovered. |

Every candidate also gets a representative duration profile:

- `Concise` — normal editions are bulletin/news-summary length;
- `Mixed` — normal product is concise, with occasional longer editions;
- `Long-form` — long episodes are the normal product.

Predominantly `Long-form` products are not promoted merely because individual episodes happen to fit
under the current 30-minute playlist ceiling. The ceiling is a configurable final safety filter, not
a substitute for choosing bulletin-like sources. Issue #132 owns the later evidence-based decision on
the long-term default ceiling.

## Promotion contract

Before moving a source from research/candidate into the runtime catalog, verify and record at least:

- official programme/feed identity and stable collection URL;
- provider/product origin;
- editorial scope (`LOC`, `REG`, `NAT`, `INT`, or `MIX`);
- language/locale tag;
- current publication cadence from real recent editions;
- representative duration profile and meaningful outliers;
- stable source identity/GUID behaviour;
- source timezone and edition-time/title pattern;
- deterministic Spotify show/catalogue identity when Spotify is the destination;
- deterministic source-edition to Spotify-episode matching without global search guesses;
- compatibility with the engine's existing failure-safety model;
- parser/matcher/provider-watch tests appropriate to the source.

Promotion should be done by a focused implementation PR. Updating research notes alone must never
change production behaviour.

## Runtime sources through PR #137

These are the sources exposed by the current multi-playlist dev candidate branch.

| Source id | `ORIGIN · SCOPE · LANGUAGE` | Bulletin/provider | Duration profile | Role |
| --- | --- | --- | --- | --- |
| `ser` | `ES · NAT · es-ES` | Cadena SER — Las noticias de la SER | Mixed | Spain/general Spanish; hourly bulletins with a longer morning edition |
| `rne` | `ES · NAT · es-ES` | RNE — Noticias RNE | Concise | recurring Spain news bulletins; observed useful editions can exceed the old 8-minute limit |
| `ondacero` | `ES · NAT · es-ES` | Onda Cero — Las noticias en Onda Cero | Concise | recurring Spain radio bulletins |
| `abc` | `ES · NAT · es-ES` | ABC — Las Noticias de ABC | Concise | daily Spain/general news summary |
| `cnn` | `US · INT · es-ES` | CNN — 5 Cosas | Concise | global/international Spain-Spanish briefing; default for `INT · ES`, not the Spain-national product |
| `un_news_en` | `US · INT · en` | United Nations — UN News Today | Concise | daily global English bulletin, preferred over long-form BBC Global News Podcast |
| `reuters_world` | `GB · INT · en` | Reuters — World News | Concise | daily ~10-minute global briefing; `INT · EN` default alongside UN News Today |
| `cbc_world_report` | `CA · MIX · en` | CBC — World Report | Concise | daily Canada/world newscast, registered for later country use |
| `un_news_es` | `US · INT · es` | United Nations — ONU en minutos | Concise | broad UN Spanish audio feed is explicitly filtered to dated ONU-en-minutos editions; spoken locale unresolved |
| `nplus_univision` | `US · INT · es` | N+ Univision 24-7 | Concise | international/US Spanish daily newscast; spoken locale unresolved |
| `dw_actualidad` | `DE · INT · es` | DW — Actualidad en análisis | Mixed | normally under 30 minutes, with occasional longer episodes excluded by destination duration policy; spoken locale unresolved |
| `rfi_fr` | `FR · INT · fr` | RFI — Journal Monde | Mixed | world-news journals, ordinarily around bulletin length with occasional longer tranches |
| `dlf_news` | `DE · MIX · de` | Deutschlandfunk — Die Nachrichten | Concise | short recurring Germany/world bulletin; provisional source for the initial German playlist |
| `rmf_fakty` | `PL · MIX · pl` | RMF FM — Fakty | Mixed | short recurring Poland/world bulletin with occasional longer editions; provisional source for the initial Polish playlist |

The Phase-1 playlist templates are intentionally broader than the source labels:

- `Noticias en Español`: SER, RNE, Onda Cero and ABC;
- `INT · ES`: CNN 5 Cosas;
- `INT · EN`: UN News Today and Reuters World News;
- `INT · FR`: RFI Journal Monde;
- `INT · DE`: Deutschlandfunk — Die Nachrichten, provisionally while purer `INT` sources are researched;
- `INT · PL`: RMF FM — Fakty, provisionally while purer `INT` sources are researched.

## Research inventory

| Proposed classification | Bulletin/source | Status | Current research note |
| --- | --- | --- | --- |
| `ES · NAT · es-ES` | COPE — boletines | `blocked` | Frequent concise bulletins and parser research exist, but deterministic Spotify show identity remains unverified. |
| `CZ · NAT · es` | Chequia en 30 minutos | `blocked / #138` | Feed and show are known, but nullable Spotify catalogue entries require the dedicated global safety policy. |
| `FR · REG · es-419` | RFI Español — Noticias de América | `candidate` | Americas-regional Spanish product; useful for a future Latin-American scope, not default `INT · ES`. |
| `DE · NAT · de` | Tagesschau in 100 Sekunden | `candidate` | Multiple very short updates per day; verify feed identity and deterministic Spotify path. |
| `FR · NAT · fr` | RTL — Le journal RTL | `candidate` | Hourly short bulletin; strong candidate for a future `FR · FR` product. |
| `FR · NAT · fr` | Europe 1 — Le journal | `candidate` | Frequent feed containing both short and longer editions; measure representative profile. |
| `PL · NAT · pl` | TOK FM — Informacje | `candidate` | Frequent normal bulletin-length editions with anomalous long entries to characterise. |
| `PL · NAT · pl` | Radio ZET — Wiadomości | `candidate` | Hourly national bulletin candidate. |
| `IT · NAT/MIX · it` | Sky TG24 — news bulletin feed | `candidate` | Multiple short editions per day; deterministic feed/catalogue verification pending. |
| `PT · NAT · pt` | Antena 1 — Noticiário | `candidate` | Many daily concise editions; verify identity and Spotify path. |
| `BE · NAT · nl` | VRT NWS update | `candidate` | Hourly explicitly short product; verify collection/catalogue contract. |
| `CH · NAT/MIX · de` | SRF Nachrichten | `candidate` | Multiple short daily editions; verify deterministic destination path. |
| `FI · NAT · fi` | Yle Uutiset / Radiouutiset | `candidate` | Short radio-news editions found; Spotify path still needs verification. |
| `SE · NAT · sv` | Sveriges Radio — Ekot senaste nytt | `candidate` | Hourly short bulletin; strong fit to verify. |
| `DK · NAT · da` | DR — Radioavisen | `candidate` | Hourly short bulletin candidate. |
| `GB · NAT/MIX · en` | Times Radio News Briefing | `candidate` | Short editions several times per day; deterministic runtime verification pending. |
| `GB · NAT · en` | BBC Radio 4 — News Summary | `candidate` | Strong short format; verify stable individual Spotify catalogue path. |
| `AT · NAT/MIX · de` | Ö1 — Nachrichten / Journale | `candidate` | Useful short editions appear to exist; verify target editions can be isolated deterministically. |
| `IE · NAT · en` | RTÉ news bulletins | `research` | Search for a better short/frequent deterministic product than long-form programmes. |
| `NL · NAT · nl` | NOS | `research` | Continue searching for a true short recurring bulletin rather than single-topic explainers. |
| `RO · NAT · ro` | Radio România Actualități — Știri | `candidate` | Hourly schedule appears promising; verify feed and Spotify catalogue. |
| `GB · INT · en` | BBC Global News Podcast | `rejected` | Strong world scope but predominantly long-form, so unsuitable for the default concise-news product. |

`NAT/MIX` above means classification itself still needs verification; only built-in runtime sources get a
single authoritative scope value.

## Spain / Spanish evidence — 2026-09-04

- SER official podcast page still describes hourly updates and exposes RSS/Spotify distribution:
  <https://cadenaser.com/podcast/cadena-ser/1043/>.
- Spotify still exposes the deterministic SER show as
  <https://open.spotify.com/show/4EwwdoHHYmbt49UXODQMpi>.
- Spotify still exposes Noticias RNE as
  <https://open.spotify.com/show/0UgidTKsoaHiHDARuPQNW1>. Production diagnostics from build
  `71788836ea1e` showed a useful **605-second / 10:05** RNE edition rejected solely by the former
  480-second ceiling, one of the concrete reasons the current candidate uses 1800 seconds by default.
- Onda Cero's official bulletin page is actively publishing hourly editions:
  <https://www.ondacero.es/podcast/programas/boletines/>.
- ABC's active podcast has a stable Omny RSS path
  (<https://omny.fm/shows/las-noticias-de-abc-1/playlists/podcast.rss>) and Spotify show
  (<https://open.spotify.com/show/0cLJl7pvrr1bkUaKiVRggf>); PR #130 adds deterministic runtime
  support using title + release-date matching inside that configured show.
- COPE continues to expose frequent short bulletins, but remains blocked until a deterministic Spotify
  show identity is established rather than guessed through global search.

## Review workflow

[#55](https://github.com/eXPerience83/news-bulletin-playlist/issues/55) owns recurring review of this
catalog. For each review:

1. look for new concise bulletin candidates;
2. classify origin, editorial scope and language independently;
3. measure representative episode durations, rejecting predominantly long-form products;
4. revisit `candidate`, `blocked` and `research` entries;
5. re-check implemented feeds, cadence, title patterns and Spotify catalogue identities;
6. update this document and #53 with meaningful evidence/status changes;
7. open focused implementation work only for sources actually selected for rollout.

Provider-watch has a narrower role: it detects breakage in already-known implemented provider
contracts. It does not discover new sources or promote research candidates.
