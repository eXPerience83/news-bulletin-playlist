# TrueNAS deployment

The first supported NAS target is TrueNAS 26-BETA.3 or newer. This project is installed as a **Custom App from YAML**; it is not packaged for the TrueNAS Community catalog. Use **Apps -> Discover Apps -> menu -> Install via YAML**.

## Storage first

Create one dedicated dataset directly under `Pool1`:

`/mnt/Pool1/news-bulletin-playlist`

Use the **Apps** dataset preset and keep the remaining dataset options at their defaults unless you have a specific reason to change them.

TrueNAS 26's Apps preset includes Modify access for the built-in apps group (GID `568`). The TrueNAS deployment therefore runs the container directly as numeric UID/GID `568:568`. This keeps the process non-root and avoids any manual ACL customization.

The runtime root filesystem is read-only. `/data` is the only persistent writable application path; `/tmp` is an in-memory tmpfs.

For another TrueNAS system, edit the host path in `deploy/truenas.yaml` before installation.

## Install via YAML

1. In **Datasets**, select `Pool1` and create a dataset named `news-bulletin-playlist`.
2. Select **Dataset Preset: Apps** and save it. Do not customize ACLs for this deployment.
3. Open `deploy/truenas.yaml`.
4. In TrueNAS open **Apps -> Discover Apps -> menu -> Install via YAML**.
5. Use application name `news-bulletin-playlist`.
6. Paste the YAML and save.
7. Wait for the container health state to become healthy.
8. Browse to `http://<truenas-host>:8788/` using any reachable TrueNAS address.

The application listens on container port `8080`. The checked-in TrueNAS deployment publishes it as `0.0.0.0:8788:8080`, so Docker listens on all host interfaces. This allows the read-only status service to be reached through the appropriate TrueNAS LAN, Tailscale or other trusted host address without hard-coding one NAS IP into the deployment.

The YAML also includes top-level `x-portals` metadata with `host: 0.0.0.0`. Current TrueNAS portal generation accepts `0.0.0.0` as the wildcard/default host value. `x-portals` is only metadata: `ports:` is what actually publishes the service. If the TrueNAS UI does not show a Web UI button for the YAML-installed Custom App, use `http://<truenas-host>:8788/` directly.

The `/` page is intentionally read-only and `/healthz` remains the Docker health endpoint. Do not forward port `8788` from the router or expose it directly to the public Internet.

If port `8788` is already occupied, change the host port in `0.0.0.0:8788:8080` and the `x-portals` `port` to the same free value. Keep the container target at `8080`.

## Production Spotify authorization

Production Spotify authorization is opt-in. The checked-in base YAML does not contain a Spotify client ID, administration password or external URL, so `/admin/` is absent until you explicitly configure it.

The production flow uses Spotify Authorization Code + PKCE. It requires an **HTTPS external origin terminated by a reverse proxy**. The Python backend itself intentionally remains HTTP; it will accept administrative routes only when the immediate network peer belongs to `NEWS_PLAYLIST_TRUSTED_PROXY_CIDRS` and that trusted proxy supplies exactly `X-Forwarded-Proto: https`.

Do not put a Spotify Client Secret in this deployment. PKCE does not need one.

### 1. Create the administration password file

Store the administration password separately from the application YAML. The runtime supports `NEWS_PLAYLIST_ADMIN_PASSWORD_FILE`, and the file must be a regular file with no group/other permissions.

For this TrueNAS layout, `/data/admin-password` is a suitable location. Create it as UID/GID `568:568` with mode `0600`. Avoid commands that place the password directly in shell history.

The password must contain at least 16 characters.

### 2. Put the non-secret OAuth settings in the Custom App YAML

Add the following under `services.app` in your local TrueNAS Custom App definition, replacing the example values:

```yaml
environment:
  SPOTIFY_CLIENT_ID: "your-spotify-client-id"
  NEWS_PLAYLIST_EXTERNAL_URL: "https://news.example.com"
  NEWS_PLAYLIST_ADMIN_PASSWORD_FILE: "/data/admin-password"
  NEWS_PLAYLIST_TRUSTED_PROXY_CIDRS: "10.0.0.2/32"
```

