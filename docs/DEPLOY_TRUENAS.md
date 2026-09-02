# TrueNAS deployment

The first supported NAS target is TrueNAS 26-BETA.3 or newer. Install this project as a **Custom App from YAML** using **Apps -> Discover Apps -> menu -> Install via YAML**. It is not packaged for the TrueNAS Community catalog.

## Storage first

Create one dedicated dataset directly under `Pool1`:

`/mnt/Pool1/news-bulletin-playlist`

Use the **Apps** dataset preset and keep the remaining dataset options at their defaults unless you have a specific reason to change them.

TrueNAS 26's Apps preset includes Modify access for the built-in apps group (GID `568`). The checked-in deployment therefore runs the container as numeric UID/GID `568:568`. The root filesystem is read-only; `/data` is the only persistent writable application path and `/tmp` is an in-memory tmpfs.

For another TrueNAS system, edit the host path in `deploy/truenas.yaml` before installation.

## Install the base runtime

1. In **Datasets**, select `Pool1` and create `news-bulletin-playlist`.
2. Select **Dataset Preset: Apps** and save it. Do not customize ACLs for this deployment.
3. Open `deploy/truenas.yaml`.
4. In TrueNAS open **Apps -> Discover Apps -> menu -> Install via YAML**.
5. Use application name `news-bulletin-playlist`.
6. Paste the YAML and save.
7. Wait for the container health state to become healthy.
8. Browse to `http://<truenas-host>:8788/` using a reachable TrueNAS address.

The application listens on container port `8080`. The base deployment publishes `0.0.0.0:8788:8080`; `x-portals` is only TrueNAS UI metadata. `/` is read-only and `/healthz` performs the Docker health check.

The checked-in base YAML deliberately contains **no Spotify credentials and no production engine configuration**. It therefore starts as a healthy status/runtime host with the engine shown as **Not configured**. This keeps initial installation and upgrades recoverable even before Spotify authorization is ready.

Do not forward backend port `8788` from the router or expose it directly to the public Internet. If `8788` is occupied, change both the published host port and the `x-portals` port; keep container port `8080`.

## Enable the production engine

The production engine is enabled by placing a valid configuration file at:

`/data/news-bulletin-playlist.yaml`

Start from `config/news-bulletin-playlist.example.yaml`. Replace every placeholder destination `external_id` with the **already provisioned Spotify playlist ID** before copying the file into `/data`. Do not invent IDs and do not use a Spotify show ID as a writable playlist destination.

An alternative persistent path can be selected with:

```yaml
environment:
  NEWS_PLAYLIST_CONFIG: "/data/my-playlists.yaml"
```

If `NEWS_PLAYLIST_CONFIG` explicitly names a missing file, startup fails closed instead of silently running a different configuration.

### Scheduler

One scheduler inside the existing process runs the complete engine cycle immediately after startup and then approximately every 10 minutes. The default is 600 seconds:

```yaml
environment:
  NEWS_PLAYLIST_INTERVAL_SECONDS: "600"
```

The runtime rejects values below 60 seconds so a configuration mistake cannot create a tight retry loop. The normal production recommendation remains **600 seconds**.

There is no cron job, one-container-per-playlist model or overlapping worker pool. A cycle must finish before the next begins. A successful Spotify authorization callback wakes the scheduler so the engine can retry promptly rather than waiting for the full interval.

Each cycle performs the architecture-defined sequence:

1. load the validated shared configuration;
2. determine the union of sources required by enabled playlists;
3. fetch each required source once;
4. normalize and persist canonical editions;
5. record source outcomes;
6. reuse/persist deterministic Spotify episode matching;
7. build each playlist from durable still-valid state;
8. reconcile destinations independently with exact readback verification;
9. record playlist outcomes and prune operational history safely.

A failed RSS source does not become an empty successful source. Recent previously matched canonical editions remain eligible until their playlist retention boundary, so one transient feed outage does not wipe valid recent items. One Spotify destination failure also does not block another playlist.

## Production Spotify authorization

Production Spotify authorization uses **Authorization Code + PKCE** and is opt-in. No Spotify Client Secret is required or supported by this application model.

