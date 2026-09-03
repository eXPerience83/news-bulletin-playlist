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
| `ondacero` | ES | es | Onda Cero — Las noticias en Onda Cero | `implemented` | Re-checked 2026-09-04: the current official programme is already the granular hourly bulletin product; recent public episodes are normally within the common short-bulletin duration profile, so no replacement feed is currently justified. |
| `cnn` | US | es | CNN — 5 Cosas | `implemented` | Spanish-language international source currently available to the Spain playlist through explicit source membership. |

## Research inventory

The table below is the initial backlog captured from #53. A `candidate` label is intentionally not a
claim that the source is ready for production.

| Country | Lang | Bulletin/source | Status | Current research note |
| --- | --- | --- | --- | --- |
| ES | es | ABC — Las Noticias de ABC | `candidate` | Re-checked 2026-09-04: active daily podcast, stable Omny programme/RSS path and public Spotify show `0cLJl7pvrr1bkUaKiVRggf` are now identified; recent episodes are generally about 4–6 minutes. Keep as candidate until RSS identity/GUID behaviour and deterministic parser/matching support are verified and tested. |
| ES | es | COPE — boletines | `blocked` | Re-checked 2026-09-04: COPE still publishes frequent short boletines and the repository already has a provider parser, but no deterministic Spotify show identity was established from the public review. Keep blocked on destination-catalogue identity rather than guessing via global search. |
| DE | de | Deutschlandfunk — Die Nachrichten | `candidate` | Re-checked 2026-09-04: official RSS `https://www.deutschlandfunk.de/nachrichten-108.xml` and Spotify show `4eYPgoQH9VLTfgAxIbwHqs` are identified; Deutschlandfunk describes the podcast as round-the-clock news. Runtime parser, feed GUID/title-contract verification and deterministic matcher tests are still missing. |
| DE | de | Tagesschau in 100 Sekunden | `candidate` | Re-checked 2026-09-04: ARD/tagesschau exposes an official RSS and Spotify show `4QwUbrMJZ27DpjuYmN4Tun`; the programme publishes multiple very short updates per day. Runtime parser, feed identity/GUID verification and matching tests are still missing. |
| FR | fr | RTL — Le journal RTL | `candidate` | Re-checked 2026-09-04: hourly official programme, Audiomeans RSS `81fdd41c-51e9-4839-b867-7ad5bfe61dea` and Spotify show `6xlXRVwfN8ruLSLDoEfo0U` are identified. The feed mixes short ~3-minute editions with longer journals/promotional entries, so deterministic edition parsing plus common duration eligibility must be verified before promotion. |
| FR | fr | Europe 1 — Le journal | `candidate` | Re-checked 2026-09-04: Audiomeans RSS `44f6a116-20a6-44c4-98c4-b3e1bdd46dce` and Spotify show `1AUM0tB6DZBShd4nyzZHHE` are identified. Frequent half-hour/hour editions mix normal ~2–5-minute bulletins with 10–15-minute editions, requiring deterministic title/edition parsing and common duration policy. |
| PL | pl | RMF FM — Fakty | `candidate` | Re-checked 2026-09-04: official RSS `https://www.rmf24.pl/podcast/fakty/feed`, active Apple catalogue and official RMF Spotify distribution are confirmed. Most editions are ~2–5 minutes, with some combined/overnight entries around 11–13 minutes. Exact Spotify show identity and runtime parser/matcher verification remain before promotion. |
| PL | pl | TOK FM — Informacje | `candidate` | Re-checked 2026-09-04: official publisher material confirms hourly Spotify/Apple publication; Apple show `1797085838` is active with explicit `HH:00` titles. Most editions fit the short profile but 17–18-minute outliers occur. Exact Spotify show identity/feed contract and parser/matcher tests remain. |
| PL | pl | Radio ZET — Wiadomości | `blocked` | Re-checked 2026-09-04: the official player exposes hourly `Wiadomości` editions and a reusable podcast catalogue, but this review did not establish a deterministic Spotify show path. Treat destination catalogue identity as the current blocker rather than guessing. |
| IT | it | Sky TG24 — news bulletin feed | `candidate` | Multiple short editions per day; deterministic feed/catalogue verification pending. |
| PT | pt | Antena 1 — Noticiário | `candidate` | Many daily concise editions; verify identity and Spotify path. |
| BE | nl | VRT NWS update | `candidate` | Hourly, explicitly short-format candidate; verify collection/catalogue contract. |
| CH | de | SRF Nachrichten | `candidate` | Multiple short daily editions; verify deterministic destination path. |
| FI | fi | Yle Uutiset / Radiouutiset | `candidate` | Short radio-news editions found; Spotify catalogue path still needs verification. |
| SE | sv | Sveriges Radio — Ekot senaste nytt | `candidate` | Hourly short bulletin candidate; strong fit to verify. |
| DK | da | DR — Radioavisen | `candidate` | Hourly short bulletin candidate. |
| GB | en | Times Radio News Briefing | `candidate` | Re-checked 2026-09-04: stable Acast RSS `https://feeds.acast.com/public/shows/thetimesbriefing`, current publication three times per weekday plus weekend daily editions, and public Spotify episodes are confirmed. Recent editions are consistently about 3 minutes. Exact Spotify show identity plus parser/GUID/matcher verification remain. |
| GB | en | BBC Radio 4 — News Summary | `blocked` | Re-checked 2026-09-04: BBC Radio 4 continues broadcasting ~4-minute `News Summary` editions, but BBC Sounds exposes a short availability window and no stable Spotify show/catalogue identity was established. Keep blocked until a durable deterministic destination path exists. |
| AT | de | Ö1 — Nachrichten / Journale | `candidate` | Useful short editions appear to exist, but verify that the useful editions can be isolated deterministically. |
| IE | en | RTÉ news bulletins | `research` | Search for a better short/frequent candidate than long-form programmes. |
| NL | nl | NOS | `research` | Continue searching for a true short recurring bulletin rather than single-topic explainers. |
| RO | ro | Radio România Actualități — Știri | `candidate` | Hourly schedule appears promising; verify feed and Spotify catalogue. |

