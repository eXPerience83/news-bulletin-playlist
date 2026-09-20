# Architecture

## Current status

The authoritative runtime is **Spotify-first**.

Runtime playlist decisions do not depend on RSS collection, RSS-native episode identity,
RSS-to-Spotify matching or per-edition `PENDING` / `AMBIGUOUS` matching retries.

The current authority split is:

- the unauthenticated public Spotify surface is a bounded **change detector only**;
- the official Spotify Web API show catalogue is the **episode metadata authority**;
- Spotify episode ID/URI is the **canonical runtime episode identity**;
- durable local Spotify-first state is the authority for unchanged sources between catalogue
  refreshes;
- the existing reconciliation/readback machinery is the authority for determining whether a
  destination mutation safely applied.

Released legacy SQLite tables are intentionally not dropped merely because the current runtime no
longer reads them. They may remain inert so upgrades from released schemas stay boring and safe.

Historical P0/P1 and shadow-planner documents describe earlier implementation stages. They are useful
for provenance, but they are not the current runtime contract when they conflict with this document.

## Core invariants

1. One process can maintain multiple playlists, countries and languages.
2. A source is global and may be selected by several playlists.
3. A normal source change refreshes **only that source's** official Spotify catalogue.
4. Unchanged sources reuse persisted local episode state.
5. Playlist retention, duration eligibility, ordering and source selection are computed locally.
6. Spotify episode ID/URI is identity; title/timestamps are metadata.
7. Detector failure, catalogue failure or incomplete evidence must not be converted into an
   authoritative empty playlist.
8. Deterministic stale/unavailable-item evidence may escalate catalogue refreshes; ambiguous writes
   may not.
9. Mutating transport/5xx failures remain ambiguous until readback/snapshot evidence resolves them.
10. HTTP 429 uses one shared durable Spotify cooldown. No component may bypass it with ad-hoc retry.
11. A failing source or destination must not unnecessarily block independent sources/playlists.
12. Audio is never downloaded or stored.

## Runtime topology

The supported deployment is one long-lived Python 3.14 process.

It owns:

- the read-only status/health surface;
- the protected administration and OAuth callback surface;
- one sequential engine scheduler;
- the local SQLite state under `/data`;
- managed installation state under `/data/managed-state.json`.

The default engine interval is approximately ten minutes. GitHub Actions is not the production
playlist scheduler.

The runtime may be unconfigured: with no active managed playlist it remains healthy and does not
invent provider work.

## Configuration model

Application-owned source/catalog knowledge is separate from installation-owned playlist choices.

The built-in source catalog defines, for each supported source:

- stable source ID;
- country/language/editorial scope;
- timezone;
- title parser strategy;
- optional collection/content filter;
- exactly one Spotify `show` external reference.

A normal Spotify-first source has no runtime RSS endpoint requirement.

Managed state stores installation choices such as:

- active/paused playlist state;
- Spotify destination ID;
- playlist metadata and cover choices;
- explicit source membership;
- per-playlist duration ceiling.

The protected `/admin/` UI is the normal configuration path.

Schema-v1 YAML remains an advanced/manual compatibility path. It uses the same Spotify-first source
model and does not require `endpoint_url`. If managed state and the default YAML both exist,
startup fails closed rather than guessing which configuration is authoritative.

## Source selection and catalogue promotion

Playlist country/language metadata does not implicitly select sources. Explicit source membership is
authoritative for the current schema.

A research candidate is promoted to the built-in runtime catalog only after the project has
sufficient evidence for:

- one stable Spotify show identity;
- suitable bulletin/news-summary editorial scope and cadence;
- parser behavior on current Spotify episode titles;
- any required collection/content filter;
- representative duration behavior;
- source timezone/language/country classification;
- failure behavior compatible with the runtime safety model.

A predominantly long-form show is not promoted merely because some episodes fit below a playlist
duration ceiling.

## Public detector

The public detector exists to reduce authenticated Web API work.

For each source it observes bounded latest-episode evidence from the public Spotify surface and
persists detector state. It does **not** scrape or reconstruct a historical episode catalogue.

A detector identity transition is a signal that the source's official catalogue is due for refresh.

The detector can skip intermediate publications. That is safe because a subsequent official
catalogue refresh ingests all relevant episodes needed to cover the retention window rather than
only the episode seen by the detector.

