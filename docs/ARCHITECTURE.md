# Architecture

This document defines the architectural invariants of News Bulletin Playlist. These constraints are intentional and should be preserved as the project evolves.

## Product shape

News Bulletin Playlist is **one engine that manages many playlists**.

Spain / Spanish-language news is the first implementation and validation target, not a permanent architectural boundary. The same runtime must be able to manage playlists for multiple countries and languages without duplicating the application or running one complete collector per playlist.

Planned expansion includes English, French, German and Polish, followed by playlists for the main European countries and potentially cross-country / pan-European playlists.

## Core invariant

> **Fetch once -> normalize once -> store once -> distribute to many playlists.**

A provider/source is independent from a playlist. A source can feed zero, one or many playlists. A playlist selects sources or normalized editions according to configuration and policy.

The engine must not fetch or parse the same source independently for every playlist that uses it.

## Main domains

### Source / provider

A source describes where bulletin metadata comes from and how it is interpreted.

Source configuration and metadata should be capable of representing at least:

- stable source/provider identifier;
- provider/display name;
- feed or discovery endpoint;
- country or countries;
- language or languages;
- bulletin/category type;
- parser / normalization rules;
- source-specific timezone when required;
- enabled/disabled state;
- priority or quality metadata when required.

Country and language are separate dimensions. For example, an English-language playlist may combine sources from several countries, while a UK playlist may apply a country-specific selection.

### Canonical edition

Provider-specific metadata is converted to a provider-independent canonical representation before playlist selection.

Playlist logic must not depend directly on provider-specific title formats, RSS quirks or parsing rules.

### Playlist definition

A playlist is configuration/policy, not its own collector.

Each playlist may independently define:

- destination playlist identity;
- display name and description;
- source selection and/or selectors;
- countries;
- languages;
- retention window;
- maximum number of episodes;
- ordering policy;
- inclusion/exclusion rules.

The initial defaults remain 48 hours of playlist retention, a maximum of 100 episodes and 30 days of internal operational metadata unless a playlist explicitly overrides them.

## Editorial independence and monetization invariant

Playlist membership and ordering must be determined by documented editorial/source-selection rules, freshness and playlist policy — **never by payment or other compensation**.

The engine must not implement or expose:

- paid placement in a Spotify playlist;
- sponsor-funded inclusion or guaranteed inclusion;
- paid boosts, paid priority or compensation-dependent ordering;
- rules where donations, sponsorships or payments influence the name or content of a Spotify playlist.

This is an architectural constraint, not merely a launch-policy choice. Spotify states that accepting or offering compensation to influence a user playlist or its content is not permitted.

Possible future monetization of the **software or a separate non-streaming service** is a different concern and must remain technically and conceptually separate from playlist composition. It may only be considered if the deployment's Spotify access mode and the then-current Spotify Developer Policy permit it.

As of 2026-08-27, Spotify Development Mode is intended for learning, experimentation and personal **non-commercial** projects and must not be relied on as the foundation for building or scaling a business. Therefore the current Development Mode deployment is treated as non-commercial.

Relevant Spotify references:

- https://developer.spotify.com/blog/2026-02-06-update-on-developer-access-and-platform-security
- https://developer.spotify.com/policy
- https://developer.spotify.com/documentation/web-api/concepts/quota-modes
- https://artists.spotify.com/en/blog/behind-the-playlists-your-questions-answered-by-our-playlist-editors

Before any future commercial launch, these constraints must be re-verified against the current Spotify terms and access model. Commercialization must not require changing the editorial-independence invariant above.

## Engine cycle

A normal engine run should conceptually perform these stages:

1. Load global configuration, source registry and playlist definitions.
2. Determine the union of sources needed by all enabled playlists.
3. Fetch each required source once.
4. Parse and normalize editions once.
5. Persist canonical operational state once.
6. Evaluate every enabled playlist against the canonical data.
7. Reconcile each destination playlist independently.
8. Apply playlist-specific ordering, retention and limits.
9. Record source-level and playlist-level results/errors.

This may later use concurrency internally, but concurrency must not change the single-engine / shared-fetch model.

## Failure isolation

A failing source must not prevent unrelated sources from being processed.

A failing destination playlist must not prevent other playlists from being reconciled during the same engine run.

Errors must therefore be attributable at least to source/provider and playlist where applicable.

## Idempotency

Running the engine repeatedly with unchanged upstream data should converge on the same playlist state and must not create duplicates or unnecessary churn.