The production admin flow requires an **HTTPS external origin terminated by a reverse proxy**. The Python backend intentionally remains HTTP; administrative routes are accepted only when the immediate socket peer is inside `NEWS_PLAYLIST_TRUSTED_PROXY_CIDRS` and that proxy supplies exactly `X-Forwarded-Proto: https`.

### 1. Create the administration password file

Store the administration password separately from the Custom App YAML. `/data/admin-password` is a suitable location. It must be a regular file owned/readable by the runtime user and must grant no group/other access; use mode `0600`. The password must contain at least 16 characters.

Avoid commands that place the password itself in shell history.

### 2. Add the non-secret runtime settings

Add the following under `services.app` in your **local TrueNAS Custom App definition**, replacing example values:

```yaml
environment:
  SPOTIFY_CLIENT_ID: "your-spotify-client-id"
  NEWS_PLAYLIST_EXTERNAL_URL: "https://news.example.com"
  NEWS_PLAYLIST_ADMIN_PASSWORD_FILE: "/data/admin-password"
  NEWS_PLAYLIST_TRUSTED_PROXY_CIDRS: "10.0.0.2/32"
  NEWS_PLAYLIST_INTERVAL_SECONDS: "600"
```

`NEWS_PLAYLIST_TRUSTED_PROXY_CIDRS` is the source IP/CIDR of the **immediate reverse proxy as seen by the application**, not the browser subnet, LAN subnet or a broad Tailscale range. Multiple explicit entries may be comma-separated only when genuinely required.

If a production engine YAML exists but the Spotify Web UI authorization service is not configured, startup fails closed. It is valid for the service to be configured but not yet connected: the scheduler records authorization-required failures while the Web UI remains available for **Connect Spotify**.

### 3. Configure the reverse proxy

Terminate TLS at the reverse proxy and forward the external HTTPS origin to backend port `8788`. The proxy must **overwrite** `X-Forwarded-Proto` with `https`; do not append or preserve a client-supplied value.

Keep backend port `8788` private. Production `/admin/*` requests that arrive directly or from an untrusted peer are rejected before any Basic-authentication challenge. `/` and `/healthz` remain read-only and do not use that admin transport gate.

### 4. Register the exact Spotify callback

For:

`NEWS_PLAYLIST_EXTERNAL_URL=https://news.example.com`

register exactly:

`https://news.example.com/admin/spotify/callback`

The external URL must be an HTTPS origin without path, query string or embedded credentials. HTTP is accepted only for an explicit loopback IP (`127.0.0.1` or `::1`) used by local/P0 diagnostics; private-LAN HTTP and `localhost` are intentionally rejected.

### 5. Connect Spotify

Open the HTTPS external origin, authenticate to `/admin/` with username `admin` and the configured administration password, then choose **Connect Spotify**. The application uses one-time CSRF, strong OAuth state and PKCE S256; Spotify returns directly to the server callback. Normal production operation requires no shell and no callback URL paste.

Only the long-lived refresh credential is persisted under `/data`, owner-only and atomically replaced. Access tokens remain memory-only. Scheduler token refresh and Web UI reconnect/callback operations are serialized against the same credential store so they cannot race.

If Spotify returns `invalid_grant`, the runtime surfaces **Reauthorization required** and removes the known-invalid refresh credential from durable state whenever storage permits.

## Read-only operational status

The `/` page reports, without token material:

- runtime and persistent-storage state;
- Spotify authorization state;
- whether the engine is configured/running/scheduled;
- last cycle result, start and finish;
- next scheduled run;
- source result, last success, fetched/matched count and current error summary;
- playlist result, last success, desired/verified count, whether Spotify changed, and current error summary.

Operational cycle status is in memory; authoritative source/playlist history, canonical data, match state and Spotify refresh authorization remain under `/data` and survive container restart. After restart the scheduler immediately runs a new cycle and reconstructs the current status from the durable engine state.

A source, playlist or authorization failure does **not** make `/healthz` unhealthy by itself. Container health is reserved for failures that make the runtime/storage itself unusable; otherwise an upstream outage could create an unhelpful restart loop. Operational failures are shown on `/` instead.