Detector failures preserve last-known-good episode state and never authorize destructive playlist
changes.

## Official Spotify catalogue refresh

The official Spotify Web API show catalogue is the episode authority.

A source catalogue refresh can occur for these reasons:

- **bootstrap** — no usable Spotify-first source state exists yet;
- **change** — the public detector observed a new latest episode identity;
- **inactivity audit** — no detector transition/catalogue refresh has occurred for the configured
  inactivity threshold;
- **stale escalation** — deterministic destination evidence requires refreshing other relevant
  sources before retrying a desired state.

The default inactivity threshold is 24 hours and is configurable through the runtime environment.

A normal detector change must not fan out into reads of every source catalogue. The engine refreshes
the changed source, persists its updated relevant catalogue and recomputes complete playlists from
that source plus unchanged local source state.

Catalogue traversal is bounded and must prove coverage of the configured retention window before
old local episode rows are treated as absent. If safe coverage cannot be established, the runtime
preserves prior state rather than deleting unseen items as if absence had been proven.

Request/page counts are diagnostic/accounting data and must remain bounded.

## Spotify-first episode model

The durable episode model stores:

- `source_id`;
- Spotify show ID;
- Spotify episode ID;
- Spotify episode URI;
- original title;
- parsed `edition_at` when exact semantic time can be established;
- explicit timestamp provenance;
- Spotify release-date value and precision;
- duration in seconds when available;
- local `first_seen_at`;
- local `last_seen_at`.

Spotify episode ID/URI is canonical identity.

Two episodes may have similar or identical visible titles/timestamps and still remain distinct if
Spotify gives them distinct episode IDs.

Persisted episode URI and ID must agree with the shared Spotify episode-URI invariant before reuse.

## Timestamp policy

Title parsing is preferred when a source parser can recover an exact editorial date/time.

When exact title parsing succeeds:

- normalize the resulting instant to UTC;
- store it as `edition_at`;
- record timestamp provenance as `title`.

When exact parsing is unavailable:

- preserve the original Spotify release-date value and precision;
- preserve local first-seen time;
- record explicit fallback provenance;
- never invent minute precision.

Public Embed/oEmbed release dates are not treated as a universal publication timestamp.

The only supported playlist ordering contract is semantic newest-first
(`edition_at_desc`). Episodes with exact `edition_at` use it; fallback episodes use the
Spotify-first ordering helper built from release-date precision plus local first-seen evidence.

## Collection/content filters

Source-specific parser and collection filters remain supported where they are semantically useful
against Spotify catalogue metadata.

These are not remnants of RSS matching. They are part of selecting the intended bulletin product
from a Spotify show when a provider mixes several products or title forms.

A filter/parser failure must be observable and bounded. It must not silently convert uncertain
input into an authoritative empty source state.

## Spotify-first persistence

`SpotifyFirstJournal` owns current authoritative episode/source/destination evidence.

Its current tables hold:

- Spotify-first episode state;
- per-source last refresh/reason/coverage evidence;
- per-playlist destination verification fingerprints.

Source catalogue replacement is conservative:

- episodes are upserted by Spotify episode identity;
- unseen local episodes are removed only when catalogue coverage has been proven;
- stale writes cannot regress newer `first_seen_at` / `last_seen_at` metadata.

Operational source/playlist run history, playlist attestations, pending snapshot confirmation,
rate-limit cooldown and diagnostics continue to use the shared SQLite database.

Destination skip authority has two deliberately separate levels:

- exact verification uses the normal destination-audit lease (six hours by default);
- a safely attested unavailable-media/opaque-slot result may use a shorter degraded lease
  (30 minutes by default).

The degraded lease never turns partial content into exact evidence. It is valid only for the same
destination and desired fingerprint while the matching durable playlist attestation still exists
and no pending snapshot transition is active. Expiry, desired/destination change or missing
attestation forces conservative remote reconciliation again.

### Legacy database compatibility

Released migrations created tables for canonical RSS editions, Spotify matching and the old planner.

Those schema migrations remain so an existing database can upgrade without destructive surgery.
Current runtime code does not use those rows to decide source refreshes, desired playlists or
destination writes.

Legacy diagnostic keys likewise remain allowlisted so already-persisted diagnostic history can be
rendered/exported after upgrade. That compatibility surface must not be mistaken for an active
legacy runtime path.

