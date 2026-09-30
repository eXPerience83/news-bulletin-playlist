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
4. Review the available built-in playlist templates. The primary Spain-focused Spanish template is **Noticias en Español**; the current catalog also exposes initial `INT · ES`, `INT · EN`, `INT · FR`, `INT · DE` and `INT · PL` experiments.
5. Review/edit each playlist name, provider-agnostic description, bundled cover, selected supported sources and maximum episode duration. The current general default is **30 minutes**, but every managed playlist stores its own configurable ceiling.
6. Activate a template. The application creates a Spotify playlist destination using the current **public** product semantics and persists the managed instance under `/data/managed-state.json`.
7. The scheduler is woken immediately; the read-only status page moves from **Not configured** to running/scheduled once an enabled managed playlist exists.
8. Repeat activation only for the additional playlists you actually want this installation to manage.

Subsequent playlist/source choices are installation-owned state. Updating the image can add supported sources/templates without overwriting selections already stored under `/data`. In particular, an existing managed playlist keeps its saved display name and explicit source selection when a newer image changes catalog defaults; review and save those changes deliberately from `/admin/`.

Stopping management of a playlist does not delete its Spotify destination by default. Paused/disabled managed playlists remain durable installation state but do not cause their sources to be fetched solely for that playlist.

### Source labels in admin

The source table and selectors distinguish **provider origin** (country or institution), **editorial scope** and **language**. Scope values are `LOC`, `REG`, `NAT`, `INT` and `MIX`. United Nations sources display `UN`, not a headquarters country. Origin does not imply coverage: CNN 5 Cosas is `US · INT · es-ES`, so its US origin does not mean US-only news; it is the current default for `INT · ES`, not for the Spain-focused **Noticias en Español** template.

## Managed configuration only

The runtime configuration is the image-owned built-in catalog plus schema v5 `/data/managed-state.json`. Existing v1/v2 managed state migrates atomically to v5 with an empty installation-owned `sources` collection; v3 upgrades preserve custom sources and default an absent temporal strategy to Auto. v4 upgrades default custom public release modes to instant and initialize an empty built-in mode overlay. Admin Sources can inspect and add validated custom Spotify-first sources, edit safe metadata, reversibly enable or disable them, assign them to playlists, and explicitly delete unreferenced custom sources after source-local operational cleanup. Managed state does not store OAuth secrets, inspection reports or raw provider responses.
Playlist activation, metadata, source membership and duration policy are managed through `/admin/`; there is no longer a second full-YAML runtime configuration path.

The pre-1.0 settings `NEWS_PLAYLIST_CONFIG` and `/data/news-bulletin-playlist.yaml` are obsolete.
A non-empty legacy environment value or any leftover default YAML path, including a broken symlink, causes startup to fail closed with an actionable message. Remove or relocate the obsolete setting or file after confirming the managed state is the intended installation configuration. The application never reads, rewrites or deletes the old YAML automatically.
### Scheduler

One scheduler inside the existing process runs the complete engine cycle immediately after a playlist becomes active/startup configuration is available and then approximately every 10 minutes. The default is 600 seconds:

```yaml
environment:
  NEWS_PLAYLIST_INTERVAL_SECONDS: "600"
```

The runtime rejects values below 60 seconds so a configuration mistake cannot create a tight retry loop. The normal production recommendation remains **600 seconds**.

There is no cron job, one-container-per-playlist model or overlapping worker pool. A cycle must finish before the next begins. A successful Spotify authorization callback or managed configuration change wakes the scheduler so the engine can retry promptly rather than waiting for the full interval.

Each cycle performs the Spotify-first sequence:

1. load the validated effective configuration from managed state plus the built-in catalog;
2. determine the union of sources required by enabled playlists;
3. observe each required source through the bounded public Spotify detector;
4. decide which official Spotify show catalogues are due because of bootstrap, detector change, inactivity audit or deterministic stale-item escalation;
5. refresh only those due catalogues and persist Spotify episode state;
6. reuse durable local episode state for unchanged sources;
7. build each playlist locally from retained Spotify episodes, title-derived semantic time where available, explicit release-date/first-seen provenance, duration policy and source filters;
8. reconcile destinations independently, using qualified local verification evidence when safe and conservative remote readback/snapshot handling when required;
9. record bounded source/playlist diagnostics plus public, Web API and OAuth request accounting;
10. prune operational history safely.

A detector or catalogue failure does not become an authoritative empty source. Still-valid persisted Spotify episodes remain eligible until their playlist retention boundary, so a transient public/API outage does not wipe recent content. One Spotify destination failure also does not block another playlist.

## Current TrueNAS Spotify authorization

Spotify authorization uses **Authorization Code + PKCE** and is opt-in. No Spotify Client Secret is required or supported by this application model.

The current production choice is the explicit **trusted-LAN administration mode** documented in [`LAN_ADMIN_AUTH.md`](LAN_ADMIN_AUTH.md). Hardened HTTPS/reverse-proxy administration remains implemented, but adopting it in production is a separate decision tracked by #145.

