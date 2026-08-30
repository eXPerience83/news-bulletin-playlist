# News Bulletin Playlist

Open-source service for dynamic news bulletin playlists across countries and languages,
with Spotify as the first destination.

> Pre-release. The P1 production multi-playlist engine is complete; provisioning and operating the first public Spain / Spanish-language playlist is the next release step. This project is not affiliated with or endorsed by Spotify or any news provider.

## Goal

Run **one engine that can maintain multiple news-bulletin playlists** from shared provider data.

Spain / Spanish-language news is the first implementation and validation target. The architecture is intentionally multi-playlist, multi-country and multi-language so the same runtime can later power English, French, German, Polish and other European playlists without duplicating the application.

Core architectural invariant:

> **Fetch once -> normalize once -> store once -> distribute to many playlists.**

A provider/source is independent from a playlist and may feed several playlists. A single engine run fetches each required source once, normalizes and persists it once, then evaluates and reconciles every configured destination playlist independently.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the architectural contract and invariants.

Initial product defaults:

- keep episodes published within the last **48 hours**;
- cap a playlist at **100 episodes**;
- order by source publication timestamp (`published_at`), newest first;
- retain operational metadata locally for **30 days**;
- run the production engine approximately every **10 minutes**;
- never download or store podcast audio;
- treat RSS/provider metadata as the timing source and Spotify as the playlist destination;
- allow playlist-specific policies to override defaults when required.

## Editorial independence

Playlist inclusion and ordering are based on source/editorial rules, freshness and playlist policy — **never on payment, donations, sponsorship or other compensation**.

Paid playlist placement, guaranteed inclusion, paid priority and sponsor-funded changes to playlist content are explicitly outside the product design.

Possible future monetization of the software or a separate qualifying non-streaming service is a different concern and must remain separate from playlist composition.

## Spotify access model

As of 2026-08-27, Spotify Development Mode is intended for learning, experimentation and personal **non-commercial** projects. The current deployment is therefore treated as non-commercial.

Spotify's Developer Policy permits certain limited commercial uses for qualifying non-streaming applications, but any future commercial use would require re-checking Spotify's then-current access mode, eligibility requirements and policies. Development Mode must not be used as the assumed foundation for building or scaling a business.

See [`docs/P0_FINDINGS.md`](docs/P0_FINDINGS.md) for the current Spotify platform constraints and the authenticated probes still required.

## P0 providers

The initial provider research focuses on the first Spain / Spanish-language playlist.

| Provider | Parser | Spotify show identified | Status |
| --- | --- | --- | --- |
| Cadena SER | ✅ | ✅ | core |
| RNE | ✅ | ✅ | core |
| Onda Cero | ✅ | ✅ | core |
| CNN 5 Cosas | ✅ | ✅ | core international |
| COPE | ✅ national title contract | ⚠️ authenticated lookup pending | candidate |

See [`docs/P0_FINDINGS.md`](docs/P0_FINDINGS.md) for the source research and the remaining authenticated Spotify probe.

## Container runtime

The supported runtime is a hardened Python 3.14 container. One long-lived process owns the HTTP status/admin surface and one sequential engine scheduler; it does **not** create one cron, worker or container per playlist.

The container uses a read-only root filesystem, runs as a non-root numeric UID/GID, keeps `/data` as its only persistent writable path and drops Linux capabilities. `/healthz` validates persistent storage. The read-only `/` page reports runtime/Spotify state plus last cycle, next run, source outcomes and playlist outcomes when the engine is configured.

For local Docker:

```bash
docker compose up --build -d
docker compose ps
```

The local status page is available only on `http://127.0.0.1:8788/`.

The base container remains healthy with no production engine configuration. To enable automatic cycles, copy [`config/news-bulletin-playlist.example.yaml`](config/news-bulletin-playlist.example.yaml) to `/data/news-bulletin-playlist.yaml`, replace the placeholder destination with the already provisioned Spotify playlist ID, and configure the production Spotify PKCE/HTTPS settings described in [`docs/DEPLOY_TRUENAS.md`](docs/DEPLOY_TRUENAS.md). The scheduler starts immediately and then runs every 600 seconds by default.

For TrueNAS 26-BETA.3 or newer, create a dedicated dataset with the **Apps** preset and install [`deploy/truenas.yaml`](deploy/truenas.yaml) as a **Custom App via YAML**, not as a Community catalog app. The base YAML publishes the read-only status UI on port `8788` and contains no Spotify credentials.