`NEWS_PLAYLIST_TRUSTED_PROXY_CIDRS` is the source IP or CIDR of the **immediate reverse proxy as seen by the application**, not the browser/client subnet and not a broad Tailscale or LAN range. Multiple explicit entries may be comma-separated when genuinely required.

The application fails startup if an HTTPS Spotify administration URL is configured without an administration password or without a trusted proxy CIDR.

### 3. Configure the reverse proxy

Terminate TLS at the reverse proxy and forward the external HTTPS origin to the application's HTTP backend on port `8788`. The proxy must **overwrite** `X-Forwarded-Proto` with `https`; do not preserve or append a client-supplied value.

Keep backend port `8788` private to the host/trusted network. Even if a direct request reaches it, production `/admin/*` routes reject the request before issuing a Basic-authentication challenge unless the socket peer is a configured trusted proxy. The read-only `/` and `/healthz` routes do not require that administrative transport gate.

### 4. Register the exact Spotify callback

If `NEWS_PLAYLIST_EXTERNAL_URL` is:

`https://news.example.com`

register this exact redirect URI in the Spotify developer application:

`https://news.example.com/admin/spotify/callback`

The external URL must be an HTTPS origin without a path, query string or embedded credentials. HTTP is accepted only for an explicit loopback IP (`127.0.0.1` or `::1`) used by local/P0 development; private-LAN HTTP and `localhost` are intentionally rejected.

### 5. Connect Spotify

Open the **HTTPS external URL**, authenticate to `/admin/` with username `admin` and the configured administration password, then use **Connect Spotify**. The server generates a one-time CSRF token and a PKCE/state authorization request; Spotify returns directly to the server callback. No manual callback paste is required for the production flow.

Only the long-lived refresh credential is persisted under `/data`, owner-only and atomically replaced. Access tokens remain memory-only. If Spotify later returns `invalid_grant`, the runtime immediately becomes reauthorization-required and removes the known-invalid refresh credential from durable state whenever storage permits.

## Image integrity and updates

The checked-in TrueNAS YAML is pinned to an exact GHCR `sha256` digest that was built and validated by CI. It does **not** deploy the mutable `latest` tag. Reinstalling or restarting therefore resolves to the same reviewed image.

CI also attempts to pull the exact digest anonymously. This proves that a TrueNAS installation does not need GHCR credentials. If this check fails because the package is private, make the GHCR package **Public** rather than putting registry credentials in the repository or deployment YAML.

The GHCR workflow still publishes convenience `latest`, `sha-<git-sha>` and version tags from `main`, but the TrueNAS deployment should update only through a reviewed PR that replaces the digest in `deploy/truenas.yaml`. Keeping the previous digest in Git history provides a deterministic rollback path.

## Container shell and P0 OAuth probe

TrueNAS 26 exposes a shell for each application workload. The long-running process is `news-playlist serve`; the shell can run diagnostic commands alongside it.

The original P0/manual Spotify probe remains available for diagnostics:

```sh
export SPOTIFY_CLIENT_ID='your runtime client id'
python -m news_bulletin_playlist.spotify.oauth_probe --callback-mode manual --market ES
```

Do not add `--write` until the read-only output has been reviewed. The client ID is supplied only to that shell session and is not committed to the repository. No Client Secret is needed because the probe uses PKCE.

With manual callback mode, Spotify redirects the browser to `http://127.0.0.1:8787/callback`. A browser connection error is expected because browser loopback is not the remote container. Copy the complete callback URL from the browser address bar and paste it into the no-echo terminal prompt. This loopback/manual workflow is a P0 diagnostic path, not the production unattended authorization flow.

## Runtime design

The container does not use `sleep infinity`, `tail -f /dev/null`, cron, or a disposable external server. `news-playlist serve` is the durable application host. It owns process lifecycle, the read-only status portal, persistent-path validation, graceful signal handling, health checks and the opt-in private Spotify administration surface. The scheduler/engine in #20 grows inside the same runtime model.

The health endpoint performs a small real write, flush and `fsync` against `/data`, then removes the temporary probe file. This makes storage-full, quota and write failures observable as an unhealthy container instead of relying only on permission bits.
