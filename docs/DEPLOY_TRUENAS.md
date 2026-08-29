# TrueNAS deployment

The first supported NAS target is TrueNAS 26-BETA.3 or newer. TrueNAS custom applications use Docker Compose YAML through **Apps -> Discover Apps -> menu -> Install via YAML**.

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

The YAML intentionally publishes no host ports. The only current HTTP endpoint is a localhost-only `/healthz` endpoint used by Docker HEALTHCHECK. A future private admin UI can add an explicit port without changing the container/runtime model.

## Image integrity and updates

The checked-in TrueNAS YAML is pinned to an exact GHCR `sha256` digest that was built and validated by CI. It does **not** deploy the mutable `latest` tag. Reinstalling or restarting therefore resolves to the same reviewed image.

The GHCR workflow still publishes convenience `latest`, `sha-<git-sha>` and version tags from `main`, but production-style TrueNAS deployment should update only through a reviewed PR that replaces the digest in `deploy/truenas.yaml`. Keeping the previous digest in Git history provides a deterministic rollback path.

The first GHCR publication must be anonymously pullable for the supplied TrueNAS YAML. If GitHub creates the container package as private, change the package visibility to **Public** before installing it on TrueNAS; do not add registry credentials to the repository.

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

The container does not use `sleep infinity`, `tail -f /dev/null`, cron, or a fake external web server. `news-playlist serve` is the durable application host. Today it owns process lifecycle, persistent-path validation, graceful signal handling, and the internal health endpoint. Later the scheduler/engine can run inside this same host without replacing the Docker/TrueNAS deployment model.

The health endpoint performs a small real write, flush and `fsync` against `/data`, then removes the temporary probe file. This makes storage-full, quota and write failures observable as an unhealthy container instead of relying only on permission bits.