### 1. Configure trusted-LAN administration

Add the following under `services.app` in your **local TrueNAS Custom App definition**, replacing example values:

```yaml
environment:
  NEWS_PLAYLIST_ADMIN_MODE: "lan"
  NEWS_PLAYLIST_ADMIN_PASSWORD: "replace-with-a-strong-16-plus-character-password"
  SPOTIFY_CLIENT_ID: "your-spotify-client-id"
  NEWS_PLAYLIST_INTERVAL_SECONDS: "600"
```

Alternatively, store the administration password in an owner-only `0600` file such as `/data/admin-password` and configure `NEWS_PLAYLIST_ADMIN_PASSWORD_FILE=/data/admin-password`. Configure only one password source.

In LAN mode:

- do **not** configure `NEWS_PLAYLIST_EXTERNAL_URL`;
- do **not** configure `NEWS_PLAYLIST_TRUSTED_PROXY_CIDRS`;
- keep backend port `8788` reachable only from the trusted private LAN;
- never forward it from the router or publish it through an untrusted tunnel/funnel.

It is valid for the service to be configured but not yet connected: `/admin/` remains available for **Connect Spotify** and the engine remains **Not configured** until at least one managed playlist is activated.

### 2. Register the LAN callback

Register exactly this redirect URI in the Spotify developer application:

`http://127.0.0.1:8787/admin/spotify/callback`

Spotify redirects the browser to loopback after approval. Because the callback intentionally targets the browser machine rather than the NAS, a browser connection error is expected.

### 3. Connect Spotify from the Web UI

1. Open `http://<truenas-host>:8788/admin/` from the trusted LAN.
2. Authenticate with username `admin` and the configured administration password.
3. Choose **Connect Spotify** and open the generated Spotify authorization page.
4. Approve access.
5. After Spotify redirects to `127.0.0.1:8787`, copy the complete callback URL from the browser address bar.
6. Paste it into the authenticated **Complete callback URL** form in the original administration tab.

The runtime validates the callback origin/path, OAuth state and PKCE verifier before exchanging the code. Callback URLs, authorization codes and PKCE verifiers are not persisted or logged.

After Spotify reports connected, review **Noticias en Español** and any additional built-in templates you want to operate. Each activation creates a Spotify destination, persists its managed state and wakes the scheduler. There is no need to pre-create Spotify playlists or paste their IDs into YAML for the ordinary managed path.

### Optional hardened HTTPS mode

The HTTPS/reverse-proxy path using `NEWS_PLAYLIST_EXTERNAL_URL` plus narrow `NEWS_PLAYLIST_TRUSTED_PROXY_CIDRS` remains supported in code. It is not the current TrueNAS production choice. Evaluate and migrate to it separately under #145 rather than mixing that deployment change into engine/reconciliation validation.

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
- source result, last success, persisted episode count, refresh/request evidence and current error summary;
- playlist result, last success, desired/verified count, whether Spotify changed, and current error summary.

Operational cycle status is in memory; authoritative Spotify-first source/destination state, retained episode metadata, managed installation choices, request metrics/diagnostics and Spotify refresh authorization remain under `/data` and survive container restart. The current database format is 2 and retains accepted-episode research history separately from operational state; the separate pre-0.1 SQLite file may remain temporarily beside it solely as manual rollback evidence during the controlled cutover and is never read by the new runtime. After restart the scheduler immediately runs a new cycle when at least one playlist is enabled and reconstructs the current status from the durable engine state.

### Duration evidence

Sanitized diagnostics deliberately record duration exclusions, accepted editions of at least 20 minutes and bounded per-source/per-playlist duration histograms. The current buckets are `<5`, `5–8`, `8–10`, `10–15`, `15–20`, `20–30` and `>30` minutes. These logs are intended for multi-day review under issue #132 before the project chooses a long-term default hard limit. Do not infer source quality solely from the hard ceiling: predominantly long-form products remain a source-selection concern.

A source, playlist or authorization failure does **not** make `/healthz` unhealthy by itself. Container health is reserved for failures that make the runtime/storage itself unusable; otherwise an upstream outage could create an unhelpful restart loop. Operational failures are shown on `/` instead.

## Graceful shutdown

SIGTERM/SIGINT asks the HTTP host and scheduler to stop. The scheduler does not begin another cycle after shutdown is requested. If a cycle is already executing, the process lets that single in-flight cycle reach a safe boundary before exiting; TrueNAS/Docker retains the configured stop grace period as the outer bound.

## Troubleshooting

**Engine says Not configured**

- open `/admin/` and confirm at least one managed playlist is activated/enabled;
- after activation, confirm `/data/managed-state.json` exists and remains writable by the runtime user.

**Startup reports obsolete YAML configuration**

- remove any non-empty `NEWS_PLAYLIST_CONFIG` setting;
- remove or relocate `/data/news-bulletin-playlist.yaml` after confirming `/data/managed-state.json` is the intended configuration;
- do not delete or rewrite `managed-state.json` as part of this cleanup.

