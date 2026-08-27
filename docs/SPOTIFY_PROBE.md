# Authenticated Spotify P0 probe

The public-source P0 research deliberately stops before using private credentials. This probe is the next step once a Spotify Developer application is available.

## What it checks

By default the probe is **read-only**:

- fetches up to 50 episodes from each known core show in market ES;
- prints recent SER, RNE, Onda Cero and CNN episode IDs/titles;
- checks how many of the known RNE 2026-08-25 18:00 republications Spotify exposes;
- searches the authenticated Spotify catalogue for `Boletines COPE`.

Writing is disabled unless `SPOTIFY_PROBE_WRITE=1` is set explicitly. The write probe creates a temporary **private** playlist, writes one recent episode from each core provider and reads it back.

## Required scopes

The first authorization experiment should request only:

- `user-read-playback-position` for show/episode catalogue endpoints as currently documented;
- `playlist-modify-private` for the optional private write probe.

`playlist-modify-public` is not required until the production playlist is made public.

## Redirect URI rule

Spotify currently requires HTTPS except for explicit loopback IP literals. `localhost` is not accepted. For a local authorization helper, use an explicit loopback URI such as:

`http://127.0.0.1:8787/callback`

A TrueNAS-hosted/admin-web callback will instead need a real HTTPS URL; a browser on another device cannot use the server's `127.0.0.1`.

## Running the probe

Never commit tokens. Export a short-lived access token only in the local shell/runtime:

```bash
export SPOTIFY_ACCESS_TOKEN='...'
python -m news_bulletin_playlist.spotify.probe
```

Only after the read-only output has been reviewed:

```bash
export SPOTIFY_PROBE_WRITE=1
python -m news_bulletin_playlist.spotify.probe
```

The temporary private playlist is intentionally left in the account so its order can be inspected manually, then deleted.
