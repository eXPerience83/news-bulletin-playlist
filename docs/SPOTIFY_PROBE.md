# Authenticated Spotify P0 probe

The public-source P0 research deliberately stops before using private credentials. This probe is the next step once a Spotify Developer application is available.

## What it checks

By default the PKCE probe is **read-only**:

- fetches up to 50 episodes from each known core show in market ES;
- prints recent SER, RNE, Onda Cero and CNN episode IDs/titles;
- checks how many of the known RNE 2026-08-25 18:00 republications Spotify exposes;
- searches the authenticated Spotify catalogue for `Boletines COPE`.

Writing is disabled unless `--write` is passed explicitly. The write probe creates a temporary **private** playlist, writes one recent episode from each core provider and reads it back.

## Required scopes

The first authorization experiment requests only:

- `user-read-playback-position` for show/episode catalogue endpoints as currently documented;
- `playlist-modify-private` for the optional private write probe.

`playlist-modify-public` is not required until a production playlist is intentionally made public.

## Redirect URI rule

Spotify currently requires HTTPS except for explicit loopback IP literals. `localhost` is not accepted. The local PKCE helper uses exactly:

`http://127.0.0.1:8787/callback`

A TrueNAS-hosted/admin-web callback will instead need a real HTTPS URL; a browser on another device cannot use the server's `127.0.0.1`.

## Running via PKCE

No Client Secret is required. Never commit tokens or credentials.

### Windows PowerShell

```powershell
$env:SPOTIFY_CLIENT_ID='YOUR_CLIENT_ID'
python -m news_bulletin_playlist.spotify.oauth_probe
```

### Linux/macOS

```bash
export SPOTIFY_CLIENT_ID='YOUR_CLIENT_ID'
python -m news_bulletin_playlist.spotify.oauth_probe
```

The helper opens the Spotify authorization page, listens only on `127.0.0.1:8787`, exchanges the returned authorization code using PKCE, runs the catalogue probe, and does not persist the returned access token.

Only after the read-only output has been reviewed should the write probe be run:

### Windows PowerShell

```powershell
$env:SPOTIFY_CLIENT_ID='YOUR_CLIENT_ID'
python -m news_bulletin_playlist.spotify.oauth_probe --write
```

### Linux/macOS

```bash
export SPOTIFY_CLIENT_ID='YOUR_CLIENT_ID'
python -m news_bulletin_playlist.spotify.oauth_probe --write
```

The temporary private playlist is intentionally left in the account so its order can be inspected manually, then deleted.
