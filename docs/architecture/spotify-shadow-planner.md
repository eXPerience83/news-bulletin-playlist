# Spotify shadow planner

Issue #158 introduced the durable local planner and its bounded
`spotify_planner_shadow_decision` event. Phase 3 consumes its catalogue and retry evidence to
suppress unnecessary show-catalogue reads while retaining the existing OAuth/client lifecycle and
reconciling every destination. Destination skip decisions remain shadow-only.

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
the planner and are not duplicated. Catalogue audit freshness records a successful structurally
valid bounded inspection, so complete, truncated, and empty valid responses renew its six-hour
deadline. This is separate from absence proof: only a complete, valid, non-empty result from the
current cycle may prove a mapping absent. Completeness is not persisted. Audit timestamps and
clean/unknown/confirmed relationships are validated when loaded; a syntactically valid but
contradictory row fails closed. Local RSS cycles do not move audit deadlines.

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

## Phase 3 catalogue execution

Matching now consumes the durable input and unresolved-retry evidence. Reusable `MATCHED`
mappings with unchanged semantic input and a fresh catalogue audit require no show-catalogue
request. New or changed inputs, due adaptive retries, due audits, and untrusted planner state
trigger a bounded fetch. Retry intervals are 15 minutes below one hour of age, 30 minutes from one
to three hours, one hour from three to six hours, and six hours thereafter. Only an actual local
evaluation advances the durable attempt deadline.

A cycle-scoped in-memory coordinator shares each bounded fetch by show, authorization context,
market mode, pagination contract, and catalogue contract version. Parser, timezone, and release
delay remain source-specific evaluation inputs rather than HTTP cache dimensions. Failed fetches
are coalesced within the cycle. Candidates and completeness claims are never persisted.

Playlist reconciliation still runs every cycle. Only after pending-snapshot handling and the
existing unchanged/stability checks fail to avoid mutation does a pre-write barrier validate each
selected mapping. Presence in a truncated result is positive evidence; absence in a truncated or
empty result blocks the write. Complete non-empty absence permits invalidation and rematching. A
changed desired state re-enters reconciliation from the beginning, and repeated fingerprints or
an exhausted finite iteration bound fail closed. Phase 3 does not implement global lazy OAuth,
destination local skips, or snapshot-first audits.

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
