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

The checked-in base YAML deliberately contains **no Spotify credentials and no active managed playlist**. It therefore starts as a healthy status/runtime host with the engine shown as **Not configured**. This keeps initial installation and upgrades recoverable even before Spotify authorization or playlist provisioning is ready.

Do not forward backend port `8788` from the router or expose it directly to the public Internet. If `8788` is occupied, change both the published host port and the `x-portals` port; keep container port `8080`.

## Managed first-run configuration

The normal production path uses the built-in catalog plus installation-owned managed state under `/data`. Do **not** copy the legacy example YAML merely to make a new installation start running.

The ordinary first-run flow is:

1. Install or update the container image.
2. Configure the protected production administration surface and Spotify settings described below.
3. Connect Spotify from `/admin/`.
4. Review the available built-in playlist templates. The primary Spanish template is **Noticias en Español**; the current catalog also exposes initial `INT · ES`, `INT · EN`, `INT · FR`, `INT · DE` and `INT · PL` experiments.
5. Review/edit each playlist name, provider-agnostic description, bundled cover, selected supported sources and maximum episode duration. The current general default is **30 minutes**, but every managed playlist stores its own configurable ceiling.
6. Activate a template. The application creates a **private** Spotify playlist destination and persists the managed instance under `/data/managed-state.json`.
7. The scheduler is woken immediately; the read-only status page moves from **Not configured** to running/scheduled once an enabled managed playlist exists.
8. Repeat activation only for the additional playlists you actually want this installation to manage.

Subsequent playlist/source choices are installation-owned state. Updating the image can add supported sources/templates without overwriting selections already stored under `/data`. In particular, an existing managed playlist keeps its saved display name and explicit source selection when a newer image changes catalog defaults; review and save those changes deliberately from `/admin/`.

Stopping management of a playlist does not delete its Spotify destination by default. Paused/disabled managed playlists remain durable installation state but do not cause their sources to be fetched solely for that playlist.

### Source labels in admin

The source table and selectors distinguish **provider country**, **editorial scope** and **language**. Scope values are `LOC`, `REG`, `NAT`, `INT` and `MIX`. This prevents origin from being mistaken for coverage: for example, CNN 5 Cosas is `US · INT · es`, so its US origin does not mean US-only news and it can intentionally feed both `Noticias en Español` and `INT · ES`.

## Legacy/manual YAML compatibility

The original schema-v1 full-YAML loader remains available as an advanced/manual compatibility path. It is not the preferred first-run configuration for a managed installation.

The default legacy path is:

`/data/news-bulletin-playlist.yaml`

A different legacy file can be selected explicitly with:

```yaml
environment:
  NEWS_PLAYLIST_CONFIG: "/data/my-playlists.yaml"
```

When `NEWS_PLAYLIST_CONFIG` is explicitly set, that file must exist and validate or startup fails closed.

Without an explicit override, the runtime uses `/data/managed-state.json` when managed state exists and only falls back to the default legacy YAML when no managed state exists. If **both** `/data/managed-state.json` and `/data/news-bulletin-playlist.yaml` are present, startup fails closed rather than guessing which configuration source should win. Remove the unintended one before restarting.

[`config/news-bulletin-playlist.example.yaml`](../config/news-bulletin-playlist.example.yaml) is retained for this compatibility path. Legacy YAML destinations must contain real already-provisioned Spotify playlist IDs; never invent an ID or use a Spotify show ID as a writable playlist destination. If a legacy `duration_policy` omits `default_max_seconds`, it inherits the same current **1800-second** shared default as managed playlists; an explicit YAML value remains authoritative.

### Scheduler

One scheduler inside the existing process runs the complete engine cycle immediately after a playlist becomes active/startup configuration is available and then approximately every 10 minutes. The default is 600 seconds:

```yaml
environment:
  NEWS_PLAYLIST_INTERVAL_SECONDS: "600"
```

The runtime rejects values below 60 seconds so a configuration mistake cannot create a tight retry loop. The normal production recommendation remains **600 seconds**.

There is no cron job, one-container-per-playlist model or overlapping worker pool. A cycle must finish before the next begins. A successful Spotify authorization callback or managed configuration change wakes the scheduler so the engine can retry promptly rather than waiting for the full interval.

Each cycle performs the architecture-defined sequence:

1. load the validated effective configuration from managed state plus the built-in catalog, or from the deliberately selected legacy compatibility path;
2. determine the union of sources required by enabled playlists;
3. fetch each required source once;
4. normalize and persist canonical editions;
5. record source outcomes;
6. reuse/persist deterministic Spotify episode matching;
7. build each playlist from durable still-valid state;
8. apply the common per-playlist episode-eligibility policy;
9. reconcile destinations independently with exact readback verification;
10. record playlist outcomes and prune operational history safely.

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

