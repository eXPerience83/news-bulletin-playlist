# Operational cadence and watchdog roles

The project has three deliberately separate recurring activities. They solve different problems and
must not be substituted for one another.

## Production Docker scheduler — approximately every 10 minutes

The long-lived application process on TrueNAS owns normal collection and playlist maintenance. It
runs one complete engine cycle immediately after startup and then every 600 seconds by default.

A production cycle collects the union of required sources, normalizes and matches editions, builds
desired playlist state and reconciles Spotify destinations. This is the only recurring mechanism
that mutates the managed production playlists.

The runtime cadence is configured with `NEWS_PLAYLIST_INTERVAL_SECONDS`; the production default is
600 seconds. GitHub Actions is **not** the production playlist scheduler.

## Provider Watch GitHub Action — approximately every 6 hours

`.github/workflows/provider-watch.yml` is an independent known-contract canary. Its scheduled run is
`17 */6 * * *`, or roughly four checks per day. GitHub may delay scheduled workflows, so exact
wall-clock execution is not a product requirement.

Provider Watch checks whether already-supported upstream feed/title/parser contracts still behave as
expected. It does not reconcile Spotify playlists and does not mutate production runtime state.

The workflow also keeps its existing immediate paths:

- relevant pushes to `main` run the contract check;
- `workflow_dispatch` allows an explicit manual run;
- a failed contract check opens or updates the single
  `[provider-watch] Upstream contract check failed` incident;
- a later successful check closes that incident automatically;
- the workflow remains visibly failed while an upstream incident is active.

## Source catalog review — monthly initially, later quarterly

Source discovery and qualitative re-evaluation are tracked by issue #55. This review searches for
new or better bulletin sources, revisits candidates/blocked sources and verifies that the catalog
still represents the best available product choices.

This is intentionally slower than Provider Watch because it is research/curation rather than an
availability alarm. While European coverage is expanding, review monthly; once the main
country/language combinations are stable, move to quarterly. Extra reviews may be run after major
broadcaster/feed or Spotify-catalog changes.

## Summary

| Activity | Normal cadence | Purpose | Mutates production playlists? |
| --- | --- | --- | --- |
| Docker/TrueNAS engine | ~10 minutes | Collect, match and reconcile managed playlists | Yes |
| Provider Watch Action | ~6 hours | Detect breakage of known upstream contracts | No |
| Source catalog review (#55) | Monthly, later quarterly | Discover/re-evaluate sources | No |

Keeping these roles separate prevents a GitHub outage or delayed scheduled Action from affecting
normal playlist updates, while still preserving an independent upstream-contract warning system and
a slower editorial/source-research loop.