## Desired playlist state

Each enabled playlist is computed from persisted Spotify-first episodes for its explicit selected
sources.

The normal policy is:

- retain recent content for 48 hours;
- cap the destination at 100 episodes;
- apply the playlist's duration ceiling (currently 1800 seconds by default);
- apply any explicit bounded duration exception;
- sort newest-first under the semantic Spotify-first ordering policy;
- deduplicate identical Spotify episode URIs at destination level.

Duration is destination eligibility, not episode identity.

If Spotify duration is unavailable, that episode is excluded and counted as unavailable; it is not
treated as proof that the whole source or playlist is empty.

A predominantly long-form source still belongs outside the default catalog even if individual
episodes sometimes pass the duration filter.

## Destination reconciliation

Reconciliation compares the complete desired URI sequence with the Spotify destination.

Unchanged desired state must cause zero writes.

When a write is required, existing safety machinery remains authoritative:

- pre-write destination reads are bounded and validated;
- playlist mutations are serialized through the configured mutation guard;
- exact readback verifies the resulting URI order;
- snapshot attestations/fingerprints protect fast paths;
- pending snapshot confirmation handles propagation uncertainty;
- malformed/truncated/contradictory destination responses fail closed.

A destination failure must not block reconciliation of independent playlists.

## Stale-item escalation

A normal write failure is **not** evidence that every source is stale.

Full relevant-source refresh is exceptional and requires deterministic evidence that the desired
state contains an unavailable/stale episode or equivalent safe catalogue evidence.

On such evidence the engine may:

1. refresh the other relevant source catalogues for the affected playlist;
2. rebuild local retained/filter/order state;
3. retry reconciliation under the same write guards.

Ambiguous mutating failures do not enter this path until readback/snapshot evidence establishes what
actually happened.

## Rate limiting and request accounting

Spotify Web API 429 responses activate one runtime-wide durable cooldown using `Retry-After` when
valid, otherwise the conservative fallback deadline.

While the cooldown is active:

- new token/provider work that would reach Spotify is suppressed locally;
- later same-cycle Spotify requests are suppressed;
- existing destination/local state is preserved;
- no component starts its own bypass retry loop.

Request accounting distinguishes unauthenticated public Embed/oEmbed requests, authenticated Spotify Web API requests and OAuth token requests from locally suppressed work.

## Failure isolation and fail-closed behavior

The runtime prefers preserving known-good state over destructive guesses.

Examples:

- detector failure -> preserve local episodes;
- incomplete catalogue coverage -> do not delete unseen local episodes;
- unavailable duration -> exclude/count the item, do not invent a duration;
- malformed Spotify response -> fail that operation, do not interpret it as an empty catalogue;
- ambiguous mutation -> read back before retry;
- persistence failure -> surface failure, do not substitute empty state;
- one source/destination failure -> continue independent work where safe.

## Diagnostics and secrets

Diagnostics are structured, bounded and allowlisted.

Never expose or persist:

- access/refresh tokens;
- Authorization headers;
- authorization codes;
- PKCE verifiers;
- admin passwords;
- raw provider response bodies containing unknown material;
- unbounded URI/title collections.

Current diagnostics should expose enough source/playlist/request/refresh reason evidence to explain
why a catalogue was or was not refreshed and why a destination was written, preserved or skipped.

Legacy RSS/matching/planner diagnostic fields are retained only for already-persisted history from
released versions.

## Editorial independence

Playlist membership and ordering are determined by documented source/editorial rules, freshness and
playlist policy, never by payment, sponsorship or other compensation.

Paid placement, guaranteed paid inclusion and compensation-dependent ordering are outside the
architecture.

## Deployment invariant

One deployed engine/container should normally update all configured playlists.

Adding a country/language/playlist should require catalog/configuration work, not a copied
application, country-specific engine or one scheduler/container per playlist.

Separate runtimes may later be used for scaling or isolation, but that is an operational choice,
not the application model.

## Historical implementation documents

The following may intentionally describe superseded paths and should be read as historical evidence
unless explicitly updated to say otherwise:

- `docs/P0_FINDINGS.md`;
- `docs/P1_5_RECONCILIATION.md`;
- `docs/architecture/spotify-shadow-planner.md`;
- investigation notes under `docs/investigations/`.

The current runtime contract is this document plus the root `AGENTS.md` and tests.
