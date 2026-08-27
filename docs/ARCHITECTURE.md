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
- inclusion/exclusion rules;
- optional future sponsorship/promotion rules.

The initial defaults remain 48 hours of playlist retention, a maximum of 100 episodes and 30 days of internal operational metadata unless a playlist explicitly overrides them.

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

## Intended configuration direction

The exact schema is not frozen yet, but the architecture should support explicit source lists and selector-based playlists, for example:

```yaml
playlists:
  - id: spain
    languages: [es]
    countries: [ES]
    retention_hours: 48
    max_episodes: 100
    sources:
      - rne_24h
      - cadena_ser
      - cope
      - onda_cero

  - id: english
    languages: [en]
    retention_hours: 48
    max_episodes: 100
    selector:
      category: news_bulletin
      enabled: true
```

The final schema may change, but the domain separation above must remain.

## Deployment invariant

One deployed engine/container should be capable of updating multiple configured playlists in one scheduled execution. We should not require one Docker container, cron job or GitHub Actions workflow per playlist merely because the number of playlists grows.

Separate runtimes may still be supported later for scaling or isolation, but they are an operational choice, not the application model.

## First implementation vs. long-term architecture

The first release may contain only Spanish providers and one Spanish/Spain-oriented Spotify playlist. That is acceptable as an incremental delivery choice.

What is not acceptable is introducing assumptions into the shared domain model, persistence layer, scheduler or reconciliation engine that make `Spain`, `Spanish`, or `one playlist` mandatory concepts.

When implementation convenience conflicts with this invariant, prefer the smallest implementation that preserves the multi-playlist, multi-country, multi-language architecture.