## Configuration over duplication

Adding a new country, language or playlist should normally require:

1. adding/reusing source definitions;
2. adding a playlist definition or selector;
3. adding parser/provider code only where a genuinely new source format requires it.

It should **not** require copying the application, forking the engine, or creating a country-specific runtime implementation.

## P1.1 domain and configuration contract

Schema version 1 defines four separate concepts:

- `SourceDefinition`: a global source/provider definition with stable ID, parser, endpoint,
  country/language metadata and optional external catalogue references;
- `CanonicalEdition`: one source-native asset identified by `(source_id, source_native_id)`, with
  its source title plus UTC `published_at` and optional UTC `edition_at` metadata;
- `PlaylistDefinition`: editorial metadata, an explicit source selection and per-playlist policy;
- `DestinationReference`: the adapter and writable external destination for a playlist.

An external catalogue reference, such as a Spotify show ID, is not a destination. The source
parser ID is likewise separate from the destination adapter ID.

The YAML schema is represented by the non-production example in
[`config/news-bulletin-playlist.example.yaml`](../config/news-bulletin-playlist.example.yaml).
The following is a deliberately minimal, self-consistent excerpt:

```yaml
schema_version: 1
sources:
  - id: cnn
    display_name: CNN 5 Cosas
    countries: [US]
    languages: [es]
    timezone: America/New_York
    enabled: true
    parser_id: cnn
    endpoint_url: https://feeds.megaphone.fm/WMHY5696831164

playlists:
  - id: spain_spanish_news
    display_name: Spain Spanish News
    description: Core Spanish-language news bulletins
    languages: [es]
    countries: [ES]
    enabled: true
    source_selection:
      explicit: [cnn]
    destination:
      adapter_id: spotify
      external_id: replace-with-provisioned-playlist-id
    retention_hours: 48
    max_episodes: 100
    ordering: edition_at_desc
```

`source_selection.explicit` is authoritative in schema version 1. Playlist countries and languages
are independent editorial metadata and do not implicitly filter explicitly selected sources.
Selectors may later evaluate source dimensions, but selectors are deliberately not part of P1.1.

The default playlist policy is 48 retention hours, 100 episodes and descending semantic bulletin
time (`edition_at`), falling back to RSS `published_at` only when no reliable edition timestamp exists. `published_at_desc` remains available as an explicit legacy ordering policy. `CORE_PROVIDERS` remains temporarily available to the P0 provider watch and Spotify probes;
new P1 code resolves parsers independently and does not consume that legacy tuple.

## P1.2 shared collection contract

The collection stage implements the first half of the core invariant without introducing playlist-specific collectors:

- required sources are the union of explicit source selections across all enabled playlists;
- each required enabled source is fetched and parsed at most once per collection cycle;
- runtime collection uses the configured source endpoint, including Onda Cero's real RSS feed; watchdog-only webpage fallbacks are not production metadata authorities;
- RSS/provider identity is preserved independently from title and timestamps. Native identity prefers `guid`/`id`, then the enclosure asset URL, then an editorial link when no stronger source-native identifier exists;
- title/date/hour are never used to synthesize identity, so distinct native assets with identical visible metadata remain distinct;
- RSS publication metadata produces `published_at`, while the provider title parser produces the editorial `edition_at`; the configured `SourceDefinition.timezone` is authoritative for interpreting that local editorial wall clock before the canonical model normalizes it to UTC;
- collection returns an explicit success/failure result for each source. A malformed feed, failed fetch, missing required endpoint, or feed with no valid canonical bulletin editions is a source failure for that cycle rather than an invented empty-success state;
- one source failure does not prevent unrelated sources from being collected;
- RSS response bodies are bounded to 10 MiB before parsing so an unhealthy or misconfigured endpoint cannot consume unbounded process memory.

P1.2 deliberately does not persist state, perform Spotify catalogue matching, apply playlist retention/order policy, or reconcile destinations. Those responsibilities belong to later P1 workstreams.

## P1.3 persistence contract

Operational state is stored once in SQLite under `/data`, independent from any one playlist:

- canonical editions are keyed globally by `(source_id, source_native_id)` and are never duplicated per playlist;
- schema upgrades use a small explicit, repeatable migration mechanism rather than an ORM;
- source run history, latest-attempt state and last-known-good timestamps are persisted separately;
- Spotify matching state persists `matched`, `pending` and `ambiguous` outcomes plus diagnostics and the durable source-identity to Spotify episode URI mapping;
- playlist run history and latest playlist state are persisted without owning copies of canonical editions;
- logical writes are transactional and persistence failures surface as `PersistenceError` rather than being interpreted as an empty fresh state;
- state updates are monotonic, so replaying older collection/matching/reconciliation work cannot regress newer canonical metadata, mappings, source status or playlist status;
- run history is eligible for deterministic 30-day pruning;
- canonical editions older than the retention boundary may be removed only when the caller explicitly supplies the identities that still need protection for playlist correctness. If that protection boundary is unknown, canonical state is retained by default;
- deleting an eligible canonical edition removes its dependent Spotify mapping transactionally through the database relationship.

The runtime initializes and migrates the database automatically under its persistent data directory on container start. P1.3 does not itself decide which collected editions to match or which playlist items to write.

## P1.4 deterministic Spotify matching contract

Matching maps already-persisted canonical source identities to Spotify episode URIs without changing source identity or publication metadata:

- each source resolves exactly one configured Spotify `show` external reference; enabled-source matching never falls back to global Spotify search;
- persisted successful mappings are reused before any catalogue request;
- recent `PENDING` or `AMBIGUOUS` outcomes are reused for a 15-minute retry grace to avoid repeated catalogue churn; a future persisted timestamp is also retained until the matcher clock catches up so P1.3 monotonicity cannot be violated;
- unresolved editions are matched in a source batch, so Spotify catalogue pages are fetched once and reused across all editions for that source rather than once per bulletin;
- catalogue traversal is deliberately bounded to two pages of at most 50 episodes each: 100 candidates maximum per source matching pass;
- matching is scoped to the known show and reuses the source's existing title parser to convert Spotify episode titles to the same editorial `edition_at` representation used during collection;
- the configured source timezone remains authoritative when interpreting the parsed Spotify title wall clock;
- Spotify release-date precision must be compatible with the canonical edition date before a candidate is viable;
- when canonical `edition_at` is unavailable, only normalized exact-title equality is accepted as the title signal;
- duration may be recorded in diagnostics but never decides a match;
- one viable candidate produces `MATCHED`; more than one produces `AMBIGUOUS`; none produces `PENDING`. A Spotify API/transport failure is not rewritten as `PENDING` and therefore cannot masquerade as a valid empty catalogue result;
- all three match outcomes and useful diagnostics are persisted through P1.3;
- distinct RNE source-native identities remain distinct even when Spotify represents them with the same episode URI. Destination-level URI deduplication, if required, belongs to playlist desired-state construction in P1.5.

P1.4 does not write playlists, choose playlist retention/order policy, schedule engine runs or own the production OAuth lifecycle.

## Deployment invariant

One deployed engine/container should be capable of updating multiple configured playlists in one scheduled execution. We should not require one Docker container, cron job or GitHub Actions workflow per playlist merely because the number of playlists grows.

Separate runtimes may still be supported later for scaling or isolation, but they are an operational choice, not the application model.

## First implementation vs. long-term architecture

The first release may contain only Spanish providers and one Spanish/Spain-oriented Spotify playlist. That is acceptable as an incremental delivery choice.

What is not acceptable is introducing assumptions into the shared domain model, persistence layer, scheduler or reconciliation engine that make `Spain`, `Spanish`, or `one playlist` mandatory concepts.

When implementation convenience conflicts with this invariant, prefer the smallest implementation that preserves the multi-playlist, multi-country, multi-language architecture.


## Bulletin duration eligibility

Duration is a common playlist eligibility policy, never provider-parser behavior. The
default destination-side ceiling is 480 seconds. A longer bulletin is eligible only
when a version-controlled, bounded exception matches its source and semantic local
edition time. The initial exception is `ser_morning_0800`: SER 08:00 in the source
timezone may run up to 1800 seconds. The exception is not a source-wide bypass.

Final eligibility uses the matched destination episode duration (Spotify `duration_ms`
converted to seconds). RSS/source duration remains diagnostic metadata only. If a
matched destination duration is unavailable, desired-state construction fails closed
and reconciliation preserves the existing destination rather than treating unknown
metadata as an empty authoritative state. Non-default duration decisions are emitted
to the bounded diagnostics event stream so exclusions and scoped exceptions are visible.

To add a justified recurring exception, add one stable unique rule to the built-in
playlist template with an existing `source_id`, exact `edition_local_time` in that
source's timezone, and a finite `max_seconds` greater than the default. Do not add
source-wide unlimited bypasses or provider-specific filtering.