## Graceful shutdown

SIGTERM/SIGINT asks the HTTP host and scheduler to stop. The scheduler does not begin another cycle after shutdown is requested. If a cycle is already executing, the process lets that single in-flight cycle reach a safe boundary before exiting; TrueNAS/Docker retains the configured stop grace period as the outer bound.

## Troubleshooting

**Engine says Not configured**

- confirm `/data/news-bulletin-playlist.yaml` exists, or set `NEWS_PLAYLIST_CONFIG` to the correct persistent path;
- validate that the file is based on the current schema and contains real playlist destination IDs.

**Spotify says Not connected / Reauthorization required**

- use the HTTPS `/admin/` page and Connect/Reconnect Spotify;
- do not add a Client Secret;
- confirm the exact Spotify redirect URI matches the configured external origin.

**Admin returns 403 before asking for credentials**

- confirm traffic really arrives through the configured immediate reverse proxy;
- confirm its source address matches `NEWS_PLAYLIST_TRUSTED_PROXY_CIDRS`;
- confirm the proxy overwrites `X-Forwarded-Proto` with exactly `https`.

**A source fails but the playlist still contains recent items**

- this is expected carry-forward behavior; valid durable matches remain until the playlist retention boundary;
- the source row on `/` shows the current collection error and previous success.

**One playlist fails while another succeeds**

- this is intentional failure isolation; inspect that destination's error on `/` without treating the successful destination as failed.

## Image integrity and updates

Container publication follows the channel contract documented in [`RELEASE_CHANNELS.md`](RELEASE_CHANNELS.md):

`dev -> edge -> stable = latest`

The checked-in TrueNAS YAML follows `ghcr.io/experience83/news-bulletin-playlist:edge-amd64`. That channel contains only code already integrated into `main`; it is updated automatically after relevant merged changes pass the image build and hardened runtime smoke test. Normal TrueNAS upgrades therefore do **not** require editing a digest in the YAML for every build.

`dev-amd64` is reserved for deliberate pre-merge testing of an explicitly owner-authorized PR candidate. Normal PR CI never moves `dev`. `stable-amd64` and `latest` move only when an exact `vMAJOR.MINOR.PATCH` release from `main` is published, and `latest` is always the same stable image rather than an alias for current development.

GHCR also keeps candidate/revision tags and immutable image digests for audit and rollback. When exact reproduction matters, temporarily pin `ghcr.io/experience83/news-bulletin-playlist@sha256:<digest>` rather than relying on a mutable channel.

CI checks the deployment channel, package visibility, Compose rendering and the hardened health/web smoke test. The `/data` dataset is deliberately external to the container image, so updating or rolling back an image does not erase SQLite state, Spotify refresh authorization or managed configuration.

## P0/manual OAuth probe

The original P0/manual probe remains available as a diagnostic tool from the workload shell. The
one-time authorization URL contains OAuth `state` and is therefore written to a private file rather
than application/container logs:

```sh
export SPOTIFY_CLIENT_ID='your runtime client id'
python -m news_bulletin_playlist.spotify.oauth_probe \
  --callback-mode manual \
  --authorization-url-file /tmp/spotify-authorization-url \
  --market ES
```

Do not add `--write` until the read-only output has been reviewed. The probe creates the URL file as
a new mode-`0600` file and prints only its path. Inspect/open it deliberately outside retained
application logs. Manual mode redirects to `http://127.0.0.1:8787/callback`; on a remote container a
browser connection error is expected. Copy the complete loopback callback into the no-echo terminal
prompt, then remove `/tmp/spotify-authorization-url`. This remains a **P0 diagnostic path only**, not
normal production authorization.

## Runtime design

`news-playlist serve` is the durable application host. It owns process lifecycle, storage validation, health, the read-only operational portal, the private OAuth administration surface and one sequential scheduler. The architecture remains:

> **Fetch once -> normalize once -> store once -> distribute to many playlists.**

The health endpoint performs a small real write, flush and `fsync` against `/data`, then removes the temporary probe file. This makes storage-full, quota and write failures observable without conflating ordinary upstream failures with container health.
