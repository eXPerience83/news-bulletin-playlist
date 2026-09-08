# Spotify shadow planner

Issue #158 introduces a durable local planner without using its decisions to suppress Spotify
work. The engine still obtains OAuth, matches every source, and reconciles every destination in
the same order as before. The planner runs alongside that flow and emits one bounded
`spotify_planner_shadow_decision` event per completed cycle.

## Durable evidence

Schema migration 5 adds four focused tables:

- `spotify_planner_catalogue_state` stores one six-hour audit deadline per hashed source/show,
  authorization-generation, market and pagination context;
- `spotify_planner_match_inputs` stores the canonical semantic input fingerprint last covered by
  qualifying matching evidence;
- `spotify_planner_unresolved_state` stores per-edition `PENDING`/`AMBIGUOUS` retry timing and a
  bounded reason;
- `spotify_planner_destination_state` stores configuration, destination and auth-contract
  fingerprints, the observed and confirmed desired fingerprints, dirty/unknown flags, and the
  six-hour destination audit deadline.

Pending snapshot transitions remain authoritative in the existing #134 journal. They are read by
the planner and are not duplicated. Catalogue and destination evidence is renewed only after a
qualifying real verification. Audit timestamps and clean/unknown/confirmed relationships are
validated when loaded; a syntactically valid but contradictory row fails closed. Local RSS cycles
do not move audit deadlines. Empty or truncated catalogues do not qualify.

## Fingerprint contracts

All fingerprints use canonical JSON with sorted keys, compact separators and SHA-256. Their
payloads are:

- edition input: source ID, source-native identity, title, publication time, semantic edition
  time and provider duration;
- source context: source ID, parser, timezone, Spotify release-delay policy and sorted external
  catalogue references, combined with authorization generation, market mode and bounded
  pagination contract;
- playlist configuration: logical ID, enabled state, ordered source selection, retention, item
  limit, ordering and duration policy;
- destination context: adapter and destination identity;
- desired state: the ordered Spotify URI sequence.

RSS observation time (`last_seen_at`) is deliberately absent. Raw destination/show IDs and URI
lists are represented in planner tables only through their fingerprints. Edition identity remains
the existing local durable key and does not add provider payload data.

The authorization-context fingerprint derives from the durable, non-secret `authorized_at`
generation and market mode. A normal token refresh preserves that generation. Every new OAuth
authorization changes it conservatively, even when the operator reconnects the same account. The
default market mode records that availability comes from the user-token account; future explicit
markets form different contexts. No token, refresh token, client ID or other credential enters a
fingerprint, and reading this context performs no OAuth refresh or Web API request.

A cached URI's presence in a complete catalogue proves existence, but does not by itself certify a
changed semantic edition input. The matcher performs an additional pure evaluation over the
catalogue it already fetched and publishes shadow input evidence only when the current input
resolves uniquely to the same persisted mapping. This metadata changes neither the production
match nor provider request sequence. A missing unresolved-retry row is reported as missing
evidence; input-changed is reserved for a present row with a different fingerprint.

## Safety behavior

Missing, corrupt or incompatible evidence requires hypothetical Spotify work and cannot authorize
a local skip. A desired transition immediately clears its old confirmed fingerprint. An unknown
mutation also clears confirmation, so `A -> B -> unknown -> A` cannot revive evidence for the
first A. Confirmation is invalidated before every current reconciliation and restored only when
the exact desired, destination, configuration and authorization context observed for that
operation still matches the durable row. Mutation outcome and skip-quality remote evidence are
separate: an exact readback may prove that an ambiguous write applied while a later unstable
snapshot bracket still prevents planner confirmation. Pending/degraded verification likewise does
not qualify. A crash during a possibly mutating call leaves conservative durable state.
Clock rollback makes audits due and blocks unresolved retry eligibility without moving a retry
earlier.

Planner persistence and evaluation are fail-closed for a future skip, but fail-soft for this
shadow integration. A planner failure produces `state_available=false`, `would_need_spotify=true`
and `would_allow_local_skip=false`; the existing provider flow continues unchanged.
When a later phase activates planner decisions, successful persistence of the pre-reconciliation
invalidation must become a prerequisite for any remote mutation; #158 deliberately does not add
that behavior-changing gate.