It is valid for the service to be configured but not yet connected: `/admin/` remains available for **Connect Spotify** and the engine remains **Not configured** until at least one managed playlist is activated. If a deliberately selected legacy engine configuration exists before authorization is available, scheduler attempts surface authorization-required failures rather than clearing a destination.

### 3. Configure the reverse proxy

Terminate TLS at the reverse proxy and forward the external HTTPS origin to backend port `8788`. The proxy must **overwrite** `X-Forwarded-Proto` with `https`; do not append or preserve a client-supplied value.

Keep backend port `8788` private. Production `/admin/*` requests that arrive directly or from an untrusted peer are rejected before any Basic-authentication challenge. `/` and `/healthz` remain read-only and do not use that admin transport gate.

### 4. Register the exact Spotify callback

For:

`NEWS_PLAYLIST_EXTERNAL_URL=https://news.example.com`

register exactly:

`https://news.example.com/admin/spotify/callback`

The external URL must be an HTTPS origin without path, query string or embedded credentials. HTTP is accepted only for an explicit loopback IP (`127.0.0.1` or `::1`) used by local/P0 diagnostics; private-LAN HTTP and `localhost` are intentionally rejected.

### 5. Connect Spotify and activate playlists

Open the HTTPS external origin, authenticate to `/admin/` with username `admin` and the configured administration password, then choose **Connect Spotify**. The application uses one-time CSRF, strong OAuth state and PKCE S256; Spotify returns directly to the server callback. Normal production operation requires no shell and no callback URL paste.

After Spotify reports connected, review **Noticias en Español** and any additional built-in templates you want to operate. Each activation creates a private Spotify destination, persists its managed state and wakes the scheduler. There is no need to pre-create Spotify playlists or paste their IDs into YAML for the ordinary managed path.

Only the long-lived refresh credential is persisted under `/data`, owner-only and atomically replaced. Access tokens remain memory-only. Scheduler token refresh and Web UI reconnect/callback operations are serialized against the same credential store so they cannot race.

If Spotify returns `invalid_grant`, the runtime surfaces **Reauthorization required** and removes the known-invalid refresh credential from durable state whenever storage permits.

## Read-only operational status

The `/` page reports, without token material:

- runtime and persistent-storage state;
- Spotify authorization state;
- whether the engine is configured/running/scheduled;
- last cycle result, start and finish;
- next scheduled run;
- application version and safe short source-build revision;
- source result, last success, fetched/matched count and current error summary;
- playlist result, last success, desired/verified count, whether Spotify changed, and current error summary.

Operational cycle status is in memory; authoritative source/playlist history, canonical data, match state, managed installation choices and Spotify refresh authorization remain under `/data` and survive container restart. After restart the scheduler immediately runs a new cycle when at least one playlist is enabled and reconstructs the current status from the durable engine state.

### Duration evidence

Sanitized diagnostics deliberately record duration exclusions, accepted editions of at least 20 minutes and bounded per-source/per-playlist duration histograms. The current buckets are `<5`, `5–8`, `8–10`, `10–15`, `15–20`, `20–30` and `>30` minutes. These logs are intended for multi-day review under issue #132 before the project chooses a long-term default hard limit. Do not infer source quality solely from the hard ceiling: predominantly long-form products remain a source-selection concern.

A source, playlist or authorization failure does **not** make `/healthz` unhealthy by itself. Container health is reserved for failures that make the runtime/storage itself unusable; otherwise an upstream outage could create an unhelpful restart loop. Operational failures are shown on `/` instead.

## Graceful shutdown

SIGTERM/SIGINT asks the HTTP host and scheduler to stop. The scheduler does not begin another cycle after shutdown is requested. If a cycle is already executing, the process lets that single in-flight cycle reach a safe boundary before exiting; TrueNAS/Docker retains the configured stop grace period as the outer bound.

## Troubleshooting

**Engine says Not configured**

- on a normal managed installation, open `/admin/` and confirm at least one managed playlist is activated/enabled;
- after activation, confirm `/data/managed-state.json` exists and remains writable by the runtime user;
- if intentionally using the legacy compatibility path, confirm the selected YAML exists and validates;
- do not leave both `/data/managed-state.json` and the default `/data/news-bulletin-playlist.yaml` in place.

**Startup reports ambiguous managed/legacy configuration**

- decide which path the installation should use;
- for normal operation, retain `/data/managed-state.json` and remove/relocate the obsolete default legacy YAML;
- for an intentional legacy deployment, remove managed state or explicitly select the intended legacy file with `NEWS_PLAYLIST_CONFIG`.

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
