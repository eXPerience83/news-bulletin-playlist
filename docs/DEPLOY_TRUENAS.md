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

The application listens on container port `8080`. The checked-in TrueNAS deployment publishes it as `0.0.0.0:8788:8080`, so Docker listens on all host interfaces. This allows the same service to be reached through the appropriate TrueNAS LAN, Tailscale or other trusted host address without hard-coding one NAS IP into the deployment.

The YAML also includes top-level `x-portals` metadata with `host: 0.0.0.0`. Current TrueNAS portal generation accepts `0.0.0.0` as the wildcard/default host value. `x-portals` is only metadata: `ports:` is what actually publishes the service. If the TrueNAS UI does not show a Web UI button for the YAML-installed Custom App, use `http://<truenas-host>:8788/` directly.

The current `/` page is intentionally read-only: it shows runtime, persistent-storage and version status only. It contains no credentials and has no administrative actions. `/healthz` remains the Docker health endpoint. Do not forward port `8788` from the router or otherwise expose it directly to the public Internet. Authentication must be added before future state-changing administration is exposed through the web UI.

If port `8788` is already occupied, change the host port in `0.0.0.0:8788:8080` and the `x-portals` `port` to the same free value. Keep the container target at `8080`.

## Image integrity and updates

The checked-in TrueNAS YAML is pinned to an exact GHCR `sha256` digest that was built and validated by CI. It does **not** deploy the mutable `latest` tag. Reinstalling or restarting therefore resolves to the same reviewed image.

CI also attempts to pull the exact digest anonymously. This proves that a TrueNAS installation does not need GHCR credentials. If this check fails because the package is private, make the GHCR package **Public** rather than putting registry credentials in the repository or deployment YAML.

The GHCR workflow still publishes convenience `latest`, `sha-<git-sha>` and version tags from `main`, but the TrueNAS deployment should update only through a reviewed PR that replaces the digest in `deploy/truenas.yaml`. Keeping the previous digest in Git history provides a deterministic rollback path.

## Container shell and OAuth probe

TrueNAS 26 exposes a shell for each application workload. The long-running process is `news-playlist serve`; the shell can run diagnostic commands alongside it.

For the first Spotify probe, open the app workload shell and run:

```sh
export SPOTIFY_CLIENT_ID='your runtime client id'
python -m news_bulletin_playlist.spotify.oauth_probe --callback-mode manual --market ES
```

Do not add `--write` until the read-only output has been reviewed. The client ID is supplied only to that shell session and is not committed to the repository. No Client Secret is needed because the probe uses PKCE.

With manual callback mode, Spotify redirects the browser to `http://127.0.0.1:8787/callback`. A browser connection error is expected because browser loopback is not the remote container. Copy the complete callback URL from the browser address bar and paste it into the no-echo terminal prompt.

## Runtime design

The container does not use `sleep infinity`, `tail -f /dev/null`, cron, or a disposable external server. `news-playlist serve` is the durable application host. Today it owns process lifecycle, the read-only status portal, persistent-path validation, graceful signal handling and the health endpoint. Later the scheduler/engine and authenticated private administration can grow inside the same runtime model.

The health endpoint performs a small real write, flush and `fsync` against `/data`, then removes the temporary probe file. This makes storage-full, quota and write failures observable as an unhealthy container instead of relying only on permission bits.
