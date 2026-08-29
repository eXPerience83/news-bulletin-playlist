# Authenticated Spotify P0 probe

The public-source P0 research deliberately stops before using private credentials. This probe is the next step once a Spotify Developer application is available.

## What it checks

By default the PKCE probe is **read-only**:

- fetches up to 50 episodes from each known core show in market ES;
- prints recent SER, RNE, Onda Cero and CNN episode IDs/titles;
- checks how many of the known RNE 2026-08-25 18:00 republications Spotify exposes;
- searches the authenticated Spotify catalogue for `Boletines COPE`.

Writing is disabled unless `--write` (or the PowerShell helper's `-Write`) is passed explicitly. The write probe creates a temporary **private** playlist, writes one recent episode from each core provider and reads it back.

## Required scopes

The default read-only authorization requests only:

- `user-read-playback-position` for show/episode catalogue endpoints as currently documented;
- `user-read-private` for Spotify Search while investigating COPE.

Only `--write` requests the additional private-playlist scopes:

- `playlist-modify-private`;
- `playlist-read-private`.

`playlist-modify-public` is not required until a production playlist is intentionally made public.

## Redirect URI rule

Spotify currently requires HTTPS except for explicit loopback IP literals. `localhost` is not accepted. The local PKCE helper uses exactly:

`http://127.0.0.1:8787/callback`

A TrueNAS-hosted/admin-web callback will instead need a real HTTPS URL; a browser on another device cannot use the server's `127.0.0.1`.

## Recommended Docker/remote path: manual callback

When this helper runs in a remote Docker container, its `127.0.0.1` is not the
user's browser loopback. Use the default `manual` callback mode:

```bash
export SPOTIFY_CLIENT_ID='YOUR_CLIENT_ID'
python -m news_bulletin_playlist.spotify.oauth_probe --market ES
```

The helper prints the authorization URL and does not start an HTTP server or
attempt to open a browser. Open the URL in the user's browser and authorize it.
Spotify will redirect to `http://127.0.0.1:8787/callback?...`; a browser
connection failure is expected in this remote-container setup. Copy the complete
URL from the browser address bar and paste it into the terminal when prompted.
The callback is validated locally (including `state`) and is not echoed or
persisted. Access tokens, refresh tokens, authorization codes and PKCE verifiers
are kept only in memory and are never printed.

Spotify may use the country associated with the authenticated user ahead of the
requested `--market` parameter. `ES` is only the P0 default, not a global engine
assumption.

## Local-machine callback mode

For a helper actually running on the same computer as the browser, use:

```bash
python -m news_bulletin_playlist.spotify.oauth_probe --callback-mode local --market ES
```

It listens only on `127.0.0.1:8787`, has a finite timeout and ignores irrelevant
requests rather than ending authorization early.

## Recommended Windows path: one command

No Client Secret is required. Never commit tokens or credentials.

From a PowerShell terminal opened in the cloned repository:

```powershell
.\scripts\run_spotify_probe.ps1 -ClientId 'YOUR_CLIENT_ID'
```

The helper creates a local `.venv` if needed, installs the project, exposes the Client ID only for that process, prints the Spotify authorization URL and runs the read-only P0 probe after the secure manual callback is pasted.

Only after the read-only output has been reviewed should the write probe be run:

```powershell
.\scripts\run_spotify_probe.ps1 -ClientId 'YOUR_CLIENT_ID' -Write
```

## Advanced token diagnostic path

`SPOTIFY_ACCESS_TOKEN` remains supported by `news_bulletin_playlist.spotify.probe`
for short-lived, local diagnostics only. It is not the recommended authentication
path, must never be stored in `.env` or source control, and is not needed for the
PKCE helper above.

## Manual PKCE invocation

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

The default helper uses the secure manual callback path described above. Add
`--callback-mode local` only when browser and process share the same loopback.
It exchanges the returned authorization code using PKCE, runs the catalogue probe,
and does not persist returned tokens.

For the manual write probe:

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
