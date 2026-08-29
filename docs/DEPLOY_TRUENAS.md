# TrueNAS deployment

The first supported NAS target is TrueNAS 26-BETA.3 or newer. This project is installed as a **Custom App from YAML**; it is not packaged for the TrueNAS Community catalog. Use **Apps -> Discover Apps -> menu -> Install via YAML**.

## Storage first

Create a dedicated dataset before installing the app, for example:

`POOL/apps/news-bulletin-playlist`

The container runs as numeric UID/GID `10001:10001`. Grant that UID (or GID) **Modify** access to the dataset ACL. TrueNAS does not require a matching local user account for a numeric app UID/GID.

The runtime root filesystem is read-only. `/data` is the only persistent writable application path; `/tmp` is an in-memory tmpfs.

## Install via YAML

1. Open `deploy/truenas.yaml`.
2. Replace `POOL` in `/mnt/POOL/apps/news-bulletin-playlist` with the actual pool name.
3. In TrueNAS open **Apps -> Discover Apps -> menu -> Install via YAML**.
4. Use application name `news-bulletin-playlist`.
5. Paste the YAML and save.
6. Wait for the container health state to become healthy.
7. Browse to `http://<truenas-host>:8788/`.

The application listens on container port `8080`. The TrueNAS YAML publishes it as host port `8788` using `ports:`. This mapping is what makes the status page reachable.

The YAML also includes top-level `x-portals` metadata matching TrueNAS catalog-generated Compose files and the existing remote-dev deployment convention. This is safe Compose extension metadata, but the current TrueNAS documentation explicitly says Custom Apps installed via YAML might not show a **Web UI** button in the Application Info widget. Therefore the deployment does **not** depend on `x-portals`: use `http://<truenas-host>:8788/` if the button is absent. `x-portals` never publishes the port by itself.

The current `/` page is intentionally read-only: it shows runtime, persistent-storage and version status only. It contains no credentials and has no administrative actions. `/healthz` remains the Docker health endpoint. Do not expose port `8788` directly to the public Internet; keep it on the LAN or a trusted private network such as Tailscale. Authentication must be added before future state-changing administration is exposed through the web UI.

If host port `8788` is already occupied, change the host side of `8788:8080` and the `x-portals` `port` to the same free value. Keep the container target at `8080`.

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
