# Authenticated Spotify P0 probe

The public-source P0 research deliberately stops before using private credentials. This probe is the
next step once a Spotify Developer application is available.

## What it checks

By default the PKCE probe is **read-only**:

- fetches up to 50 episodes from each known core show in market ES;
- prints recent SER, RNE, Onda Cero and CNN episode IDs/titles;
- checks how many of the known RNE 2026-08-25 18:00 republications Spotify exposes;
- searches the authenticated Spotify catalogue for `Boletines COPE`.

Writing is disabled unless `--write` (or the PowerShell helper's `-Write`) is passed explicitly. The
write probe creates a temporary **private** playlist, writes one recent episode from each core
provider and reads it back.

## Required scopes

The default read-only authorization requests only:

- `user-read-playback-position` for show/episode catalogue endpoints as currently documented;
- `user-read-private` for Spotify Search while investigating COPE.

Only `--write` requests the additional private-playlist scopes:

- `playlist-modify-private`;
- `playlist-read-private`.

`playlist-modify-public` is not required until a production playlist is intentionally made public.

## OAuth handoff safety

The authorization URL contains the live OAuth `state` value and must be treated as transient
sensitive material. The probe therefore **never prints the authorization URL to stdout/stderr**.

There are two supported handoff paths:

1. **Local browser:** the probe opens the authorization URL directly in the default browser. The URL
   is passed to the browser process but is not echoed into application/terminal logs.
2. **Remote/container file handoff:** pass `--authorization-url-file PATH`. The probe creates a new
   file with mode `0600`, writes the one-time URL there, and prints only the file path. It refuses to
   overwrite an existing file. Inspect that file deliberately outside retained application logs,
   then remove it after authorization.

Authorization codes, PKCE verifiers and access tokens remain in memory only and are never printed.
The callback URL entered in manual mode is read with no-echo terminal input.

## Redirect URI rule

Spotify currently requires HTTPS except for explicit loopback IP literals. `localhost` is not
accepted. The local PKCE helper uses exactly:

`http://127.0.0.1:8787/callback`

A TrueNAS-hosted/admin-web callback will instead need a real HTTPS URL; a browser on another device
cannot use the server's `127.0.0.1`.

## Recommended local / Codex path

When the probe runs on the same computer as the browser, use local callback mode:

```bash
export SPOTIFY_CLIENT_ID='YOUR_CLIENT_ID'
python -m news_bulletin_playlist.spotify.oauth_probe --callback-mode local --market ES
```

The default browser opens automatically. After the user grants access, Spotify redirects to the
loopback listener and the probe continues without copying an authorization URL or callback code
through the terminal.

This is the preferred path for local Codex/CLI-assisted diagnostics: `SPOTIFY_CLIENT_ID` is not a
secret, while the authorization code/access token remain transient inside the probe. Codex should
never be given a long-lived production refresh token merely to run this diagnostic.

## Recommended Docker/remote path: private URL file + manual callback

When this helper runs in a remote Docker container, its `127.0.0.1` is not the user's browser
loopback. Use manual callback mode plus a one-time private URL file:

```bash
export SPOTIFY_CLIENT_ID='YOUR_CLIENT_ID'
python -m news_bulletin_playlist.spotify.oauth_probe \
  --callback-mode manual \
  --authorization-url-file /tmp/spotify-authorization-url \
  --market ES
```

The helper prints only the path `/tmp/spotify-authorization-url`, not the URL itself. Deliberately
retrieve/open the file outside retained application/container logs. Spotify will redirect the user's
browser to `http://127.0.0.1:8787/callback?...`; a browser connection failure is expected in this
remote-container setup. Copy the complete callback URL from the browser address bar and paste it
into the probe's no-echo terminal prompt. The callback is validated locally, including `state`.

Delete the one-time URL file after the handoff:

```bash
rm -f /tmp/spotify-authorization-url
```

The probe deliberately refuses to replace an existing authorization URL file; remove the previous
file or choose a new path before retrying.

Spotify may use the country associated with the authenticated user ahead of the requested `--market`
parameter. `ES` is only the P0 default, not a global engine assumption.

## Recommended Windows path: one command

No Client Secret is required. Never commit tokens or credentials.

From a PowerShell terminal opened in the cloned repository:

```powershell
.\scripts\run_spotify_probe.ps1 -ClientId 'YOUR_CLIENT_ID'
```

The helper creates a local `.venv` if needed, installs the project, exposes the Client ID only for
that process, starts the loopback callback listener and opens Spotify authorization in the default
browser. The live authorization URL/state are not printed.

Only after the read-only output has been reviewed should the write probe be run:

```powershell
.\scripts\run_spotify_probe.ps1 -ClientId 'YOUR_CLIENT_ID' -Write
```

## Advanced token diagnostic path

`SPOTIFY_ACCESS_TOKEN` remains supported by `news_bulletin_playlist.spotify.probe` for short-lived,
local diagnostics only. It is not the recommended authentication path, must never be stored in
`.env` or source control, and is not needed for the PKCE helper above.

## Manual PKCE invocation

### Windows PowerShell

```powershell
$env:SPOTIFY_CLIENT_ID='YOUR_CLIENT_ID'
python -m news_bulletin_playlist.spotify.oauth_probe --callback-mode local
```

### Linux/macOS — local browser

```bash
export SPOTIFY_CLIENT_ID='YOUR_CLIENT_ID'
python -m news_bulletin_playlist.spotify.oauth_probe --callback-mode local
```

Use manual mode with `--authorization-url-file` only when browser and process do not share the same
loopback. The helper exchanges the returned authorization code using PKCE, runs the catalogue probe,
and does not persist returned tokens.

For the local manual write probe, append `--write`. The temporary private playlist is intentionally
left in the account so its order can be inspected manually, then deleted.