**Spotify says Not connected / Reauthorization required**

- use the HTTPS `/admin/` page and Connect/Reconnect Spotify;
- do not add a Client Secret;
- confirm the exact Spotify redirect URI matches the configured external origin.

**Optional hardened HTTPS mode returns 403 before asking for credentials**

- confirm traffic really arrives through the configured immediate reverse proxy;
- confirm its source address matches `NEWS_PLAYLIST_TRUSTED_PROXY_CIDRS`;
- confirm the proxy overwrites `X-Forwarded-Proto` with exactly `https`.

**A source detector/catalogue refresh fails but the playlist still contains recent items**

- this is expected Spotify-first carry-forward behavior; still-valid persisted Spotify episodes remain eligible until the playlist retention boundary;
- the source row and authenticated Diagnostics show the current detector/catalogue outcome and previous success.

**One playlist fails while another succeeds**

- this is intentional failure isolation; inspect that destination's error on `/` without treating the successful destination as failed.

## Image integrity and updates

Container publication follows the channel contract documented in [`RELEASE_CHANNELS.md`](RELEASE_CHANNELS.md):

`dev -> edge -> stable = latest`

The checked-in TrueNAS YAML follows `ghcr.io/experience83/news-bulletin-playlist:edge-amd64`. That channel contains only code already integrated into `main`; it is updated automatically after relevant merged changes pass the image build and hardened runtime smoke test. Normal TrueNAS upgrades therefore do **not** require editing a digest in the YAML for every build.

`dev-amd64` is reserved for deliberate pre-merge testing of an explicitly owner-authorized PR candidate. Normal PR CI never moves `dev`. `stable-amd64` and `latest` move only when an exact `vMAJOR.MINOR.PATCH` release from `main` is published, and `latest` is always the same stable image rather than an alias for current development.

GHCR also keeps candidate/revision tags and immutable image digests for audit and rollback. When exact reproduction matters, temporarily pin `ghcr.io/experience83/news-bulletin-playlist@sha256:<digest>` rather than relying on a mutable channel.

CI checks the deployment channel, package visibility, Compose rendering and the hardened health/web smoke test. The `/data` dataset is deliberately external to the container image, so updating or rolling back an image does not erase SQLite state, Spotify refresh authorization or managed configuration.

## 0.1.0 persistence baseline cutover

The clean Spotify-first persistence baseline uses
`/data/news-bulletin-playlist-v1.sqlite3`. The pre-0.1 private-development database remains at
`/data/news-bulletin-playlist.sqlite3` and is deliberately **not** migrated or modified by the new
runtime.

For the controlled TrueNAS cutover:

1. keep a backup of the complete `/data` dataset;
2. preserve `managed-state.json`, `spotify-auth.json`, admin credential files and
   `managed-playlist-provisioning.json` if present;
3. deploy the new image; it creates or upgrades to format 3 at the existing v1-named SQLite path;
4. expect the first cycle to rebuild detector/catalogue/destination evidence conservatively;
5. verify existing Spotify destination IDs are reused and playlists converge without being recreated;
6. inspect the new database read-only and verify database format 3 contains only the expected
   Spotify-first schema and separate accepted-episode history;
7. allow subsequent cycles to confirm pending snapshots and demonstrate quiet-cycle reuse without
   unnecessary catalogue reads;
8. restart/redeploy once more after current-format state exists and verify the runtime reuses that state without
   another bootstrap;
9. keep the old SQLite file until the new baseline has been validated, then archive/remove it
   deliberately together with any old `-wal`/`-shm` companions.

Do not treat container `healthy` as sufficient cutover evidence. The production validation for
0.1.0 demonstrated the intended sequence: bootstrap into the clean v1 database, natural
reconciliation/pending convergence, exact reuse of existing Spotify destination IDs, and then a
real container replacement that reopened the same v1 state without refreshing every catalogue.

The clean database intentionally starts without historical diagnostics/request metrics. Request
metric coverage is marked incomplete at database creation and becomes complete naturally after the
new runtime has accumulated a full 24-hour observation window.

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
application logs. Manual mode redirects to
`http://127.0.0.1:8787/admin/spotify/callback`; on a remote container a browser connection error is
expected. Copy the complete loopback callback into the no-echo terminal prompt, then remove
`/tmp/spotify-authorization-url`. This is the same canonical callback path as the admin flow and
remains a **P0 diagnostic path only**, not normal production authorization.

## Runtime design

`news-playlist serve` is the durable application host. It owns process lifecycle, storage validation, health, the read-only operational portal, the private OAuth administration surface and one sequential scheduler.

The current runtime data flow is:

> **Detect public change -> refresh only due official Spotify catalogues -> persist episode state -> compute locally -> reconcile safely.**

The health endpoint performs a small real write, flush and `fsync` against `/data`, then removes the temporary probe file. This makes storage-full, quota and write failures observable without conflating ordinary upstream failures with container health.

Older RSS/canonical/matching descriptions in historical investigation documents are retained only
for provenance. They are not current runtime, persistence or upgrade guidance.
