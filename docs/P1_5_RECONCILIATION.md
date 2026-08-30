# P1.5 desired-state and Spotify reconciliation

P1.5 turns durable canonical editions and Spotify match state into the exact ordered contents for each enabled Spotify playlist.

## Desired-state policy

Desired-state construction is pure and performs no SQLite or Spotify I/O. For each playlist it:

1. selects only canonical editions from the playlist's explicit source selection;
2. uses source `published_at` as the authoritative ordering timestamp;
3. keeps only editions inside the configured retention window, including the exact lower boundary and excluding future timestamps;
4. excludes editions without a durable `MATCHED` Spotify mapping, including `PENDING` and `AMBIGUOUS` outcomes;
5. sorts publication time descending with canonical identity as a deterministic tie-breaker;
6. deduplicates identical Spotify episode URIs after sorting, so distinct source-native identities that converge on one Spotify episode produce one destination item and the newest canonical occurrence wins;
7. applies the configured `max_episodes` and Spotify's hard 100-item replacement limit, whichever is lower.

The default policy therefore remains 48 hours and at most 100 Spotify episodes. Items older than the retention window are never backfilled merely to fill the playlist.

## Carry-forward on source failure

Playlist desired state is built from durable canonical/match state rather than only the current RSS fetch batch.

As a result, when a source fails in the current collection cycle, previously matched editions from that source remain eligible while their `published_at` timestamp is still inside the playlist retention window. They expire naturally at the same retention boundary as any healthy-source edition. No separate copied playlist-owned edition state is required.

This preserves the core invariant:

> Fetch once -> normalize once -> store once -> distribute to many playlists.

## Spotify reconciliation

Reconciliation is idempotent and bounded:

- the complete current playlist state is read before a write;
- if current URI order/content already equals desired state, no write is performed;
- desired states above 100 items are rejected before an API write;
- when a replacement is required, the playlist is replaced as one bounded operation;
- the playlist is read again immediately after a write;
- exact URI order, count and content must match desired state or reconciliation fails closed.

The playlist reader also probes one item beyond the first 100 when Spotify reports another page, preserving the P0 overflow guard.

## Multi-playlist isolation

The same canonical edition and durable Spotify mapping can contribute to several playlist desired states without being duplicated in collection or persistence.

Destination reconciliation is executed independently. A Spotify API, transport or structural/readback failure for one playlist produces a failed result for that destination and does not prevent another playlist plan from being reconciled in the same batch.

## Deliberate boundaries

P1.5 does not own:

- recurring engine scheduling;
- production OAuth callback handling or refresh-token lifecycle;
- full engine-cycle orchestration and status-page integration.

Those remain P1.6 (#19) and P1.7 (#20).