## Review record

### Spain / Spanish — 2026-09-04

Evidence reviewed:

- SER official podcast page still describes hourly updates and exposes RSS/Spotify distribution:
  <https://cadenaser.com/podcast/cadena-ser/1043/>.
- Spotify still exposes the deterministic SER show as
  <https://open.spotify.com/show/4EwwdoHHYmbt49UXODQMpi>.
- Spotify still exposes Noticias RNE as
  <https://open.spotify.com/show/0UgidTKsoaHiHDARuPQNW1>; current catalogue entries include both
  normal short bulletins and longer non-bulletin/outlier items, reinforcing the common eligibility
  policy rather than a source-wide duration bypass.
- Onda Cero's official bulletin page is actively publishing hourly editions:
  <https://www.ondacero.es/podcast/programas/boletines/>. Recent public Spotify episodes reviewed
  were typically around 3–6 minutes, so the existing implemented programme is already the granular
  source previously sought by the `implemented / review` note.
- ABC's podcast is active daily on Apple Podcasts
  (<https://podcasts.apple.com/es/podcast/las-noticias-de-abc/id1439939819>), has a stable Omny
  programme with RSS discovery
  (<https://omny.fm/shows/las-noticias-de-abc-1/playlists/podcast> and
  <https://omny.fm/shows/las-noticias-de-abc-1/playlists/podcast.rss>), and a public Spotify show
  (<https://open.spotify.com/show/0cLJl7pvrr1bkUaKiVRggf>). This removes the old feed/catalogue
  discovery uncertainty, but the repository still lacks an ABC parser and deterministic identity /
  matching tests, so promotion is intentionally deferred.
- COPE continues to expose short, frequent boletines on its official site
  (<https://www.cope.es/podcasts/audios>) and through Apple Podcasts
  (<https://podcasts.apple.com/es/podcast/boletines-cope/id1320143861>). The existing `CopeParser`
  remains useful, but the review did not establish a deterministic Spotify show identity. COPE
  therefore remains `blocked` rather than being promoted through an uncertain global-search guess.

Status changes from this review:

- `ondacero`: `implemented / review` -> `implemented`;
- ABC: remains `candidate`, but its next blocker is now parser/identity/matching verification rather
  than basic feed/Spotify discovery;
- COPE: remains `blocked` on deterministic Spotify catalogue identity;
- no new duration exception is justified by this review.

### France / French — 2026-09-04

Evidence reviewed:

- RTL's official `Le journal RTL` page says a new edition is published every hour and exposes current
  short editions around three minutes alongside longer journals. Apple/Spotify distribution remains
  active. The stable Audiomeans RSS is
  <https://feeds.audiomeans.fr/feed/81fdd41c-51e9-4839-b867-7ad5bfe61dea.xml> and the Spotify show
  is <https://open.spotify.com/show/6xlXRVwfN8ruLSLDoEfo0U>.
- `Le journal d'Europe 1` remains active with frequent half-hour/hour titles such as `Le journal
  20h30` and `Le journal 21h00`. The stable Audiomeans RSS is
  <https://feeds.audiomeans.fr/feed/44f6a116-20a6-44c4-98c4-b3e1bdd46dce.xml> and the Spotify show
  is <https://open.spotify.com/show/1AUM0tB6DZBShd4nyzZHHE>.
- Both programmes mix short bulletin editions with deliberately longer editions and occasional
  cross-promotion. This is not a reason for a provider-wide bypass: deterministic edition parsing
  plus the common 480-second eligibility policy is the intended boundary.
- The repository currently has no RTL or Europe 1 parser, so feed GUID/title semantics and
  deterministic source-to-Spotify matching are still untested.

Status changes from this review:

- RTL and Europe 1 remain `candidate`, but feed and Spotify discovery are no longer blockers;
- next work is parser/GUID/title/matching verification rather than broad source discovery;
- no duration exception is justified yet: long editions should remain excluded unless later product
  evidence identifies one specific recurring edition worth preserving.

### Germany / German — 2026-09-04

Evidence reviewed:

- Deutschlandfunk's official podcast directory exposes `Die Nachrichten` as round-the-clock news,
  with direct Spotify and RSS subscription links. The current identities are
  <https://www.deutschlandfunk.de/nachrichten-108.xml> and
  <https://open.spotify.com/show/4eYPgoQH9VLTfgAxIbwHqs>.
- `tagesschau in 100 Sekunden` has an official tagesschau/ARD podcast page describing multiple short
  updates per day, with direct RSS and Spotify links. The RSS is
  <https://www.tagesschau.de/tagesschau_in_100_sekunden/podcast-ts100-audio-100~podcast.xml> and the
  Spotify show is <https://open.spotify.com/show/4QwUbrMJZ27DpjuYmN4Tun>.
- Both are stronger candidates than the previous generic research note because destination identity
  is now deterministic at show level.
- The repository has no provider parser for either source, and raw feed GUID/title contracts plus
  source-edition to Spotify matching have not yet been tested in CI.

Status changes from this review:

- both remain `candidate` rather than being promoted prematurely to `verified`;
- the remaining verification surface is now parser/feed identity/matching behaviour, not discovery;
- no duration exception is indicated for tagesschau; Deutschlandfunk duration behaviour should be
  measured from the feed during parser verification before any exception is considered.

### Poland / Polish — 2026-09-04

Evidence reviewed:

- RMF officially exposes a `Fakty` podcast RSS at <https://www.rmf24.pl/podcast/fakty/feed> and its
  own podcast pages advertise Spotify and Apple distribution. Current editions are published
  repeatedly through the day, typically around 2–5 minutes, with some combined/overnight editions
  around 11–13 minutes.
- `Informacje Radia TOK FM` is an active Apple podcast (`1797085838`), and publisher material from
  Agora/Eurozet explicitly states that the news service is published to Spotify and Apple hourly.
  Current episode titles encode the edition time, while duration observations include normal
  2–6-minute editions and occasional ~17–18-minute outliers.
- Radio ZET's official player continues to expose `Wiadomości` every hour and a reusable podcast
  catalogue. A public RSS representation exists, but this review did not establish a stable Spotify
  show identity for that specific hourly news product.
- The repository has no RMF/TOK/Radio ZET provider parser.

Status changes from this review:

- RMF and TOK FM remain strong `candidate` entries; exact destination show identity plus
  parser/GUID/matching verification remain;
- Radio ZET changes from `candidate` to `blocked` because a deterministic Spotify destination path
  is currently missing;
- long RMF/TOK outliers reinforce the common duration policy; no scoped exception is justified by
  the current evidence.

### United Kingdom / English — 2026-09-04

Evidence reviewed:

- Times Radio News Briefing remains actively hosted by Acast at
  <https://shows.acast.com/thetimesbriefing>, with RSS
  <https://feeds.acast.com/public/shows/thetimesbriefing>. The programme currently publishes morning,
  afternoon and evening briefings on weekdays and daily weekend briefings; recent episodes are
  consistently around three minutes. Individual episodes are present on Spotify.
- Public search did not establish the stable Spotify **show** id for Times Radio News Briefing, so
  the destination identity step is not yet complete even though individual Spotify episodes are
  clearly present.
- BBC Radio 4 continues to schedule `News Summary` editions of about four minutes, and BBC Sounds
  exposes them as individual programmes. Current BBC Sounds entries have a short availability
  window, and this review did not establish a dedicated stable Spotify show/catalogue path.
- The repository has no Times Radio or BBC News Summary parser.

Status changes from this review:

- Times Radio remains `candidate`; feed/cadence/duration are strong, but show-level Spotify identity
  and deterministic matching remain;
- BBC Radio 4 News Summary changes from `candidate` to `blocked` pending a durable destination
  catalogue path suitable for a 48-hour Spotify-managed playlist;
- no duration exception is needed for either short-format product.

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
