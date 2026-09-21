# Spotify shadow planner

> **Historical / superseded.** This document records the pre-0.1 RSS/matching planner design.
> The authoritative runtime is now Spotify-first; see `docs/ARCHITECTURE.md`. The planner/matching
> tables described below existed only in the private-development database and are not present in
> database format 1. The 0.1.0 runtime does not read or migrate them.


Issue #158 introduced the durable local planner and its bounded
`spotify_planner_shadow_decision` event. Phase 3 consumes its catalogue and retry evidence to
suppress unnecessary show-catalogue reads. Phase 4 uses the same evidence for operational
destination decisions: after RSS and local persistence, a completely clean cycle returns before
OAuth token acquisition, client construction, or Web API access. If shared matching work exists,
the engine acquires one provider session, rebuilds desired state, and still reconciles only the
destinations whose own evidence requires remote work.

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

Schema migration 6 adds `spotify_planner_matched_recovery_state`, a non-authoritative six-hour
retry deadline for already-`MATCHED` editions whose semantic planner evidence could not be
recovered from a bounded catalogue. It never substitutes for `spotify_planner_match_inputs` and
cannot authorize soft retention, destination skips, or writes by itself.

Schema is at version 6. Durable destination confirmation is historical evidence, but it is
not sufficient by itself to authorize a local skip. The process owns an exact-context runtime
lease for each playlist, measured with a monotonic clock. A restart deliberately loses all leases,
so each destination receives one qualifying remote verification before that process may skip it.

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

- matching input: the edition's source ID, source-native identity, title, publication time,
  semantic edition time and provider duration, bound to the source evaluation contract fingerprint;
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

Playlist reconciliation runs only for destinations that are dirty, unknown, pending, due for a
six-hour audit, or missing compatible evidence. Only after pending-snapshot handling and the
existing unchanged/stability checks fail to avoid mutation does a pre-write barrier validate each
selected mapping. Presence in a truncated result is positive evidence; absence in a truncated or
empty result blocks the write. Complete non-empty absence permits invalidation and rematching. A
changed desired state re-enters reconciliation from the beginning, and repeated fingerprints or
an exhausted finite iteration bound fail closed. Phase 3 does not implement global lazy OAuth,
destination local skips, or snapshot-first audits.

Missing Phase-2 match-input evidence is recovery work rather than permission to discard the
authoritative persisted mapping. Empty and truncated inspections preserve a valid `MATCHED` URI;
absence from a truncated catalogue is inconclusive and does not clear previously qualified
matching-input evidence. When matching-input evidence is already missing, an inconclusive bounded
inspection records only the non-authoritative matched-recovery deadline, preventing every-cycle
catalogue reads while allowing new inputs, changed inputs, due audits and other same-show work to
run immediately. Such inspections can recover semantic evidence only when local evaluation of
observed candidates resolves uniquely to that same URI. Existing schema-5 edition fingerprints fail the versioned combined
fingerprint comparison once, then converge when qualifying evidence becomes available.

Remote destination evidence is bound to the immutable final desired returned by reconciliation.
The engine separately observes the current SQLite desired after reconciliation and publishes a
confirmation only when both fingerprints still match. A concurrent local change therefore remains
dirty and cannot inherit evidence for the previously verified content.

Catalogue completeness additionally requires a coherent Spotify paging envelope. Requested and
returned limits and offsets must match, totals must remain stable, item counts must fit the page,
and `next` must agree with observed progress. Any contradiction invalidates the whole inspection,
renews no audit, and grants no absence proof.

## Safety behavior

Missing, corrupt or incompatible evidence requires Spotify work and cannot authorize a local
skip. A desired transition immediately clears its old confirmed fingerprint. An unknown
mutation also clears confirmation, so `A -> B -> unknown -> A` cannot revive evidence for the
first A. Confirmation is invalidated before every current reconciliation and restored only when
the exact desired, destination, configuration and authorization context observed for that
operation still matches the durable row. Mutation outcome and skip-quality remote evidence are
separate: an exact readback may prove that an ambiguous write applied while a later unstable
snapshot bracket still prevents planner confirmation. Pending/degraded verification likewise does
not qualify. A crash during a possibly mutating call leaves conservative durable state.
Each local skip requires both a fresh durable wall-clock deadline and a fresh process-local
monotonic lease for the exact desired, destination, configuration and authorization context. A
wall-clock jump forward can make an audit due early; a rollback cannot extend permission beyond
the monotonic six-hour lease. Desired or context changes, pending or unknown state, failed safety
persistence, and reconciliation revoke the playlist's sole lease, so an `A -> B -> A` transition
cannot recover an older A lease. Remote evidence is published durably before a replacement lease
is created. Unresolved retry rollback remains conservative and never moves a retry earlier.

Planner observation remains fail-soft for reporting, while persistence required to invalidate
destination evidence is fail-closed for mutations. If that invalidation cannot be committed, the
engine preserves the destination and never reaches a PUT. The exact desired, destination,
configuration and authorization context remain conditions of the final confirmation update.

The environment switch `NEWS_PLAYLIST_SPOTIFY_LOCAL_SKIPS=0` disables local destination skips in
the current binary without weakening strict invalidation or the shared 429 gate. Schema remains
compatible with Phase 3; no database downgrade or restore is needed solely to revert this binary.

Engine and managed Admin Web API operations share the same durable rate-limit journal and request
lock. An active Web API cooldown is checked before token refresh needed for those operations, and
a 429 from either surface suppresses the other. The interactive OAuth callback remains outside
this Web API gate because code exchange uses the Spotify Accounts endpoint. Admin metadata and
cover operations do not invalidate ordered-item evidence because they do not change the item list
or destination identity.