Production `/admin/` and the Spotify callback are intentionally fail-closed. When an external HTTPS origin is configured, administration is accepted only from the explicitly configured immediate reverse-proxy IP/CIDR and only when that proxy asserts `X-Forwarded-Proto: https`. Direct HTTP access to the backend never reaches the Basic-authentication challenge in that mode. Loopback HTTP remains available only for local/P0 development.

## Development

The project targets **Python 3.14**. PyYAML is the only runtime dependency; development checks use pytest, Ruff and mypy.

```bash
python3.14 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
pytest
ruff check .
mypy src
```

CI runs Ruff, mypy and pytest on Python 3.14. A separate container workflow validates Docker/Compose changes and publishes successful `main` builds to GHCR.

## Domain and configuration contract

P1 configuration is YAML and separates global source definitions from playlist policies and destination references. [`config/news-bulletin-playlist.example.yaml`](config/news-bulletin-playlist.example.yaml) is a non-production example: copy it to the persistent runtime and replace its Spotify destination with an already provisioned playlist ID before enabling automatic cycles.

The example uses the verified feeds and existing source IDs `ser`, `rne`, `ondacero` and `cnn`. COPE remains a disabled candidate without an invented endpoint or Spotify catalogue reference. Playlist `source_selection.explicit` is authoritative in schema version 1; playlist countries and languages describe editorial scope and do not implicitly filter that list. This is why a Spain-oriented playlist can explicitly include the US source CNN 5 Cosas.

Canonical editions are identified only by `(source_id, source_native_id)`. Titles and timestamps are metadata, never identity. Canonical timestamps are timezone-aware UTC values. Spotify show references are source catalogue metadata and are intentionally distinct from writable playlist destinations.

A cycle uses durable canonical/match state when building desired playlists. Therefore a transient source failure does not erase still-valid recent episodes; they age out naturally at the playlist retention boundary. Destination failures are isolated so one Spotify playlist cannot block another. If a source or Spotify catalogue lookup fails before enough last-known-good state exists to establish a safe desired state, the affected destination is preserved with zero reconciliation writes rather than treating an empty desired state as authoritative.

## License

Released under the [MIT License](LICENSE).

## Security

Do not commit Spotify client secrets, refresh tokens, `.env` files, administration passwords or the runtime database. Production Spotify authorization uses Authorization Code + PKCE and does not require a Spotify Client Secret. The durable refresh credential is stored owner-only under `/data`; access tokens remain memory-only.

The production administration surface must sit behind HTTPS. Configure `NEWS_PLAYLIST_TRUSTED_PROXY_CIDRS` with the immediate reverse proxy's source IP/CIDR, not a client or Tailscale subnet, and configure the proxy to overwrite `X-Forwarded-Proto` with `https`. Never expose the backend administration port as a trusted alternative to the HTTPS origin. Runtime OAuth operations and scheduler token refreshes are serialized so reconnect/refresh cannot race against the same credential store.

## Roadmap

The completed production-engine roadmap is recorded in the [P1 umbrella issue #13](https://github.com/eXPerience83/news-bulletin-playlist/issues/13).

1. **P0 — validated foundation** — provider contracts and watchdog, hardened container/TrueNAS runtime, plus Spotify catalogue/write probes for the first Spain / Spanish-language playlist.
2. **P1 — production multi-playlist engine — complete**:
   - [x] **P1.1 / #14** — source, canonical edition and playlist configuration/domain model; completed via #21.
   - [x] **P1.2 / #15** — shared RSS collection and canonical normalization; completed via #22.
   - [x] **P1.3 / #16** — SQLite persistence, migrations and 30-day operational retention; completed via #26.
   - [x] **P1.4 / #17** — deterministic source-to-Spotify episode matching; completed via #27.
   - [x] **P1.5 / #18** — desired-state generation and multi-playlist Spotify reconciliation; completed via #29.
   - [x] **P1.6 / #19** — production Spotify OAuth callback/token lifecycle through the private Web UI; completed via #30.
   - [x] **P1.7 / #20** — integrated engine cycle, scheduler and operational status in the durable runtime; completed via #31.
3. **First release — next** — provision and operate the first public Spain / Spanish-language playlist using the completed P1 engine.
4. **Expansion** — add source and playlist definitions for additional languages and European countries without duplicating the engine.

Parallel/non-blocking product work such as the playlist cover-art system in #12 may land when its configuration hook is stable, but it must never block bulletin synchronization.
