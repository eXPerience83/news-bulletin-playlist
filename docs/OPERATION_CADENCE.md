# Operational cadence

The Spotify-first runtime separates **engine cadence** from **catalogue refresh cadence**.

A ten-minute engine tick does not mean every source catalogue is read every ten minutes.

## Production engine scheduler — approximately every 10 minutes

The long-lived application process on TrueNAS owns playlist maintenance.

It runs one cycle immediately after startup and then every 600 seconds by default. The interval is
configured with `NEWS_PLAYLIST_INTERVAL_SECONDS`.

Each cycle:

1. loads the effective configuration compiled from the built-in catalog and managed installation state;
2. observes the public detector state for required sources;
3. decides which official Spotify catalogues, if any, are due;
4. refreshes only those catalogues;
5. reuses durable local state for unchanged sources;
6. computes complete desired playlist state locally;
7. reconciles each destination independently where required;
8. records bounded diagnostics/request accounting.

GitHub Actions is **not** the production playlist scheduler.

## Change-triggered catalogue refresh — event-driven within engine cycles

The normal source refresh trigger is a public Spotify latest-episode identity transition.

When source S changes, only source S's official Spotify Web API show catalogue is refreshed in the
normal path. A source selected by several playlists is still refreshed once and its persisted state
is shared by all of them.

The detector may advance across more than one publication. The official catalogue refresh is
responsible for recovering all relevant episodes needed to cover retention; the detector itself is
not an episode catalogue.

## Inactivity audit — default 24 hours

A source that has had no successful detector transition/catalogue refresh for the configured
inactivity threshold becomes due for an independent official catalogue audit.

The default is 24 hours and is configured with
`NEWS_PLAYLIST_SPOTIFY_INACTIVITY_HOURS`.

This is not a fixed daily poll. If normal detector changes keep causing successful catalogue
refreshes, the inactivity audit deadline keeps moving and no extra audit is needed.

An audit can detect a public-detector false negative. If official API state is newer than the
detector evidence, the runtime records that discrepancy and refreshes the source state.

## Stale-item escalation — exceptional

A deterministic stale/unavailable desired-item failure may cause the engine to refresh the other
relevant catalogues for the affected playlist before rebuilding desired state.

This is not a normal cadence and must not be used as a generic response to all write errors.

Ambiguous mutating transport/5xx failures continue through readback/snapshot resolution instead.
HTTP 429 continues through the shared global cooldown.

## Source catalog review — monthly initially, later quarterly

Editorial/source discovery is slower than runtime maintenance.

Review candidates, blocked products and source quality monthly while coverage is expanding; move to
quarterly once the main country/language combinations are stable. Additional review is appropriate
after material Spotify/provider product changes.

Research review does not mutate production playlists by itself. Promotion into the built-in catalog
is a normal code change with CI/tests.

## Summary

| Activity | Normal cadence | Purpose | Mutates production playlists? |
| --- | --- | --- | --- |
| Engine scheduler | ~10 minutes | Detect changes, compute desired state, reconcile when needed | Yes |
| Changed-source catalogue refresh | On detector transition | Refresh only the changed source from official Spotify API | Indirectly |
| Inactivity audit | After configured inactivity (24 h default) | Detect missed public-detector changes | Indirectly |
| Stale-item escalation | Only on deterministic evidence | Repair stale locally persisted catalogue state | Indirectly |
| Source catalog review | Monthly, later quarterly | Discover/re-evaluate products | No |

The key quota invariant is that scheduler frequency and Web API catalogue frequency are different:
frequent local cycles are acceptable because unchanged sources do not require authenticated catalogue
reads.
